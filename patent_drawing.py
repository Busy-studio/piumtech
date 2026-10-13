"""Select a drawing from this PDF only. No cached, generated or template fallback.

Unlabelled scan detection is intentionally conservative: ambiguous pages are left
blank and reported for review, never silently substituted with page-one prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import unicodedata

import fitz
import numpy as np
from PIL import Image

_FIG = r"([0-9]{1,3}(?:[a-zA-Z]|\([a-zA-Z가-힣]\))?)"
_REP = re.compile(
    r"(?:[【\[]\s*대\s*표\s*도(?:\s*면)?\s*[】\]]|"
    r"(?m:^[ \t]*대\s*표\s*도(?:\s*면)?))"
    r"\s*[-–—:：]?\s*(?:도\s*)?" + _FIG + r"(?![0-9a-zA-Z])", re.I
)
_CAPTION = re.compile(
    r"^[ \t]*(?:[【\[]\s*)?(?:도|그림|Fig(?:ure)?\.?)\s*" + _FIG +
    r"\s*(?:[】\]])?[ \t]*$", re.I
)
_SECTION = re.compile(r"^[ \t]*[【\[]?\s*도\s*면\s*[】\]]?[ \t]*$", re.M)


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_representative_figure_number(text: str) -> str | None:
    match = _REP.search(_normal(text))
    return match.group(1).lower() if match else None


def _caption_numbers(text: str) -> list[str]:
    # A stand-alone "도 1" immediately after 【대표도】 is metadata, not a figure.
    text = _REP.sub("", _normal(text))
    return [m.group(1).lower() for line in text.splitlines()
            if (m := _CAPTION.fullmatch(line.strip()))]


def is_target_figure_page(text: str, figure: str) -> bool:
    return str(figure).lower() in _caption_numbers(text)


@dataclass
class DrawingResult:
    image: Image.Image | None = field(default=None, repr=False)
    status: str = "not_found"
    page: int | None = None  # 1-based PDF page, not the printed footer
    figure: str | None = None
    source_sha256: str = ""
    reason: str = "도면을 찾지 못해 대표도면 영역을 공란으로 유지합니다."

    def metadata(self) -> dict:
        return {key: value for key, value in vars(self).items() if key != "image"}


def pdf_fingerprint(path: str | Path) -> str:
    digest = sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lines(page) -> list[tuple[str, fitz.Rect]]:
    # TEXTFLAGS_TEXT avoids copying all the raster images into the text dict.
    result = []
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            result.append((_normal(text).strip(), fitz.Rect(line["bbox"])))
    return sorted(result, key=lambda item: (round(item[1].y0, 1), item[1].x0))


def _captions(page, native: str) -> list[tuple[str, fitz.Rect]]:
    allowed = _caption_numbers(native)
    result = []
    lines = _lines(page)
    for index, (text, rect) in enumerate(lines):
        match = _CAPTION.fullmatch(text)
        if not match or match.group(1).lower() not in allowed:
            continue
        previous = "\n".join(t for t, _ in lines[max(0, index - 2):index + 1])
        if _REP.search(previous):
            continue
        result.append((match.group(1).lower(), rect))
    return result


def _content_chars(text: str) -> int:
    text = _REP.sub("", _normal(text))
    rest = []
    for line in text.splitlines():
        line = line.strip()
        if (_CAPTION.fullmatch(line) or _SECTION.fullmatch(line)
                or re.fullmatch(r"[-\d\s/]+", line or " ")):
            continue
        rest.append(line)
    return len(re.sub(r"\s", "", "".join(rest)))


def _bands(values: np.ndarray) -> list[tuple[int, int]]:
    changes = np.diff(np.r_[False, values, False].astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


def _has_long_run(line: np.ndarray, minimum: int) -> bool:
    return any(end - start >= minimum for start, end in _bands(line))


def _structural_scan(image: Image.Image) -> bool:
    small = image.convert("L")
    small.thumbnail((900, 1200))
    ink = np.asarray(small) < 180
    h, w = ink.shape
    if w < 70 or h < 60 or not .002 < ink.mean() < .38:
        return False
    horizontal = np.array([_has_long_run(row, max(35, int(w * .22))) for row in ink])
    vertical = np.array([_has_long_run(col, max(30, int(h * .12))) for col in ink.T])
    # Box/flow diagrams and charts have several sustained lines. Text, a QR, and
    # a lone logo/border normally do not. Ambiguous photographs fail closed.
    return len(_bands(horizontal)) >= 2 and len(_bands(vertical)) >= 2


def _trim_graphic(image: Image.Image, scan: bool = False) -> Image.Image | None:
    image = image.convert("RGB")
    ink = np.asarray(image.convert("L")) < (185 if scan else 245)
    if scan:
        # Ignore isolated footer digits, but never cut a drawing touching the edge.
        bands = _bands(ink.sum(axis=1) > max(2, int(image.width * .002)))
        if len(bands) > 1:
            start, end = bands[-1]
            prev_end = bands[-2][1]
            if (start > image.height * .87 and start - prev_end > image.height * .06
                    and end - start < image.height * .05):
                ink[(prev_end + start) // 2:] = False
    ys, xs = np.where(ink)
    if not len(xs) or xs.max() - xs.min() < 25 or ys.max() - ys.min() < 25:
        return None
    pad = 12
    return image.crop((max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
                       min(image.width, int(xs.max()) + pad + 1),
                       min(image.height, int(ys.max()) + pad + 1)))


def _render(page, rect: fitz.Rect) -> Image.Image:
    # Image/line coordinates are unrotated; get_pixmap clip is in rotated space.
    rect = rect * page.rotation_matrix
    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False)
    return Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")


def _graphics(page) -> list[tuple[fitz.Rect, bool]]:
    """Displayed image occurrences, not unused resources; plus vector paths."""
    bounds = page.rect * page.derotation_matrix
    result = []
    for item in page.get_image_info():
        rect = fitz.Rect(item["bbox"]) & bounds
        if rect.width >= 48 and rect.height >= 40:
            result.append((rect, True))
    for item in page.get_drawings():
        rect = fitz.Rect(item["rect"]) & bounds
        if not rect.is_empty and (rect.width >= 15 or rect.height >= 15):
            result.append((rect, False))
        elif not rect.is_infinite and (rect.width >= 15 or rect.height >= 15):
            # Horizontal/vertical vector segments have zero-area bounds.
            result.append((fitz.Rect(rect.x0 - .5, rect.y0 - .5,
                                     rect.x1 + .5, rect.y1 + .5), False))
    return sorted(result, key=lambda item: (item[0].y0, item[0].x0))


def _within(rect: fitz.Rect, region: fitz.Rect) -> bool:
    return rect.get_area() > 0 and (rect & region).get_area() >= .9 * rect.get_area()


def _region_image(page, region, graphics, *, scan_label=False):
    included = [(r, raster) for r, raster in graphics if _within(r, region)]
    if not included:
        # A full-page scan can cover the label too. Only allow it on an explicitly
        # identified, non-prose figure page; never crop arbitrary first-page text.
        if scan_label:
            return _trim_graphic(_render(page, region), scan=True)
        return None
    bbox = fitz.Rect(included[0][0])
    for rect, _ in included[1:]:
        bbox |= rect
    if bbox.width < 50 or bbox.height < 40:
        return None
    # Include native vector labels situated just outside the line-art bounds.
    if any(not raster for _, raster in included):
        near = fitz.Rect(bbox.x0 - 18, bbox.y0 - 12, bbox.x1 + 18, bbox.y1 + 12)
        for text, rect in _lines(page):
            if text and _within(rect, region) and rect.intersects(near):
                bbox |= rect
    return _trim_graphic(_render(page, bbox), scan=False)


def _caption_region(page, rect, captions):
    bounds = page.rect * page.derotation_matrix
    x0, x1 = bounds.x0 + 12, bounds.x1 - 12
    y0, y1 = rect.y1 + 1, bounds.y1 * .945
    for _, other in captions:
        if other == rect:
            continue
        if abs(other.y0 - rect.y0) < 18:
            midpoint = (other.x0 + rect.x0) / 2
            if other.x0 > rect.x0:
                x1 = min(x1, midpoint)
            else:
                x0 = max(x0, midpoint)
        elif other.y0 > rect.y1:
            y1 = min(y1, other.y0 - 2)
    return fitz.Rect(x0, y0, x1, y1)


def extract_drawing_result(pdf_path: str, page_texts: list[str] | None = None,
                           ocr_used: bool = False) -> DrawingResult:
    """Explicit representative figure > first actual figure in page order > None.

    A declared-but-missing figure is a review condition, NOT permission to pick
    an unrelated figure. OCR text may guide selection but is never generated here.
    """
    result = DrawingResult()
    try:
        result.source_sha256 = pdf_fingerprint(pdf_path)
        with fitz.open(pdf_path) as doc:
            native = [p.get_text("text") or "" for p in doc]
            texts = [((page_texts[i] if page_texts and i < len(page_texts) else "")
                      or native[i]) for i in range(len(doc))]
            representative = next((n for t in texts
                                   if (n := parse_representative_figure_number(t))), None)
            result.figure = representative
            has_target_caption = bool(representative and any(
                is_target_figure_page(t, representative) for t in texts))
            gazette = bool(texts and re.search(r"(?:공개|등록)특허공보", re.sub(r"\s", "", texts[0])))
            ambiguous = False
            for index, page in enumerate(doc):
                captions = _captions(page, native[index])
                graphics = _graphics(page)
                if representative:
                    candidates = [(n, r) for n, r in captions if n == representative]
                else:
                    candidates = captions
                page_candidates = []
                for number, rect in candidates:
                    region = _caption_region(page, rect, captions)
                    image = _region_image(page, region, graphics)
                    if image is not None:
                        page_candidates.append((rect.y0, rect.x0, number, image))
                    else:
                        ambiguous = True
                # A gazette can place its drawing immediately below "대표도 - 도N".
                if (representative and gazette and not has_target_caption
                        and parse_representative_figure_number(texts[index])):
                    lines = _lines(page)
                    for pos, (text, rect) in enumerate(lines):
                        if re.search(r"대\s*표\s*도", text):
                            next_lines = "\n".join(t for t, _ in lines[pos:pos + 2])
                            if parse_representative_figure_number(next_lines) != representative:
                                continue
                            bottom = rect.y1
                            if parse_representative_figure_number(text) is None and pos + 1 < len(lines):
                                bottom = lines[pos + 1][1].y1
                            bounds = page.rect * page.derotation_matrix
                            region = fitz.Rect(bounds.x0 + 12, bottom + 1, bounds.x1 - 12, bounds.y1 * .945)
                            image = _region_image(page, region, graphics)
                            if image is not None:
                                page_candidates.append((rect.y0, rect.x0, representative, image))
                # OCR-only captions: admit only a sparse drawing page, not its summary.
                ocr_figures = _caption_numbers(texts[index])
                if not captions and ocr_figures and _content_chars(texts[index]) < 280:
                    if not representative or representative in ocr_figures:
                        if len(ocr_figures) == 1 and graphics:
                            bounds = page.rect * page.derotation_matrix
                            image = _region_image(page, bounds, graphics, scan_label=True)
                            if image is not None and _structural_scan(image):
                                page_candidates.append((0, 0, ocr_figures[0], _trim_graphic(image, scan=True)))
                        else:
                            ambiguous = True  # no boxes to separate multiple OCR figures
                # Numberless provisional diagrams: document order, not largest area.
                if not representative and not captions and not ocr_figures:
                    if _content_chars(texts[index]) < 120:
                        bounds = page.rect * page.derotation_matrix
                        for rect, raster in graphics:
                            if not raster or rect.get_area() < bounds.get_area() * .18:
                                continue
                            image = _render(page, rect)
                            if _structural_scan(image):
                                image = _trim_graphic(image, scan=True)
                                if image is not None:
                                    page_candidates.append((rect.y0, rect.x0, None, image))
                            else:
                                ambiguous = True
                # Numberless vector diagrams also occur in provisional documents.
                if (not representative and not captions and not ocr_figures
                        and _content_chars(texts[index]) < 120
                        and sum(not raster for _, raster in graphics) >= 3):
                    bounds = page.rect * page.derotation_matrix
                    image = _region_image(page, bounds, graphics)
                    if image is not None and _structural_scan(image):
                        page_candidates.append((min(r.y0 for r, _ in graphics),
                                                min(r.x0 for r, _ in graphics), None, image))
                    else:
                        ambiguous = True
                if page_candidates:
                    _, _, number, image = min(page_candidates, key=lambda item: (item[0], item[1]))
                    if image is not None:
                        return DrawingResult(image, "selected", index + 1, number,
                                             result.source_sha256,
                                             "명시된 대표도 사용" if representative else "문서의 첫 번째 도면 사용")
            if representative:
                result.status = "review_required"
                result.reason = f"대표도 도 {representative}의 실제 그림을 확인하지 못해 공란으로 유지합니다."
            elif ambiguous:
                result.status = "review_required"
                result.reason = "이미지는 있으나 도면으로 확정하지 못해 공란으로 유지합니다. 원본을 확인해 주세요."
    except Exception as exc:
        result.status = "error"
        result.reason = f"도면 추출 실패({type(exc).__name__}). 이전 도면을 사용하지 않고 공란으로 유지합니다."
    return result


def extract_representative_drawing(pdf_path: str, page_texts: list[str] | None = None,
                                   ocr_used: bool = False) -> Image.Image | None:
    """Compatibility API. Use extract_drawing_result when diagnostics are needed."""
    return extract_drawing_result(pdf_path, page_texts, ocr_used).image

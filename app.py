import os
import re
import json
import base64
import tempfile
import zipfile
import shutil
from io import BytesIO
from typing import Any, Dict, List, Tuple

import fitz
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageStat
from openai import OpenAI
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor

TEXT_MODEL_FIXED = "gpt-4.1-mini"
IMAGE_MODEL_FIXED = "gpt-image-1"

UNIVERSITIES = [
    "부산대학교", "국립부경대학교", "국립한국해양대학교", "동아대학교", "동의대학교", "동서대학교",
    "동명대학교", "신라대학교", "울산대학교", "경남대학교", "경상국립대학교", "국립창원대학교", "인제대학교", "수기입력"
]

st.set_page_config(page_title="PIUM Tech Brief 생성기", page_icon="📄", layout="wide")

# -----------------------------------------------------
# Client / Font
# -----------------------------------------------------
@st.cache_resource
def get_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    api_key = str(api_key).strip().replace('"', '').replace("'", "")
    if not api_key.startswith("sk-"):
        st.error("OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Secrets에 API 키를 입력하세요.")
        st.stop()
    return OpenAI(api_key=api_key)

@st.cache_resource
def load_font(size: int, bold: bool = False):
    paths = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
        "/Library/Fonts/AppleGothic.ttf",
        "C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf",
    ]
    for p in paths:
        if p and os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# -----------------------------------------------------
# Logo assets
# -----------------------------------------------------
LOGO_ZIP_PATH = "logo.zip"
LOGO_EXTRACT_DIR = os.path.join(tempfile.gettempdir(), "pium_logo_assets")

UNIV_ALIAS = {
    "경상대학교": "경상국립대학교",
    "창원대학교": "국립창원대학교",
}

def decode_zip_stem(name: str) -> str:
    """logo.zip 내부의 #Uxxxx 형태 파일명을 실제 한글명으로 복원."""
    stem = os.path.splitext(os.path.basename(name))[0]
    def repl(m):
        return chr(int(m.group(1), 16))
    return re.sub(r"#U([0-9A-Fa-f]{4})", repl, stem)

@st.cache_resource
def prepare_logo_assets() -> Dict[str, str]:
    logo_map: Dict[str, str] = {}
    zip_path_candidates = [
        LOGO_ZIP_PATH,
        os.path.join(os.getcwd(), LOGO_ZIP_PATH),
        os.path.join(os.path.dirname(__file__), LOGO_ZIP_PATH) if "__file__" in globals() else LOGO_ZIP_PATH,
    ]
    zip_path = next((z for z in zip_path_candidates if os.path.exists(z)), None)
    if not zip_path:
        return logo_map

    os.makedirs(LOGO_EXTRACT_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            decoded_name = decode_zip_stem(info.filename)
            ext = os.path.splitext(info.filename)[1].lower() or ".png"
            out_path = os.path.join(LOGO_EXTRACT_DIR, decoded_name + ext)
            with zf.open(info) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            logo_map[decoded_name] = out_path
    return logo_map

def get_logo_image(name: str) -> Image.Image | None:
    if not name:
        return None
    logos = prepare_logo_assets()
    key = UNIV_ALIAS.get(name, name)
    path = logos.get(key) or logos.get(name)
    if not path or not os.path.exists(path):
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None

def get_pium_logo_image() -> Image.Image | None:
    return get_logo_image("PIUM")

def get_piumlink_logo_image() -> Image.Image | None:
    """logo.zip 또는 프로젝트 루트에서 PIUMLINK 로고를 불러온다.
    파일명이 PIUMLINK.png, PIUM_LINK.png, 피움링크.png 등으로 들어와도 최대한 탐색한다.
    """
    # 1) logo.zip에서 정확명/느슨한 이름으로 탐색
    logos = prepare_logo_assets()
    for key, path in logos.items():
        norm = re.sub(r"[^0-9a-zA-Z가-힣]", "", str(key)).lower()
        if norm in ("piumlink", "pium링크", "피움링크") or ("pium" in norm and "link" in norm):
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass

    # 2) 프로젝트 루트/앱 폴더 직접 파일 탐색
    roots = [os.getcwd()]
    if "__file__" in globals():
        roots.append(os.path.dirname(__file__))
    candidates = ["PIUMLINK.png", "PIUM_LINK.png", "piumlink.png", "PiumLink.png", "피움링크.png"]
    for root in roots:
        for fname in candidates:
            path = os.path.join(root, fname)
            if os.path.exists(path):
                try:
                    return Image.open(path).convert("RGBA")
                except Exception:
                    pass
    return None

def fit_logo_on_blue(src: Image.Image, size: Tuple[int, int], bg=(0,55,135), padding=16) -> Image.Image:
    """투명 로고를 파란 박스 안에 비율 유지로 삽입."""
    canvas = Image.new("RGBA", size, bg + (255,))
    if src is None:
        return canvas.convert("RGB")
    im = src.copy().convert("RGBA")
    max_w = max(1, size[0] - padding*2)
    max_h = max(1, size[1] - padding*2)
    im.thumbnail((max_w, max_h), Image.LANCZOS)
    x = (size[0] - im.width)//2
    y = (size[1] - im.height)//2
    canvas.alpha_composite(im, (x,y))
    return canvas.convert("RGB")

def make_logo_box_image(logo_img: Image.Image | None, label: str, size=(155,155), bg=(0,55,135)) -> Image.Image:
    box = fit_logo_on_blue(logo_img, size, bg=bg, padding=18)
    if logo_img is None and label:
        d = ImageDraw.Draw(box)
        f = load_font(34, True)
        bbox = d.textbbox((0,0), label, font=f)
        d.text(((size[0]-(bbox[2]-bbox[0]))//2, (size[1]-(bbox[3]-bbox[1]))//2), label, font=f, fill="white")
    return box


# -----------------------------------------------------
# Theme / logo color utilities
# -----------------------------------------------------
def clamp(v: int) -> int:
    return max(0, min(255, int(v)))

def mix(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(clamp(c1[i] * (1 - t) + c2[i] * t) for i in range(3))

def luminance(c: Tuple[int, int, int]) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

def ensure_dark(c: Tuple[int, int, int]) -> Tuple[int, int, int]:
    # 본문/제목에 쓸 수 있도록 너무 밝으면 어둡게 보정
    if luminance(c) > 120:
        return mix(c, (0, 35, 85), 0.45)
    return c

def extract_logo_theme(logo_img: Image.Image | None) -> Dict[str, Tuple[int, int, int]]:
    """선택 대학 로고의 주요 색상을 추출해 전체 SMK 색상 테마로 사용."""
    default_primary = (0, 55, 135)
    if logo_img is None:
        primary = default_primary
    else:
        img = logo_img.copy().convert("RGBA")
        img.thumbnail((180, 180), Image.LANCZOS)
        colors = []
        for r, g, b, a in img.getdata():
            if a < 80:
                continue
            # 흰색/회색/검정에 가까운 영역 제외
            mx, mn = max(r, g, b), min(r, g, b)
            sat = mx - mn
            bright = (r + g + b) / 3
            if bright > 232 or bright < 25 or sat < 28:
                continue
            colors.append((r, g, b))
        if not colors:
            primary = default_primary
        else:
            # 가장 많이 등장하는 계열을 안정적으로 잡기 위해 양자화 후 최빈값 사용
            buckets = {}
            for r, g, b in colors:
                key = (round(r / 24) * 24, round(g / 24) * 24, round(b / 24) * 24)
                buckets[key] = buckets.get(key, 0) + 1
            primary = max(buckets.items(), key=lambda x: x[1])[0]
            primary = tuple(clamp(v) for v in primary)
            primary = ensure_dark(primary)

    secondary = mix(primary, (30, 115, 210), 0.25)
    pale = mix(primary, (255, 255, 255), 0.88)
    pale2 = mix(primary, (255, 255, 255), 0.94)
    line = mix(primary, (220, 230, 242), 0.82)
    accent = mix(primary, (0, 175, 190), 0.35)
    return {
        "primary": primary,
        "secondary": secondary,
        "accent": accent,
        "pale": pale,
        "pale2": pale2,
        "line": line,
        "black": (28, 34, 43),
        "gray": (92, 99, 110),
    }

def make_university_logo_box(logo_img: Image.Image | None, label: str, size=(155,155), bg=(235,243,250), primary=(0,55,135)) -> Image.Image:
    """좌측 상단 대학 로고 박스: 선택 대학 로고 계열의 옅은 배경에 로고 삽입."""
    box = Image.new("RGBA", size, bg + (255,))
    if logo_img is not None:
        im = logo_img.copy().convert("RGBA")
        im.thumbnail((size[0]-26, size[1]-26), Image.LANCZOS)
        x = (size[0]-im.width)//2
        y = (size[1]-im.height)//2
        box.alpha_composite(im, (x, y))
    elif label:
        d = ImageDraw.Draw(box)
        f = load_font(24, True)
        lines = label.replace("국립", "국립\n") if len(label) > 5 else label
        bbox = d.multiline_textbbox((0, 0), lines, font=f, spacing=4)
        tx = (size[0] - (bbox[2]-bbox[0])) // 2
        ty = (size[1] - (bbox[3]-bbox[1])) // 2
        d.multiline_text((tx, ty), lines, font=f, fill=primary, align="center", spacing=4)
    return box.convert("RGB")

def make_transparent_logo_canvas(logo_img: Image.Image | None, size=(155,155), padding=8) -> Image.Image:
    """우측 상단 PIUM 로고: 박스 없이 흰/연한 헤더 위에 로고만 배치."""
    canvas = Image.new("RGBA", size, (255,255,255,0))
    if logo_img is None:
        d = ImageDraw.Draw(canvas)
        f = load_font(30, True)
        d.text((20, 55), "PIUM", font=f, fill=(0,55,135,255))
    else:
        im = logo_img.copy().convert("RGBA")
        im.thumbnail((size[0]-padding*2, size[1]-padding*2), Image.LANCZOS)
        x = (size[0]-im.width)//2
        y = (size[1]-im.height)//2
        canvas.alpha_composite(im, (x, y))
    bg = Image.new("RGBA", size, (255,255,255,0))
    bg.alpha_composite(canvas)
    return bg

# -----------------------------------------------------
# PDF extraction
# -----------------------------------------------------
def save_uploaded_file(uploaded_file) -> str:
    suffix = os.path.splitext(uploaded_file.name)[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name

def extract_patent_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    text = "\n".join([p.get_text("text") for p in doc])
    doc.close()
    return text[:60000]


def _render_page_image(doc: fitz.Document, page_index: int, zoom: float = 3.2) -> Image.Image:
    page = doc[page_index]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")


def _smart_trim_visual(img: Image.Image, threshold: int = 246, padding: int = 12) -> Image.Image:
    """흰 여백을 제거하되, 너무 공격적으로 자르지 않도록 패딩을 남긴다."""
    import numpy as np
    im = img.convert("RGB")
    arr = np.asarray(im)
    # 흰 배경이 아닌 픽셀. 특허 도면의 옅은 회색선도 살리기 위해 threshold를 약간 높게 둠.
    mask = np.any(arr < threshold, axis=2)
    ys, xs = np.where(mask)
    if len(xs) == 0 or len(ys) == 0:
        return im
    x1, x2 = max(xs.min() - padding, 0), min(xs.max() + padding + 1, im.width)
    y1, y2 = max(ys.min() - padding, 0), min(ys.max() + padding + 1, im.height)
    return im.crop((x1, y1, x2, y2))


def _binary_connected_bboxes(mask):
    """작은 ROI용 연결요소 bbox 추출. mask는 bool 2D."""
    import numpy as np
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    bboxes = []
    for yy in range(h):
        xs = np.where(mask[yy] & (~visited[yy]))[0]
        for sx in xs:
            if visited[yy, sx] or not mask[yy, sx]:
                continue
            stack = [(sx, yy)]
            visited[yy, sx] = True
            minx = maxx = sx
            miny = maxy = yy
            count = 0
            while stack:
                x, y = stack.pop()
                count += 1
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
                for nx in (x-1, x, x+1):
                    for ny in (y-1, y, y+1):
                        if nx == x and ny == y:
                            continue
                        if 0 <= nx < w and 0 <= ny < h and (not visited[ny, nx]) and mask[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((nx, ny))
            bboxes.append((minx, miny, maxx+1, maxy+1, count))
    return bboxes


def _crop_qr_by_fixed_area(page_img: Image.Image) -> Image.Image | None:
    """등록공보 1페이지 우측 상단의 QR만 안정적으로 크롭한다.
    QR 주변의 공보 구분선/본문 일부가 같이 들어가지 않도록, 우측 상단 ROI에서
    팽창(dilation)된 QR 클러스터만 찾아 다시 크롭한다.
    """
    import numpy as np
    from PIL import ImageFilter

    w, h = page_img.size
    # QR이 위치하는 우측 상단 영역만 넉넉히 가져옴. 이후 클러스터 분석으로 QR만 분리.
    roi = page_img.crop((int(w * 0.78), int(h * 0.010), int(w * 0.992), int(h * 0.125))).convert("RGB")
    arr = np.asarray(roi)
    raw_mask = np.any(arr < 245, axis=2).astype("uint8") * 255

    # QR의 작은 모듈들을 하나의 덩어리로 연결. 긴 수평선은 ratio 조건에서 제외됨.
    mask_img = Image.fromarray(raw_mask, mode="L").filter(ImageFilter.MaxFilter(13))
    mask = np.asarray(mask_img) > 0
    bboxes = _binary_connected_bboxes(mask)

    candidates = []
    for x1, y1, x2, y2, area in bboxes:
        bw, bh = x2 - x1, y2 - y1
        if bw < 45 or bh < 45:
            continue
        ratio = bw / max(1, bh)
        # QR은 정사각형에 가까움. 공보 가로선처럼 긴 요소는 제외.
        if 0.55 <= ratio <= 1.65:
            # 우측 상단에 가까운 큰 정사각형 덩어리를 우선
            score = area + bw * bh * 0.25 + x1 * 0.4 - y1 * 0.15
            candidates.append((score, x1, y1, x2, y2))

    if not candidates:
        # fallback: 기존보다 좁은 좌표. 그래도 실패하면 None.
        fallback = page_img.crop((int(w * 0.84), int(h * 0.018), int(w * 0.975), int(h * 0.105)))
        qr = _smart_trim_visual(fallback, threshold=245, padding=8)
        if qr.width < 35 or qr.height < 35:
            return None
    else:
        _, x1, y1, x2, y2 = max(candidates, key=lambda t: t[0])
        pad = 6
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(roi.width, x2 + pad), min(roi.height, y2 + pad)
        qr = roi.crop((x1, y1, x2, y2))
        qr = _smart_trim_visual(qr, threshold=245, padding=8)

    # QR 표시용 정사각 캔버스
    if qr.width < 35 or qr.height < 35:
        return None
    side = max(qr.width, qr.height)
    canvas = Image.new("RGB", (side, side), "white")
    canvas.paste(qr, ((side - qr.width)//2, (side - qr.height)//2))
    return canvas


def _crop_representative_from_first_page(page_img: Image.Image) -> Image.Image:
    """1페이지 하단의 '대표도' 실제 도면만 크롭한다.
    전체 페이지가 대표도면 박스에 들어가는 문제를 막기 위해, 페이지 하단 영역에서
    픽셀 밀도가 높은 시각 요소 클러스터를 찾아 대표도면으로 사용한다.
    """
    import numpy as np
    w, h = page_img.size

    # 대표도는 국내 공보 1페이지의 요약 아래, 대체로 하단 45% 영역에 위치
    # 페이지 번호/footer는 제외하기 위해 92%까지만 사용
    y0, y1 = int(h * 0.52), int(h * 0.925)
    x0, x1 = int(w * 0.04), int(w * 0.96)
    roi = page_img.crop((x0, y0, x1, y1)).convert("RGB")
    arr = np.asarray(roi)

    # 흰색이 아닌 픽셀 카운트. 텍스트보다 도면 영역이 행/열 방향 밀도가 높음.
    mask = np.any(arr < 245, axis=2)
    row_counts = mask.sum(axis=1)
    col_counts = mask.sum(axis=0)

    # 도면 행 클러스터 탐색: 너무 낮은 텍스트/잡음은 제외
    row_thr = max(6, int(roi.width * 0.018))
    active_rows = row_counts > row_thr

    clusters = []
    start = None
    for i, active in enumerate(active_rows):
        if active and start is None:
            start = i
        elif not active and start is not None:
            if i - start > 12:
                clusters.append((start, i, int(row_counts[start:i].sum())))
            start = None
    if start is not None and len(active_rows) - start > 12:
        clusters.append((start, len(active_rows), int(row_counts[start:].sum())))

    if clusters:
        # 픽셀량이 가장 큰 클러스터를 대표도면으로 판단
        cy1, cy2, _ = max(clusters, key=lambda t: t[2])
        # 위아래 여유 추가
        cy1 = max(0, cy1 - 25)
        cy2 = min(roi.height, cy2 + 25)
        sub = roi.crop((0, cy1, roi.width, cy2))
    else:
        sub = roi

    # 선택된 행 영역 내에서 실제 도면의 좌우 범위 산정
    arr2 = np.asarray(sub)
    mask2 = np.any(arr2 < 245, axis=2)
    col_counts2 = mask2.sum(axis=0)
    col_thr = max(4, int(sub.height * 0.018))
    xs = np.where(col_counts2 > col_thr)[0]
    ys = np.where(mask2.sum(axis=1) > max(4, int(sub.width * 0.01)))[0]

    if len(xs) and len(ys):
        pad = 28
        cx1, cx2 = max(0, xs.min() - pad), min(sub.width, xs.max() + pad + 1)
        cy1b, cy2b = max(0, ys.min() - pad), min(sub.height, ys.max() + pad + 1)
        sub = sub.crop((cx1, cy1b, cx2, cy2b))

    return _smart_trim_visual(sub, threshold=248, padding=16)



def _extract_best_embedded_image_from_page(doc: fitz.Document, page_index: int, prefer_square: bool = False, min_area: int = 15000) -> Image.Image | None:
    """PDF 페이지 내부 이미지 중 실제 도면/QR에 해당하는 이미지를 직접 추출한다.
    렌더링 페이지를 크롭하는 방식보다 전체 페이지가 잘못 들어가는 문제가 적다.
    """
    try:
        page = doc[page_index]
        candidates = []
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                rects = page.get_image_rects(xref)
                img_info = doc.extract_image(xref)
                w, h = int(img_info.get("width", 0)), int(img_info.get("height", 0))
                area = w * h
                if area < min_area:
                    continue
                ratio = w / max(1, h)
                if prefer_square and not (0.75 <= ratio <= 1.35):
                    continue
                if (not prefer_square) and (w < 120 or h < 90):
                    continue
                # 공보 대표도/도면은 보통 페이지 중하단 또는 도면 페이지 본문 영역에 있음.
                pos_score = 0
                if rects:
                    r = rects[0]
                    pos_score = float(r.y0) * 0.15 - float(r.x0) * 0.03
                score = area + pos_score
                candidates.append((score, xref, img_info))
            except Exception:
                continue
        if not candidates:
            return None
        _, xref, img_info = max(candidates, key=lambda t: t[0])
        im = Image.open(BytesIO(img_info["image"])).convert("RGB")
        return _smart_trim_visual(im, threshold=248, padding=12)
    except Exception:
        return None


def _parse_representative_figure_number(first_text: str) -> str | None:
    m = re.search(r"대\s*표\s*도\s*[-–—]?\s*도\s*([0-9]+)", first_text)
    return m.group(1) if m else None

def extract_representative_drawing(pdf_path: str) -> Image.Image:
    """대표도면 추출.
    1순위: 1페이지에 포함된 대표도 이미지 객체를 직접 추출
    2순위: '대 표 도 - 도N' 번호를 읽어 도면 페이지에서 해당 도면 이미지 객체를 추출
    3순위: 기존 렌더링 크롭 fallback
    """
    doc = fitz.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        return Image.new("RGB", (800, 600), "white")

    first_text = doc[0].get_text("text")

    # 1페이지에 대표도가 이미지 객체로 들어있는 경우가 가장 많음.
    first_embedded = _extract_best_embedded_image_from_page(doc, 0, prefer_square=False, min_area=30000)
    if first_embedded is not None and first_embedded.width > 160 and first_embedded.height > 120:
        doc.close()
        return first_embedded

    # 대표도 번호를 기준으로 도면 페이지에서 실제 이미지 객체 추출
    rep_no = _parse_representative_figure_number(first_text)
    if rep_no:
        patterns = [f"도면{rep_no}", f"도면 {rep_no}", f"도 {rep_no}"]
        for i in range(1, len(doc)):
            t = doc[i].get_text("text")
            if any(p in t for p in patterns):
                img = _extract_best_embedded_image_from_page(doc, i, prefer_square=False, min_area=25000)
                if img is not None:
                    doc.close()
                    return img

    # fallback: 도면 페이지 중 큰 이미지 객체 우선 추출
    for i in range(1, len(doc)):
        t = doc[i].get_text("text")
        if "도면" in t or "도 " in t:
            img = _extract_best_embedded_image_from_page(doc, i, prefer_square=False, min_area=25000)
            if img is not None:
                doc.close()
                return img

    # 최후 fallback: 1페이지 하단 크롭
    first_img = _render_page_image(doc, 0, zoom=3.2)
    doc.close()
    return _crop_representative_from_first_page(first_img)


def extract_qr_code_from_first_page(pdf_path: str) -> Image.Image | None:
    """등록공보 1페이지 우측 상단 QR 코드만 추출한다.
    우선 PDF의 작은 정사각형 이미지 객체를 직접 찾고, 실패 시 렌더링 ROI 크롭으로 보정한다.
    """
    try:
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            doc.close()
            return None
        page = doc[0]
        page_rect = page.rect
        candidates = []
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                img_info = doc.extract_image(xref)
                iw, ih = int(img_info.get("width", 0)), int(img_info.get("height", 0))
                if iw < 35 or ih < 35:
                    continue
                ratio = iw / max(1, ih)
                if not (0.75 <= ratio <= 1.35):
                    continue
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                r = rects[0]
                # 1페이지 우측 상단 영역의 정사각형 이미지 = QR 가능성 높음
                if r.x0 < page_rect.width * 0.70 or r.y0 > page_rect.height * 0.18:
                    continue
                score = (page_rect.width - r.x0) + (page_rect.height * 0.18 - r.y0) + iw * ih * 0.01
                candidates.append((score, img_info))
            except Exception:
                continue
        if candidates:
            _, img_info = max(candidates, key=lambda t: t[0])
            qr = Image.open(BytesIO(img_info["image"])).convert("RGB")
            qr = _smart_trim_visual(qr, threshold=245, padding=4)
            side = max(qr.width, qr.height)
            canvas = Image.new("RGB", (side, side), "white")
            canvas.paste(qr, ((side-qr.width)//2, (side-qr.height)//2))
            doc.close()
            return canvas

        img = _render_page_image(doc, 0, zoom=3.2)
        doc.close()
        return _crop_qr_by_fixed_area(img)
    except Exception:
        return None

# -----------------------------------------------------
# GPT
# -----------------------------------------------------
def safe_json_parse(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```json", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"^```", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    return json.loads(text)

def analyze_patent_with_gpt(patent_text: str, university: str, department: str, professor: str) -> Dict[str, Any]:
    client = get_client()
    prompt = f"""
너는 대학 기술마케팅자료(SMK) 작성 전문가다.
아래 특허 명세서를 바탕으로 카드형 1페이지 기술소개자료에 들어갈 내용을 생성하라.

작성 기준:
- 해당 분야 4년제 대학 졸업자가 이해할 수 있는 수준
- 기술이전/사업화 검토자가 빠르게 이해할 수 있는 개조식 문체
- 과장 금지, 특허 명세서 근거 기반
- 기술명은 마케팅용 제목으로 자연스럽게 정리하되 원 발명의 핵심을 유지
- 적용분야 제품은 3개, 각 명칭은 짧게
- 기술개요는 3개
- 기술 차별성은 3개
- 기존기술 한계는 2개
- 기술적 우위는 2개
- 지식재산권은 등록공보에 있는 출원번호/등록번호/출원일자/등록일자를 최대한 정확히 추출
- 값이 없으면 빈 문자열로 둔다
- 출력은 JSON만

JSON 형식:
{{
  "marketing_title": "",
  "subtitle": "",
  "original_title": "",
  "university": "{university}",
  "department": "{department}",
  "professor": "{professor}",
  "applications": [
    {{"name": "", "description": ""}},
    {{"name": "", "description": ""}},
    {{"name": "", "description": ""}}
  ],
  "overview": ["", "", ""],
  "differentiation": ["", "", ""],
  "limitations": ["", ""],
  "technical_advantages": ["", ""],
  "ip": {{
    "title": "",
    "application_number": "",
    "registration_number": "",
    "application_date": "",
    "registration_date": "",
    "applicant": ""
  }}
}}

대학교: {university}
학과: {department}
교수명: {professor}

특허 명세서:
{patent_text}
"""
    res = client.responses.create(model=TEXT_MODEL_FIXED, input=prompt, temperature=0.2)
    return safe_json_parse(res.output_text)

def generate_application_image(title: str, desc: str) -> Image.Image:
    client = get_client()
    prompt = f"""
Create ONE icon from a unified premium technology icon set for a Korean university tech-transfer one-page brochure.
Application: {title}
Description: {desc}

Mandatory visual style:
- pure white background only, no black background, no dark vignette, no colored backdrop
- consistent semi-isometric vector-flat illustration style
- clean blue/cyan/white/light-gray palette with subtle 3D depth
- same stroke thickness, same lighting direction, same icon scale
- centered object with generous white margin
- professional public-sector technology marketing style
- no text, no letters, no logos, no watermark
"""
    result = client.images.generate(model=IMAGE_MODEL_FIXED, prompt=prompt, size="1024x1024")
    img_b64 = result.data[0].b64_json
    img = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    return clean_dark_background(img)

def generate_application_images_set(apps: List[Dict[str, Any]]) -> List[Image.Image]:
    """3개 적용분야 아이콘을 한 번에 생성 후 3등분해 그림체를 최대한 통일."""
    client = get_client()
    app_text = []
    for idx, app in enumerate(apps[:3], 1):
        app_text.append(f"{idx}. {app.get('name','')} - {app.get('description','')}")
    joined = "\n".join(app_text)
    prompt = f"""
Create a horizontal set of THREE matching application icons for a Korean university technology brief.
The three icons must look like they belong to the exact same icon family.

Applications:
{joined}

Mandatory layout:
- 3 separate icons arranged left, center, right with large white spacing
- pure white background only
- no dividers, no text, no labels, no logos, no watermark

Mandatory unified visual style:
- consistent semi-isometric vector-flat illustration style
- clean blue/cyan/white/light-gray palette with subtle 3D depth
- same stroke thickness, same lighting direction, same icon scale
- centered objects, generous white margin
- professional public-sector technology marketing style
"""
    result = client.images.generate(model=IMAGE_MODEL_FIXED, prompt=prompt, size="1536x1024")
    img_b64 = result.data[0].b64_json
    sheet = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    sheet = clean_dark_background(sheet)
    w, h = sheet.size
    icons = []
    for i in range(3):
        crop = sheet.crop((i*w//3, 0, (i+1)*w//3, h))
        icons.append(crop)
    return icons

# -----------------------------------------------------
# Image utilities
# -----------------------------------------------------
def clean_dark_background(img: Image.Image) -> Image.Image:
    """검정/짙은 배경이 섞여 나온 경우 흰 배경으로 완화."""
    img = img.convert("RGB")
    w, h = img.size
    border = Image.new("RGB", (w*2 + h*2, 1), "white")
    px = []
    for x in range(w):
        px.append(img.getpixel((x, 0)))
        px.append(img.getpixel((x, h-1)))
    for y in range(h):
        px.append(img.getpixel((0, y)))
        px.append(img.getpixel((w-1, y)))
    avg = sum((r+g+b)/3 for r,g,b in px) / max(len(px), 1)
    if avg > 130:
        return img
    data = []
    for r,g,b in img.getdata():
        brightness = (r+g+b)/3
        # 매우 어두운 배경만 흰색으로 치환, 오브젝트 음영은 최대한 보존
        if brightness < 42 and max(r,g,b) - min(r,g,b) < 35:
            data.append((255,255,255))
        else:
            data.append((r,g,b))
    img.putdata(data)
    return img

def trim_light_margins(src: Image.Image, threshold: int = 248, padding: int = 8) -> Image.Image:
    """흰색 여백을 자동 제거해 아이콘/대표도면이 박스 안에서 작아 보이지 않도록 보정."""
    im = src.copy().convert("RGB")
    w, h = im.size
    pix = im.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b = pix[x, y]
            # 거의 흰 배경과 아주 연한 회색은 여백으로 간주
            if not (r >= threshold and g >= threshold and b >= threshold):
                # 너무 연한 그림자도 일부 포함되도록 보수적으로 판단
                if max(r, g, b) - min(r, g, b) > 8 or (r + g + b) / 3 < threshold - 6:
                    xs.append(x); ys.append(y)
    if not xs or not ys:
        return im
    x1, x2 = max(0, min(xs)-padding), min(w, max(xs)+padding)
    y1, y2 = max(0, min(ys)-padding), min(h, max(ys)+padding)
    if x2 <= x1 or y2 <= y1:
        return im
    return im.crop((x1, y1, x2, y2))

def fit_image(src: Image.Image, size: Tuple[int, int], bg=(255,255,255), trim: bool = True) -> Image.Image:
    im = src.copy().convert("RGB")
    if trim:
        im = trim_light_margins(im)
    im.thumbnail(size, Image.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    x = (size[0] - im.width)//2
    y = (size[1] - im.height)//2
    canvas.paste(im, (x,y))
    return canvas

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = None) -> List[str]:
    lines = []
    for raw in str(text).split("\n"):
        raw = raw.strip()
        if not raw:
            lines.append("")
            continue
        line = ""
        for ch in list(raw):
            test = line + ch
            if draw.textbbox((0,0), test, font=font)[2] <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = ch
                if max_lines and len(lines) >= max_lines:
                    break
        if max_lines and len(lines) >= max_lines:
            break
        if line:
            lines.append(line)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines

def draw_wrapped(draw, xy, text, font, fill, max_width, line_gap=7, max_lines=None) -> int:
    x, y = xy
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
    for i, line in enumerate(lines):
        if max_lines and i == max_lines-1 and len(wrap_text(draw, text, font, max_width)) > max_lines:
            line = line[:-1] + "…"
        draw.text((x, y), line, font=font, fill=fill)
        y += getattr(font, "size", 18) + line_gap
    return y


def draw_centered_wrapped(draw, box, text, font, fill, max_lines=None, line_gap=4):
    """주어진 박스 안에서 텍스트를 가로 중앙 정렬로 출력."""
    x1, y1, x2, y2 = box
    max_width = x2 - x1
    lines = wrap_text(draw, str(text or ""), font, max_width, max_lines=max_lines)
    line_h = getattr(font, "size", 18) + line_gap
    total_h = len(lines) * getattr(font, "size", 18) + max(0, len(lines)-1) * line_gap
    y = y1 + max(0, (y2 - y1 - total_h)//2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x1 + (max_width - tw)//2, y), line, font=font, fill=fill)
        y += line_h
    return y

def draw_section_title(draw, x, y, title, font, color):
    draw.rounded_rectangle((x, y, x+10, y+34), radius=4, fill=color)
    draw.text((x+22, y+2), title, font=font, fill=color)

def draw_card(draw, xyxy, radius=24, fill=(255,255,255), outline=(220,230,242), width=2):
    draw.rounded_rectangle(xyxy, radius=radius, fill=fill, outline=outline, width=width)

def bullet_list(draw, x, y, items, font, color, max_width, bullet="•", line_gap=7, item_gap=10, max_lines_per_item=3):
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        draw.text((x, y), bullet, font=font, fill=color)
        y = draw_wrapped(draw, (x+24, y), item, font, color, max_width-24, line_gap, max_lines=max_lines_per_item) + item_gap
    return y

# -----------------------------------------------------
# New Box Layout Rendering - boxed v2
# -----------------------------------------------------
def normalize_ip(ip: Dict[str, Any]) -> Dict[str, str]:
    return {
        "title": str(ip.get("title") or ip.get("name") or ""),
        "application_number": str(ip.get("application_number") or ip.get("app_no") or ""),
        "registration_number": str(ip.get("registration_number") or ip.get("reg_no") or ""),
        "application_date": str(ip.get("application_date") or ip.get("app_date") or ""),
        "registration_date": str(ip.get("registration_date") or ip.get("reg_date") or ""),
        "applicant": str(ip.get("applicant") or ""),
    }

def _font_size(font, fallback=18):
    return int(getattr(font, "size", fallback))

def draw_fitted_wrapped(draw, xy, text, size, bold, fill, max_width, max_height, line_gap=6, min_size=12, max_lines=None):
    """박스 밖으로 글자가 나가지 않도록 폰트 크기를 자동 축소해 출력."""
    x, y = xy
    text = str(text or "")
    for fs in range(size, min_size-1, -1):
        font = load_font(fs, bold)
        lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
        total_h = len(lines) * fs + max(0, len(lines)-1) * line_gap
        if total_h <= max_height:
            yy = y
            for i, line in enumerate(lines):
                if max_lines and i == max_lines-1 and len(wrap_text(draw, text, font, max_width)) > max_lines:
                    line = line[:-1] + "…"
                draw.text((x, yy), line, font=font, fill=fill)
                yy += fs + line_gap
            return yy
    # 그래도 안 맞으면 최소 폰트 + 줄 수 제한
    font = load_font(min_size, bold)
    max_fit_lines = max(1, int(max_height // (min_size + line_gap)))
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines or max_fit_lines)
    yy = y
    for i, line in enumerate(lines[:max_fit_lines]):
        if i == max_fit_lines-1:
            line = line[:-1] + "…" if len(line) > 1 else "…"
        draw.text((x, yy), line, font=font, fill=fill)
        yy += min_size + line_gap
    return yy

def draw_bullets_fit(draw, x, y, items, size, bold, color, max_width, max_height, bullet="›", line_gap=5, item_gap=8, min_size=12, max_lines_per_item=3):
    """불릿 목록이 지정 영역을 넘지 않도록 자동 축소."""
    items = [str(v).strip() for v in (items or []) if str(v).strip()]
    for fs in range(size, min_size-1, -1):
        font = load_font(fs, bold)
        yy = y
        ok = True
        cached = []
        for item in items:
            lines = wrap_text(draw, item, font, max_width-26, max_lines=max_lines_per_item)
            h = len(lines) * fs + max(0, len(lines)-1) * line_gap + item_gap
            if yy + h > y + max_height:
                ok = False
                break
            cached.append(lines)
            yy += h
        if ok:
            yy = y
            for lines in cached:
                draw.text((x, yy), bullet, font=font, fill=color)
                tx = x + 26
                for line in lines:
                    draw.text((tx, yy), line, font=font, fill=color)
                    yy += fs + line_gap
                yy += item_gap
            return yy
    # 최소 폰트에서도 넘치면 가능한 만큼만 출력
    font = load_font(min_size, bold)
    yy = y
    bottom = y + max_height
    for item in items:
        if yy >= bottom - min_size:
            break
        lines = wrap_text(draw, item, font, max_width-26, max_lines=max_lines_per_item)
        draw.text((x, yy), bullet, font=font, fill=color)
        tx = x + 26
        for line in lines:
            if yy > bottom - min_size:
                return yy
            draw.text((tx, yy), line, font=font, fill=color)
            yy += min_size + line_gap
        yy += item_gap
    return yy

def get_visual_palette(university_logo: Image.Image | None) -> Dict[str, Tuple[int, int, int]]:
    """대학 로고 대표색을 전체 SMK accent로 사용하되, 본문 가독성은 검정/회색으로 유지한다.
    PIUM 로고는 원본 색상을 그대로 두고, 주변 박스/라인만 대학색 계열 tint로 맞춘다.
    """
    uni_theme = extract_logo_theme(university_logo)
    uni_primary = uni_theme["primary"]

    # 대학 대표색을 기본 accent로 사용
    primary = uni_primary
    primary_dark = ensure_dark(mix(uni_primary, (0, 0, 0), 0.18))
    secondary = mix(primary, (0, 112, 185), 0.15)
    accent = mix(primary, (0, 170, 190), 0.20)

    # 너무 진하게 칠하지 않고, 같은 계열의 연한 tint로 박스/라인 구성
    pale = mix(primary, (255, 255, 255), 0.90)
    pale2 = mix(primary, (255, 255, 255), 0.94)
    pale3 = mix(primary, (255, 255, 255), 0.97)
    line = mix(primary, (255, 255, 255), 0.70)
    line_soft = mix(primary, (255, 255, 255), 0.82)
    table_header = mix(primary, (245, 247, 250), 0.82)

    return {
        "uni_primary": uni_primary,
        "uni_pale": pale,
        "uni_line": line,
        "pium_blue": primary,          # 기존 변수명 호환: 본문 accent도 대학색 계열 적용
        "tech_blue": secondary,
        "tech_cyan": accent,
        "tech_pale": pale2,
        "tech_pale2": pale3,
        "tech_line": line_soft,
        "table_header": table_header,
        "black": (28, 34, 43),
        "gray": (92, 99, 110),
    }



def _draw_shadowed_card(draw, xyxy, radius=28, fill=(255,255,255), outline=(220,230,242), width=2, shadow=True):
    """PPT 수정본처럼 카드에 은은한 그림자를 주는 렌더링 유틸."""
    x1,y1,x2,y2 = xyxy
    if shadow:
        for off, alpha in [(7, 32), (4, 22)]:
            sh = tuple(max(0, min(255, int(c*0.90))) for c in outline)
            draw.rounded_rectangle((x1+off, y1+off, x2+off, y2+off), radius=radius, fill=mix(sh, (255,255,255), 0.55))
    draw.rounded_rectangle(xyxy, radius=radius, fill=fill, outline=outline, width=width)


def compose_tech_brief(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str, university_logo: Image.Image | None = None, pium_logo: Image.Image | None = None, qr_img: Image.Image | None = None, piumlink_logo: Image.Image | None = None) -> Image.Image:
    """사용자가 수정한 PPTX 양식에 맞춘 1페이지 PIUM Tech Brief 렌더러."""
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)

    pal = get_visual_palette(university_logo)
    uni_primary = pal["uni_primary"]
    uni_pale = pal["uni_pale"]
    uni_line = pal["uni_line"]
    primary = pal["pium_blue"]
    secondary = pal["tech_blue"]
    sky = pal["tech_pale"]
    sky2 = pal["tech_pale2"]
    line = pal["tech_line"]
    table_header = pal["table_header"]
    black = pal["black"]
    gray = pal["gray"]

    d.rectangle((0, 0, W, H), fill=(255,255,255))

    # ---------------- Header ----------------
    header_h = 248
    d.rectangle((0, 0, W, header_h), fill=uni_pale)
    d.line((0, header_h, W, header_h), fill=mix(line, (160,160,160), 0.15), width=2)

    # 대학 로고 / 우측 PIUM + PIUMLINK를 같은 상단 기준선으로 정렬
    uni_logo_size = 124
    uni_logo_x, uni_logo_y = 28, 24
    left_logo = make_transparent_logo_canvas(university_logo, size=(uni_logo_size, uni_logo_size), padding=0) if university_logo else make_university_logo_box(None, data.get('university',''), size=(uni_logo_size, uni_logo_size), bg=uni_pale, primary=uni_primary)
    im.paste(left_logo, (uni_logo_x, uni_logo_y), left_logo if left_logo.mode == 'RGBA' else None)

    right_x = W - 172
    top_y = 28
    if pium_logo is not None:
        pium_canvas = make_transparent_logo_canvas(pium_logo, size=(145, 54), padding=0)
        im.paste(pium_canvas, (right_x+2, top_y), pium_canvas)
    # PIUMLINK는 사용자가 넣은 QR/로고 그대로, 별도 박스 없이 크게 배치
    if piumlink_logo is not None:
        link_size = 116
        link = make_transparent_logo_canvas(piumlink_logo, size=(link_size, link_size), padding=0)
        im.paste(link, (right_x+18, top_y+66), link)

    header_x = 190
    header_w = right_x - header_x - 22
    kicker = f"PIUM Tech Offer  x  {data.get('university','')}  |  {data.get('department','')}  |  {data.get('professor','')} 교수"
    draw_fitted_wrapped(d, (header_x, 54), kicker, 24, False, uni_primary, header_w, 30, line_gap=2, min_size=17, max_lines=1)
    draw_fitted_wrapped(d, (header_x, 92), data.get("marketing_title", "기술명"), 43, True, uni_primary, header_w, 78, line_gap=3, min_size=28, max_lines=2)

    sub_x, sub_y, sub_w, sub_h = header_x, 176, 820, 48
    _draw_shadowed_card(d, (sub_x, sub_y, sub_x+sub_w, sub_y+sub_h), radius=12, fill=(255,255,255), outline=uni_line, width=1, shadow=True)
    draw_fitted_wrapped(d, (sub_x+22, sub_y+12), data.get("subtitle", ""), 20, False, gray, sub_w-44, sub_h-16, line_gap=3, min_size=15, max_lines=1)

    # 공통 폭: PPT 수정본 기준의 넓은 본문 컨테이너
    X = 28
    CW = W - 56
    card_line = line
    sec_font = load_font(24, True)

    # ---------------- Applications ----------------
    app_y, app_h = 294, 238
    _draw_shadowed_card(d, (X, app_y, X+CW, app_y+app_h), radius=34, fill=(255,255,255), outline=card_line, width=2, shadow=True)
    draw_section_title(d, X+40, app_y+34, "적용분야 / 제품", sec_font, primary)
    apps = data.get("applications", [])[:3]
    col_w = CW // 3
    for i in range(3):
        col_x = X + i*col_w
        if i > 0:
            sep_x = col_x
            d.line((sep_x, app_y+88, sep_x, app_y+196), fill=mix(card_line, (255,255,255), 0.20), width=1)
        icon_size = (135, 102)
        icon_y = app_y + 74
        if i < len(app_imgs):
            icon = fit_image(app_imgs[i], icon_size, bg=(255,255,255), trim=True)
            im.paste(icon, (col_x + (col_w-icon_size[0])//2, icon_y))
        app = apps[i] if i < len(apps) else {"name":""}
        # 명칭은 박스 안에서 충분한 하단 여백을 두고 중앙 정렬
        draw_centered_wrapped(d, (col_x+20, app_y+178, col_x+col_w-20, app_y+218), app.get("name", ""), load_font(17, True), black, max_lines=2, line_gap=3)

    # ---------------- Overview / Differentiation ----------------
    y = 560
    gap = 30
    box_w = (CW - gap) // 2
    left_x = X
    right_x2 = X + box_w + gap
    box_h = 300
    for bx, title, items in [
        (left_x, "기술개요", data.get("overview", [])[:3]),
        (right_x2, "핵심 차별성", data.get("differentiation", [])[:3]),
    ]:
        _draw_shadowed_card(d, (bx, y, bx+box_w, y+box_h), radius=28, fill=sky, outline=card_line, width=2, shadow=True)
        draw_section_title(d, bx+36, y+34, title, sec_font, primary)
        draw_bullets_fit(d, bx+42, y+88, items, 17, False, black, box_w-84, box_h-112, bullet="›", line_gap=4, item_gap=8, min_size=12, max_lines_per_item=3)

    # ---------------- Representative / Competitiveness ----------------
    y = 890
    rep_w_box = 380
    comp_x = X + rep_w_box + 22
    comp_w = X + CW - comp_x
    comp_h = 332
    _draw_shadowed_card(d, (X, y, X+rep_w_box, y+comp_h), radius=28, fill=(255,255,255), outline=card_line, width=2, shadow=True)
    _draw_shadowed_card(d, (comp_x, y, comp_x+comp_w, y+comp_h), radius=28, fill=(255,255,255), outline=card_line, width=2, shadow=True)
    draw_section_title(d, X+36, y+34, "대표도면", sec_font, primary)
    draw_section_title(d, comp_x+36, y+34, "기술 경쟁력", sec_font, primary)

    rep = fit_image(rep_img, (rep_w_box-74, 220), bg=(255,255,255), trim=True)
    im.paste(rep, (X+37, y+84))

    # 기술 경쟁력 내부 박스: PPT 수정본처럼 넓고 낮게 배치
    inner_x = comp_x + 44
    inner_w = comp_w - 70
    sub1_y, sub1_h = y+76, 106
    sub2_y, sub2_h = y+200, 112
    _draw_shadowed_card(d, (inner_x, sub1_y, inner_x+inner_w, sub1_y+sub1_h), radius=16, fill=(255,255,255), outline=card_line, width=1, shadow=True)
    d.text((inner_x+20, sub1_y+15), "기존기술 한계", font=load_font(18, True), fill=gray)
    draw_bullets_fit(d, inner_x+22, sub1_y+47, data.get("limitations", [])[:2], 13, False, black, inner_w-44, sub1_h-52, bullet="•", line_gap=3, item_gap=2, min_size=10, max_lines_per_item=2)

    _draw_shadowed_card(d, (inner_x, sub2_y, inner_x+inner_w, sub2_y+sub2_h), radius=16, fill=sky2, outline=card_line, width=1, shadow=True)
    d.text((inner_x+20, sub2_y+15), "기술적 우위", font=load_font(18, True), fill=secondary)
    draw_bullets_fit(d, inner_x+22, sub2_y+47, data.get("technical_advantages", [])[:2], 13, False, primary, inner_w-44, sub2_h-52, bullet="▸", line_gap=3, item_gap=2, min_size=10, max_lines_per_item=2)

    # ---------------- IP + Shortcut QR ----------------
    y = 1264
    ip = normalize_ip(data.get("ip", {}))
    ip_h = 220
    qr_card_w = 232
    ip_w = CW - qr_card_w - 22
    _draw_shadowed_card(d, (X, y, X+ip_w, y+ip_h), radius=28, fill=sky2, outline=card_line, width=2, shadow=True)
    _draw_shadowed_card(d, (X+ip_w+22, y, X+CW, y+ip_h), radius=28, fill=(255,255,255), outline=card_line, width=2, shadow=True)
    draw_section_title(d, X+36, y+30, "지식재산권 현황", sec_font, primary)

    table_x, table_y = X+28, y+72
    table_w, table_h = ip_w-56, 132
    header_h2 = 40
    col1 = int(table_w*0.45)
    col2 = int(table_w*0.28)
    col3 = table_w - col1 - col2
    d.rectangle((table_x, table_y, table_x+table_w, table_y+header_h2), fill=table_header, outline=card_line)
    d.rectangle((table_x, table_y+header_h2, table_x+table_w, table_y+table_h), fill=(255,255,255), outline=card_line)
    c1 = table_x + col1
    c2 = c1 + col2
    for xx in [c1, c2]:
        d.line((xx, table_y, xx, table_y+table_h), fill=card_line, width=1)
    draw_centered_wrapped(d, (table_x+8, table_y+4, c1-8, table_y+header_h2-2), "발명의 명칭", load_font(17, True), black, max_lines=1)
    draw_centered_wrapped(d, (c1+8, table_y+3, c2-8, table_y+header_h2-2), "출원번호\n(등록번호)", load_font(12, True), black, max_lines=2, line_gap=1)
    draw_centered_wrapped(d, (c2+8, table_y+3, table_x+table_w-8, table_y+header_h2-2), "출원일자\n(등록일자)", load_font(12, True), black, max_lines=2, line_gap=1)
    draw_centered_wrapped(d, (table_x+12, table_y+header_h2+8, c1-12, table_y+table_h-8), ip["title"] or data.get("original_title", ""), load_font(12, False), black, max_lines=2, line_gap=2)
    num_text = f"{ip['application_number']}\n({ip['registration_number']})" if ip['registration_number'] else ip['application_number']
    date_text = f"{ip['application_date']}\n({ip['registration_date']})" if ip['registration_date'] else ip['application_date']
    draw_centered_wrapped(d, (c1+10, table_y+header_h2+8, c2-10, table_y+table_h-8), num_text, load_font(15, False), black, max_lines=2, line_gap=3)
    draw_centered_wrapped(d, (c2+10, table_y+header_h2+8, table_x+table_w-10, table_y+table_h-8), date_text, load_font(15, False), black, max_lines=2, line_gap=3)

    # QR 바로가기 별도 카드
    qr_x = X + ip_w + 22
    qr_w = qr_card_w
    draw_centered_wrapped(d, (qr_x+15, y+30, qr_x+qr_w-15, y+62), "바로가기", load_font(25, True), black, max_lines=1)
    if qr_img is not None:
        qr_size = 142
        qr = fit_image(qr_img, (qr_size, qr_size), bg=(255,255,255), trim=False)
        im.paste(qr, (qr_x + (qr_w-qr_size)//2, y+72))

    # ---------------- Contact ----------------
    y = 1518
    contact_h = 78
    _draw_shadowed_card(d, (X, y, X+CW, y+contact_h), radius=18, fill=(255,255,255), outline=card_line, width=2, shadow=True)
    d.text((X+42, y+25), "문의처", font=load_font(24, True), fill=primary)
    draw_fitted_wrapped(d, (X+160, y+29), contact, 18, False, black, CW-190, 28, line_gap=3, min_size=12, max_lines=1)

    return im

# -----------------------------------------------------
# PDF / PPTX
# -----------------------------------------------------
def make_pdf_bytes_from_image(img: Image.Image) -> bytes:
    a4_w, a4_h = 1240, 1754
    page = Image.new("RGB", (a4_w, a4_h), "white")
    img = img.convert("RGB")
    img.thumbnail((a4_w, a4_h))
    page.paste(img, ((a4_w-img.width)//2, (a4_h-img.height)//2))
    bio = BytesIO()
    page.save(bio, "PDF", resolution=150.0)
    return bio.getvalue()

def img_bytes(img: Image.Image) -> BytesIO:
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

def px(x):
    return Inches(x / 124.0)  # 1240px -> 10in

def add_textbox(slide, x, y, w, h, text, size=14, bold=False, color=(30,30,30), align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(px(x), px(y), px(w), px(h))
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = str(text)
    run.font.name = "Malgun Gothic"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor(*color)
    return box

def add_rect(slide, x, y, w, h, fill=(255,255,255), outline=(220,230,242), radius=False):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape_type, px(x), px(y), px(w), px(h))
    shp.fill.solid(); shp.fill.fore_color.rgb = RGBColor(*fill)
    shp.line.color.rgb = RGBColor(*outline)
    shp.line.width = Pt(1)
    return shp


def make_pptx_bytes(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str, university_logo: Image.Image | None = None, pium_logo: Image.Image | None = None, qr_img: Image.Image | None = None, piumlink_logo: Image.Image | None = None) -> bytes:
    """PPTX 수정용: 사용자가 수정한 PPT 템플릿과 유사한 박스형 레이아웃을 편집 가능한 객체로 구성."""
    pal = get_visual_palette(university_logo)
    uni_primary = pal["uni_primary"]
    uni_pale = pal["uni_pale"]
    uni_line = pal["uni_line"]
    primary = pal["pium_blue"]
    secondary = pal["tech_blue"]
    sky = pal["tech_pale"]
    sky2 = pal["tech_pale2"]
    line = pal["tech_line"]
    table_header = pal["table_header"]
    black = pal["black"]
    gray = pal["gray"]

    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(14.145)
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # Header
    add_rect(slide, 0, 0, 1240, 248, fill=uni_pale, outline=uni_pale)
    uni_logo_size = 124
    left_logo = make_transparent_logo_canvas(university_logo, size=(uni_logo_size, uni_logo_size), padding=0) if university_logo else make_university_logo_box(None, data.get('university',''), size=(uni_logo_size, uni_logo_size), bg=uni_pale, primary=uni_primary)
    slide.shapes.add_picture(img_bytes(left_logo), px(28), px(24), width=px(uni_logo_size), height=px(uni_logo_size))

    right_x = 1240 - 172
    top_y = 28
    if pium_logo is not None:
        pium_canvas = make_transparent_logo_canvas(pium_logo, size=(145, 54), padding=0)
        slide.shapes.add_picture(img_bytes(pium_canvas), px(right_x+2), px(top_y), width=px(145), height=px(54))
    if piumlink_logo is not None:
        link = make_transparent_logo_canvas(piumlink_logo, size=(116,116), padding=0)
        slide.shapes.add_picture(img_bytes(link), px(right_x+18), px(top_y+66), width=px(116), height=px(116))

    header_x = 190
    header_w = right_x - header_x - 22
    add_textbox(slide, header_x, 54, header_w, 30, f"PIUM Tech Offer  x  {data.get('university','')}  |  {data.get('department','')}  |  {data.get('professor','')} 교수", 13, False, uni_primary)
    add_textbox(slide, header_x, 92, header_w, 78, data.get("marketing_title", ""), 24, True, uni_primary)
    add_rect(slide, header_x, 176, 820, 48, fill=(255,255,255), outline=uni_line, radius=True)
    add_textbox(slide, header_x+22, 188, 776, 26, data.get("subtitle", ""), 11, False, gray)

    X = 28; CW = 1184
    # Applications
    app_y, app_h = 294, 238
    add_rect(slide, X, app_y, CW, app_h, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+40, app_y+34, 300, 36, "적용분야 / 제품", 14, True, primary)
    apps = data.get("applications", [])[:3]
    col_w = CW // 3
    for i in range(3):
        col_x = X + i*col_w
        if i < len(app_imgs):
            icon_w, icon_h = 135, 102
            slide.shapes.add_picture(img_bytes(fit_image(app_imgs[i], (icon_w, icon_h), trim=True)), px(col_x+(col_w-icon_w)//2), px(app_y+74), width=px(icon_w), height=px(icon_h))
        name = apps[i].get("name", "") if i < len(apps) else ""
        add_textbox(slide, col_x+20, app_y+178, col_w-40, 40, name, 10.5, True, black, align=PP_ALIGN.CENTER)

    # Overview / Difference
    y=560; gap=30; box_w=(CW-gap)//2; box_h=300
    for bx,title,items in [(X,"기술개요",data.get("overview",[])[:3]),(X+box_w+gap,"핵심 차별성",data.get("differentiation",[])[:3])]:
        add_rect(slide, bx, y, box_w, box_h, fill=sky, outline=line, radius=True)
        add_textbox(slide, bx+36, y+34, 250, 34, title, 14, True, primary)
        add_textbox(slide, bx+42, y+88, box_w-84, box_h-112, "\n".join(["› "+str(v) for v in items]), 9.5, False, black)

    # Rep + competitiveness
    y=890; rep_w_box=380; comp_x=X+rep_w_box+22; comp_w=X+CW-comp_x; comp_h=332
    add_rect(slide, X, y, rep_w_box, comp_h, fill=(255,255,255), outline=line, radius=True)
    add_rect(slide, comp_x, y, comp_w, comp_h, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+36, y+34, 180, 34, "대표도면", 14, True, primary)
    slide.shapes.add_picture(img_bytes(fit_image(rep_img, (rep_w_box-74,220), trim=True)), px(X+37), px(y+84), width=px(rep_w_box-74), height=px(220))
    add_textbox(slide, comp_x+36, y+34, 220, 34, "기술 경쟁력", 14, True, primary)
    inner_x=comp_x+44; inner_w=comp_w-70
    add_rect(slide, inner_x, y+76, inner_w, 106, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, inner_x+20, y+91, 220, 24, "기존기술 한계", 10, True, gray)
    add_textbox(slide, inner_x+22, y+123, inner_w-44, 52, "\n".join(["• "+str(v) for v in data.get("limitations",[])[:2]]), 7.2, False, black)
    add_rect(slide, inner_x, y+200, inner_w, 112, fill=sky2, outline=line, radius=True)
    add_textbox(slide, inner_x+20, y+215, 220, 24, "기술적 우위", 10, True, secondary)
    add_textbox(slide, inner_x+22, y+247, inner_w-44, 56, "\n".join(["▸ "+str(v) for v in data.get("technical_advantages",[])[:2]]), 7.2, False, primary)

    # IP + QR separate card
    y=1264; ip=normalize_ip(data.get("ip",{})); ip_h=220; qr_card_w=232; ip_w=CW-qr_card_w-22
    add_rect(slide, X, y, ip_w, ip_h, fill=sky2, outline=line, radius=True)
    add_rect(slide, X+ip_w+22, y, qr_card_w, ip_h, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+36, y+30, 260, 34, "지식재산권 현황", 14, True, primary)
    table_x, table_y, table_w, table_h = X+28, y+72, ip_w-56, 132
    add_rect(slide, table_x, table_y, table_w, table_h, fill=(255,255,255), outline=line)
    add_rect(slide, table_x, table_y, table_w, 40, fill=table_header, outline=line)
    col1=int(table_w*0.45); col2=int(table_w*0.28); col3=table_w-col1-col2
    for xx in [table_x+col1, table_x+col1+col2]:
        shp=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, px(xx), px(table_y), px(1), px(table_h))
        shp.fill.solid(); shp.fill.fore_color.rgb=RGBColor(*line); shp.line.fill.background()
    add_textbox(slide, table_x+8, table_y+8, col1-16, 26, "발명의 명칭", 10, True, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, table_x+col1+8, table_y+3, col2-16, 34, "출원번호\n(등록번호)", 7.5, True, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, table_x+col1+col2+8, table_y+3, col3-16, 34, "출원일자\n(등록일자)", 7.5, True, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, table_x+12, table_y+50, col1-24, 54, ip["title"] or data.get("original_title",""), 6.8, False, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, table_x+col1+8, table_y+50, col2-16, 54, f"{ip['application_number']}\n({ip['registration_number']})" if ip['registration_number'] else ip['application_number'], 8.6, False, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, table_x+col1+col2+8, table_y+50, col3-16, 54, f"{ip['application_date']}\n({ip['registration_date']})" if ip['registration_date'] else ip['application_date'], 8.6, False, black, align=PP_ALIGN.CENTER)
    add_textbox(slide, X+ip_w+22, y+30, qr_card_w, 34, "바로가기", 15, True, black, align=PP_ALIGN.CENTER)
    if qr_img is not None:
        qr_size=142
        qr_box=fit_image(qr_img, (qr_size, qr_size), bg=(255,255,255), trim=False)
        slide.shapes.add_picture(img_bytes(qr_box), px(X+ip_w+22+(qr_card_w-qr_size)//2), px(y+72), width=px(qr_size), height=px(qr_size))

    # Contact
    y=1518
    add_rect(slide, X, y, CW, 78, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+42, y+25, 100, 30, "문의처", 14, True, primary)
    add_textbox(slide, X+160, y+29, CW-190, 28, contact, 9, False, black)

    bio=BytesIO(); prs.save(bio); return bio.getvalue()



def _ensure_len(seq, n, default):
    out = list(seq or [])[:n]
    while len(out) < n:
        out.append(default() if callable(default) else default)
    return out

def build_data_from_edit_form(base: Dict[str, Any], university: str, department: str, professor: str, vals: Dict[str, Any]) -> Dict[str, Any]:
    """Streamlit 입력 폼 값을 내부 JSON 구조로 재조립한다."""
    return {
        "marketing_title": vals.get("marketing_title", ""),
        "subtitle": vals.get("subtitle", ""),
        "original_title": vals.get("original_title", ""),
        "university": university,
        "department": department,
        "professor": professor,
        "applications": [
            {"name": vals.get(f"app_name_{i}", ""), "description": vals.get(f"app_desc_{i}", "")}
            for i in range(3)
        ],
        "overview": [vals.get(f"overview_{i}", "") for i in range(3) if vals.get(f"overview_{i}", "").strip()],
        "differentiation": [vals.get(f"diff_{i}", "") for i in range(3) if vals.get(f"diff_{i}", "").strip()],
        "limitations": [vals.get(f"limit_{i}", "") for i in range(2) if vals.get(f"limit_{i}", "").strip()],
        "technical_advantages": [vals.get(f"adv_{i}", "") for i in range(2) if vals.get(f"adv_{i}", "").strip()],
        "ip": {
            "title": vals.get("ip_title", ""),
            "application_number": vals.get("ip_application_number", ""),
            "registration_number": vals.get("ip_registration_number", ""),
            "application_date": vals.get("ip_application_date", ""),
            "registration_date": vals.get("ip_registration_date", ""),
            "applicant": vals.get("ip_applicant", ""),
        },
    }

# -----------------------------------------------------
# Streamlit UI
# -----------------------------------------------------
st.title("PIUM SMK 생성기")
st.caption("특허 명세서 PDF를 업로드하면 카드형 1페이지 SMK를 생성합니다.")

with st.sidebar:
    st.header("입력 정보")
    uploaded_pdf = st.file_uploader("특허 명세서 PDF 업로드", type=["pdf"])
    selected_univ = st.selectbox("대학교", UNIVERSITIES, index=0)
    custom_univ = ""
    if selected_univ == "수기입력":
        custom_univ = st.text_input("대학교 수기입력", placeholder="예: ○○대학교")
    university = custom_univ.strip() if selected_univ == "수기입력" else selected_univ
    department = st.text_input("학과/소속", placeholder="예: 활빈당공학과")
    professor = st.text_input("교수명", placeholder="예: 홍길동")

    st.divider()
    st.subheader("문의처")
    org = st.text_input("소속", placeholder="예: 부산대학교 산학협력단")
    name = st.text_input("이름", placeholder="예: 고길동")
    position = st.text_input("직책", placeholder="예: 부장")
    phone = st.text_input("연락처", placeholder="예: 051.510.2741")
    email = st.text_input("이메일", placeholder="예: example@pusan.ac.kr")

    st.divider()
    make_images = st.checkbox("적용분야 이미지 생성", value=True, help="끄면 이미지 생성 비용이 발생하지 않습니다.")
    use_logos = st.checkbox("상단 로고 자동 삽입", value=True, help="logo.zip 안의 대학 로고와 PIUM 로고를 사용합니다.")
    generate_btn = st.button("SMK 생성", type="primary", use_container_width=True)

for key, default in {
    "data": None, "brief_image": None, "pdf_bytes": None, "pptx_bytes": None,
    "pdf_path": None, "app_imgs": [], "rep_img": None, "qr_img": None, "edit_version": 0
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

if generate_btn:
    if uploaded_pdf is None:
        st.error("특허 명세서 PDF를 업로드하세요."); st.stop()
    if not university:
        st.error("대학교명을 입력 또는 선택하세요."); st.stop()

    with st.spinner("PDF 분석 중..."):
        pdf_path = save_uploaded_file(uploaded_pdf)
        patent_text = extract_patent_text(pdf_path)
        rep_img = extract_representative_drawing(pdf_path)
        qr_img = extract_qr_code_from_first_page(pdf_path)
        st.session_state.pdf_path = pdf_path
        st.session_state.rep_img = rep_img
        st.session_state.qr_img = qr_img

    with st.spinner("GPT로 SMK 텍스트 생성 중..."):
        data = analyze_patent_with_gpt(patent_text, university, department, professor)
        data["university"] = university; data["department"] = department; data["professor"] = professor
        st.session_state.data = data
        st.session_state.edit_version += 1

    app_imgs=[]
    if make_images:
        with st.spinner("적용분야 이미지 세트 생성 중..."):
            try:
                app_imgs = generate_application_images_set(data.get("applications", [])[:3])
            except Exception as e:
                st.warning(f"적용분야 이미지 세트 생성 실패: {e}")
                # 세트 생성 실패 시 개별 생성으로 fallback
                for app in data.get("applications", [])[:3]:
                    try:
                        app_imgs.append(generate_application_image(app.get("name",""), app.get("description","")))
                    except Exception as e2:
                        st.warning(f"적용분야 이미지 생성 실패: {e2}")
                        app_imgs.append(Image.new("RGB", (1024,1024), "white"))
    else:
        app_imgs=[Image.new("RGB", (1024,1024), "white") for _ in data.get("applications", [])[:3]]
    st.session_state.app_imgs = app_imgs

    with st.spinner("PDF/PPTX 구성 중..."):
        contact = f"{org} {name} {position}  |  {phone}  |  {email}"
        university_logo = get_logo_image(university) if use_logos else None
        pium_logo = get_pium_logo_image() if use_logos else None
        piumlink_logo = get_piumlink_logo_image() if use_logos else None
        brief = compose_tech_brief(data, rep_img, app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)
        st.session_state.brief_image = brief
        st.session_state.pdf_bytes = make_pdf_bytes_from_image(brief)
        st.session_state.pptx_bytes = make_pptx_bytes(data, rep_img, app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)

if st.session_state.data is None:
    st.info("왼쪽에서 정보를 입력하고 특허 PDF를 업로드한 뒤 'SMK 생성'을 누르세요.")
else:
    col1, col2 = st.columns([1.15, 0.85], gap="large")
    with col1:
        st.subheader("SMK 미리보기")
        st.image(st.session_state.brief_image, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("PDF 다운로드", st.session_state.pdf_bytes, "PIUM_SMK.pdf", "application/pdf", type="primary", use_container_width=True)
        with c2:
            st.download_button("PPTX 다운로드(수정용)", st.session_state.pptx_bytes, "PIUM_SMK.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", use_container_width=True)

    with col2:
        st.subheader("생성 텍스트 수정")
        st.caption("JSON을 직접 고치지 않고 항목별 입력칸에서 수정할 수 있습니다.")

        data_now = st.session_state.data or {}
        apps_now = _ensure_len(data_now.get("applications", []), 3, lambda: {"name": "", "description": ""})
        overview_now = _ensure_len(data_now.get("overview", []), 3, "")
        diff_now = _ensure_len(data_now.get("differentiation", []), 3, "")
        limit_now = _ensure_len(data_now.get("limitations", []), 2, "")
        adv_now = _ensure_len(data_now.get("technical_advantages", []), 2, "")
        ip_now = normalize_ip(data_now.get("ip", {}))
        k = f"edit_v{st.session_state.edit_version}"

        with st.form("edit_text_form"):
            st.markdown("#### 기본 정보")
            marketing_title = st.text_input("기술명", value=data_now.get("marketing_title", ""), key=f"{k}_marketing_title")
            subtitle = st.text_input("한 줄 요약", value=data_now.get("subtitle", ""), key=f"{k}_subtitle")
            original_title = st.text_input("원 발명의 명칭", value=data_now.get("original_title", ""), key=f"{k}_original_title")

            st.markdown("#### 적용분야 / 제품")
            app_vals = []
            for i in range(3):
                with st.expander(f"적용분야 {i+1}", expanded=True):
                    app_name = st.text_input(f"적용분야 {i+1} 명칭", value=apps_now[i].get("name", ""), key=f"{k}_app_name_{i}")
                    app_desc = st.text_area(f"적용분야 {i+1} 설명", value=apps_now[i].get("description", ""), height=70, key=f"{k}_app_desc_{i}")
                    app_vals.append((app_name, app_desc))

            st.markdown("#### 기술개요")
            overview_vals = []
            for i in range(3):
                overview_vals.append(st.text_area(f"기술개요 {i+1}", value=overview_now[i], height=75, key=f"{k}_overview_{i}"))

            st.markdown("#### 핵심 차별성")
            diff_vals = []
            for i in range(3):
                diff_vals.append(st.text_area(f"핵심 차별성 {i+1}", value=diff_now[i], height=75, key=f"{k}_diff_{i}"))

            st.markdown("#### 기술 경쟁력")
            st.caption("기존기술 한계")
            limit_vals = []
            for i in range(2):
                limit_vals.append(st.text_area(f"기존기술 한계 {i+1}", value=limit_now[i], height=70, key=f"{k}_limit_{i}"))
            st.caption("기술적 우위")
            adv_vals = []
            for i in range(2):
                adv_vals.append(st.text_area(f"기술적 우위 {i+1}", value=adv_now[i], height=70, key=f"{k}_adv_{i}"))

            st.markdown("#### 지식재산권 현황")
            ip_title = st.text_input("발명의 명칭", value=ip_now.get("title", ""), key=f"{k}_ip_title")
            c_ip1, c_ip2 = st.columns(2)
            with c_ip1:
                ip_application_number = st.text_input("출원번호", value=ip_now.get("application_number", ""), key=f"{k}_ip_application_number")
                ip_application_date = st.text_input("출원일자", value=ip_now.get("application_date", ""), key=f"{k}_ip_application_date")
            with c_ip2:
                ip_registration_number = st.text_input("등록번호", value=ip_now.get("registration_number", ""), key=f"{k}_ip_registration_number")
                ip_registration_date = st.text_input("등록일자", value=ip_now.get("registration_date", ""), key=f"{k}_ip_registration_date")
            ip_applicant = st.text_input("출원인", value=ip_now.get("applicant", ""), key=f"{k}_ip_applicant")

            submitted = st.form_submit_button("수정 내용으로 PDF/PPTX 다시 생성", use_container_width=True)

        if submitted:
            vals = {
                "marketing_title": marketing_title,
                "subtitle": subtitle,
                "original_title": original_title,
                "ip_title": ip_title,
                "ip_application_number": ip_application_number,
                "ip_registration_number": ip_registration_number,
                "ip_application_date": ip_application_date,
                "ip_registration_date": ip_registration_date,
                "ip_applicant": ip_applicant,
            }
            for i, (app_name, app_desc) in enumerate(app_vals):
                vals[f"app_name_{i}"] = app_name
                vals[f"app_desc_{i}"] = app_desc
            for i, v in enumerate(overview_vals):
                vals[f"overview_{i}"] = v
            for i, v in enumerate(diff_vals):
                vals[f"diff_{i}"] = v
            for i, v in enumerate(limit_vals):
                vals[f"limit_{i}"] = v
            for i, v in enumerate(adv_vals):
                vals[f"adv_{i}"] = v

            edited = build_data_from_edit_form(data_now, university, department, professor, vals)
            contact = f"{org} {name} {position}  |  {phone}  |  {email}"
            rep_img = st.session_state.rep_img or extract_representative_drawing(st.session_state.pdf_path)
            university_logo = get_logo_image(university) if use_logos else None
            pium_logo = get_pium_logo_image() if use_logos else None
            piumlink_logo = get_piumlink_logo_image() if use_logos else None
            brief = compose_tech_brief(edited, rep_img, st.session_state.app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)
            st.session_state.data = edited
            st.session_state.brief_image = brief
            st.session_state.pdf_bytes = make_pdf_bytes_from_image(brief)
            st.session_state.pptx_bytes = make_pptx_bytes(edited, rep_img, st.session_state.app_imgs, contact, university_logo, pium_logo, st.session_state.qr_img, piumlink_logo)
            st.session_state.edit_version += 1
            st.success("수정 내용이 반영되었습니다.")
            st.rerun()

        with st.expander("고급 사용자용 JSON 보기"):
            st.json(st.session_state.data)

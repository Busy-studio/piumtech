"""Document-scoped state and exact drawing composition for premium output."""
from __future__ import annotations
from collections.abc import MutableMapping, Sequence
import numpy as np
from PIL import Image, ImageDraw, ImageOps

DRAWING_POLICY_VERSION = "2026-10-13.1"
# Known bundled design reference only. Never apply this mask to an arbitrary PDF.
STYLE_DRAWING_RECT = (.035, .665, .410, .823)
PREMIUM_DRAWING_SLOT = (.050, .680, .395, .805)
SLOT_COLOR = (255, 0, 255)


def _box(image: Image.Image, coordinates: tuple) -> tuple[int, int, int, int]:
    return tuple(round(value * (image.width if i % 2 == 0 else image.height))
                 for i, value in enumerate(coordinates))


def sanitize_style_reference(image: Image.Image) -> Image.Image:
    """Remove the sample patent drawing before the model can see/copy it."""
    image = image.convert("RGB").copy()
    ImageDraw.Draw(image).rectangle(_box(image, STYLE_DRAWING_RECT), fill="white")
    return image


def make_drawing_slot_reference(image: Image.Image) -> Image.Image:
    image = sanitize_style_reference(image)
    ImageDraw.Draw(image).rectangle(_box(image, PREMIUM_DRAWING_SLOT), fill=SLOT_COLOR)
    return image


def reference_manifest(descriptions: Sequence[str]) -> str:
    return "\n".join(f"- Image {i}: {description}" for i, description in enumerate(descriptions, 1))


def drawing_slot_instructions(has_drawing: bool) -> str:
    left, top, right, bottom = PREMIUM_DRAWING_SLOT
    state = ("A verified drawing from the current PDF is available; the program will insert it."
             if has_drawing else "The current PDF has NO verified drawing. The program will leave the slot empty.")
    return f"""
MANDATORY REPRESENTATIVE DRAWING SLOT:
{state}
- Keep the representative drawing card in the lower-left part of the page.
- Its image interior must be ONE completely solid, axis-aligned #FF00FF rectangle.
- Rectangle coordinates, as fractions of the full canvas: left={left}, top={top}, right={right}, bottom={bottom}.
- Keep the section heading ABOVE the rectangle. Put no text, lines, icons, border, shadows or gradients inside it.
- #FF00FF is a temporary machine-readable placeholder. Never use this color anywhere else.
- Do NOT render any patent drawing yourself, in this rectangle or anywhere else.
- Never copy a diagram from the design reference, or invent/substitute a diagram.
- Original drawing pixels will be composited by code AFTER generation; preserve the empty slot exactly.
"""


def composite_verified_drawing(generated: Image.Image, drawing: Image.Image | None) -> Image.Image:
    """Replace an unambiguous solid slot. Missing/ambiguous slot => fail closed.

    The caller must use the rule-based renderer when this raises ValueError.
    No coordinate guessing and no generated/template drawing may escape as output.
    """
    image = generated.convert("RGB").copy()
    pixels = np.asarray(image)
    mask = (pixels[:, :, 0] > 220) & (pixels[:, :, 1] < 45) & (pixels[:, :, 2] > 220)
    ys, xs = np.where(mask)
    if len(xs) < image.width * image.height * .015:
        raise ValueError("Premium drawing placeholder is missing")
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    w, h = image.size
    if not (.02 * w <= x0 < .15 * w and .30 * w < x1 <= .45 * w
            and .64 * h <= y0 < .73 * h and .77 * h < y1 <= .84 * h
            and mask[y0:y1, x0:x1].mean() >= .985):
        raise ValueError("Premium drawing placeholder is ambiguous or displaced")
    # Clear a 3px fringe to remove compression/antialiasing of the slot edges.
    bounds = (max(0, x0 - 3), max(0, y0 - 3), min(w, x1 + 3), min(h, y1 + 3))
    ImageDraw.Draw(image).rectangle(bounds, fill="white")
    if drawing is not None:
        target = (max(1, x1 - x0 - 12), max(1, y1 - y0 - 12))
        exact = ImageOps.contain(drawing.convert("RGB"), target, Image.Resampling.LANCZOS)
        image.paste(exact, (x0 + (x1 - x0 - exact.width) // 2,
                            y0 + (y1 - y0 - exact.height) // 2))
    return image


def reset_document_state(state: MutableMapping, source_hash: str | None) -> None:
    """Invalidate *all* output artifacts on upload change or generation restart."""
    for key in ("data", "brief_image", "pdf_bytes", "pptx_bytes", "pdf_path",
                "rep_img", "qr_img", "hq_image", "hq_png_bytes", "hq_pdf_bytes",
                "drawing_meta"):
        state[key] = None
    state["app_imgs"] = []
    state["source_pdf_sha256"] = source_hash
    state["drawing_policy_version"] = DRAWING_POLICY_VERSION
    state["edit_version"] = int(state.get("edit_version", 0)) + 1

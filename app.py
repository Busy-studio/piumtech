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
    "동명대학교", "신라대학교", "울산대학교", "경남대학교", "경상대학교", "창원대학교", "인제대학교", "수기입력"
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

def extract_representative_drawing(pdf_path: str) -> Image.Image:
    doc = fitz.open(pdf_path)
    candidate = []
    for i, page in enumerate(doc):
        t = page.get_text("text")
        if any(k in t for k in ["대표도", "대표 도", "도면1", "도 1", "도면 1"]):
            candidate.append(i)
    page_idx = candidate[-1] if candidate else max(len(doc)-1, 0)
    page = doc[page_idx]
    pix = page.get_pixmap(matrix=fitz.Matrix(2.8, 2.8), alpha=False)
    img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()
    return img

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
너는 대학 기술마케팅자료(SMK/Tech Brief) 작성 전문가다.
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
Create a premium technology brief application icon for a Korean university tech-transfer one-page brochure.
Application: {title}
Description: {desc}

Visual style requirements:
- white background only, no black background, no dark vignette, no gradient backdrop
- modern futuristic colored flat-icon mixed with subtle realistic 3D detail
- clean vector-like isometric composition
- blue, cyan, white, light gray accents
- professional public-sector technology marketing style
- centered object, generous white margin
- no text, no letters, no logos, no watermark
"""
    result = client.images.generate(model=IMAGE_MODEL_FIXED, prompt=prompt, size="1024x1024")
    img_b64 = result.data[0].b64_json
    img = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    return clean_dark_background(img)

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

def fit_image(src: Image.Image, size: Tuple[int, int], bg=(255,255,255)) -> Image.Image:
    im = src.copy().convert("RGB")
    im.thumbnail(size)
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

def compose_tech_brief(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str, university_logo: Image.Image | None = None, pium_logo: Image.Image | None = None) -> Image.Image:
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)

    theme = extract_logo_theme(university_logo)
    primary = theme["primary"]
    secondary = theme["secondary"]
    accent = theme["accent"]
    sky = theme["pale"]
    sky2 = theme["pale2"]
    line = theme["line"]
    black = theme["black"]
    gray = theme["gray"]
    table_header = mix(primary, (255,255,255), 0.88)

    f_kicker = load_font(24, False)
    f_title = load_font(42, True)
    f_sub = load_font(22, False)
    f_sec = load_font(25, True)
    f_card = load_font(20, True)
    f_body = load_font(18, False)
    f_small = load_font(15, False)
    f_tiny = load_font(14, False)

    d.rectangle((0, 0, W, H), fill=(255,255,255))

    # Header: 제목 영역은 낮게, subtitle은 별도 white pill로 분리
    header_h = 252
    d.rectangle((0, 0, W, header_h), fill=sky)

    LOGO_BOX = 150
    left_logo_box = make_university_logo_box(
        university_logo,
        data.get('university',''),
        size=(LOGO_BOX, LOGO_BOX),
        bg=sky,
        primary=primary,
    )
    im.paste(left_logo_box, (0, 0))

    # PIUM 로고는 박스 없이 우측 상단 배치
    pium_canvas = make_transparent_logo_canvas(pium_logo, size=(150, 86), padding=4)
    im.paste(pium_canvas, (W-178, 34), pium_canvas)

    header_x = 190
    header_w = W - header_x - 230
    kicker = f"PIUM Tech Offer  x  {data.get('university','')}  |  {data.get('department','')}  |  {data.get('professor','')} 교수"
    draw_fitted_wrapped(d, (header_x, 48), kicker, 24, False, primary, header_w, 32, line_gap=3, min_size=18, max_lines=1)
    draw_fitted_wrapped(d, (header_x, 88), data.get("marketing_title", "기술명"), 42, True, primary, header_w, 90, line_gap=5, min_size=30, max_lines=2)

    sub_x, sub_y, sub_w, sub_h = header_x, 186, header_w, 46
    d.rounded_rectangle((sub_x, sub_y, sub_x+sub_w, sub_y+sub_h), radius=16, fill=(255,255,255), outline=line, width=1)
    draw_fitted_wrapped(d, (sub_x+22, sub_y+11), data.get("subtitle", ""), 22, False, gray, sub_w-44, sub_h-18, line_gap=4, min_size=16, max_lines=1)

    X = 90
    CW = W - X*2

    # Applications: 3개 개별 박스가 아니라 큰 박스 1개 안에 3개 배치
    app_y = 300
    app_h = 252
    draw_card(d, (X, app_y, X+CW, app_y+app_h), radius=24, fill=(255,255,255), outline=line, width=2)
    draw_section_title(d, X+28, app_y+22, "적용분야 / 제품", f_sec, primary)
    apps = data.get("applications", [])[:3]
    inner_y = app_y + 76
    col_gap = 28
    col_w = (CW - 56 - col_gap*2)//3
    for i in range(3):
        x = X + 28 + i*(col_w+col_gap)
        # 은은한 내부 영역과 구분선
        if i > 0:
            sep_x = x - col_gap//2
            d.line((sep_x, inner_y+8, sep_x, app_y+app_h-28), fill=mix(line, (255,255,255), 0.35), width=1)
        if i < len(app_imgs):
            icon = fit_image(app_imgs[i], (126, 94), bg=(255,255,255))
            im.paste(icon, (x + (col_w-126)//2, inner_y))
        app = apps[i] if i < len(apps) else {"name":"", "description":""}
        draw_fitted_wrapped(d, (x+10, inner_y+108), app.get("name", ""), 20, True, black, col_w-20, 52, line_gap=4, min_size=14, max_lines=2)

    # Overview / differentiation
    y = 585
    left_x = X
    right_x = X + CW//2 + 15
    box_w = CW//2 - 15
    box_h = 302
    draw_card(d, (left_x, y, left_x+box_w, y+box_h), radius=24, fill=sky2, outline=line, width=2)
    draw_card(d, (right_x, y, right_x+box_w, y+box_h), radius=24, fill=sky2, outline=line, width=2)
    draw_section_title(d, left_x+28, y+24, "기술개요", f_sec, primary)
    draw_section_title(d, right_x+28, y+24, "핵심 차별성", f_sec, primary)
    draw_bullets_fit(d, left_x+34, y+80, data.get("overview", [])[:3], 18, False, black, box_w-68, box_h-105, bullet="›", line_gap=5, item_gap=8, min_size=13, max_lines_per_item=3)
    draw_bullets_fit(d, right_x+34, y+80, data.get("differentiation", [])[:3], 18, False, black, box_w-68, box_h-105, bullet="›", line_gap=5, item_gap=8, min_size=13, max_lines_per_item=3)

    # Drawing / competitiveness
    y = 920
    comp_h = 340
    draw_card(d, (left_x, y, left_x+box_w, y+comp_h), radius=24, fill=(255,255,255), outline=line, width=2)
    draw_card(d, (right_x, y, right_x+box_w, y+comp_h), radius=24, fill=(255,255,255), outline=line, width=2)
    draw_section_title(d, left_x+28, y+24, "대표도면", f_sec, primary)
    draw_section_title(d, right_x+28, y+24, "기술 경쟁력", f_sec, primary)
    rep = fit_image(rep_img, (box_w-100, 226), bg=(255,255,255))
    im.paste(rep, (left_x+50, y+84))

    sub_y1 = y + 76
    sub_h1 = 110
    d.rounded_rectangle((right_x+32, sub_y1, right_x+box_w-32, sub_y1+sub_h1), radius=16, fill=(255,255,255), outline=line, width=1)
    d.text((right_x+52, sub_y1+14), "기존기술 한계", font=f_card, fill=accent)
    draw_bullets_fit(d, right_x+52, sub_y1+46, data.get("limitations", [])[:2], 15, False, black, box_w-105, sub_h1-52, bullet="•", line_gap=4, item_gap=3, min_size=11, max_lines_per_item=2)

    sub_y2 = sub_y1 + sub_h1 + 18
    sub_h2 = 124
    d.rounded_rectangle((right_x+32, sub_y2, right_x+box_w-32, sub_y2+sub_h2), radius=16, fill=sky2, outline=line, width=1)
    d.text((right_x+52, sub_y2+14), "기술적 우위", font=f_card, fill=secondary)
    draw_bullets_fit(d, right_x+52, sub_y2+46, data.get("technical_advantages", [])[:2], 15, False, primary, box_w-105, sub_h2-52, bullet="▸", line_gap=4, item_gap=4, min_size=11, max_lines_per_item=2)

    # IP / contact
    y = 1292
    ip = normalize_ip(data.get("ip", {}))
    ip_h = 194
    draw_card(d, (X, y, X+CW, y+ip_h), radius=22, fill=sky2, outline=line, width=2)
    draw_section_title(d, X+28, y+20, "지식재산권 현황", f_sec, primary)
    table_x, table_y = X+28, y+66
    table_w, table_h = CW-56, 96
    col1, col2, col3 = int(table_w*0.42), int(table_w*0.29), int(table_w*0.29)
    d.rectangle((table_x, table_y, table_x+table_w, table_y+38), fill=table_header, outline=line)
    d.rectangle((table_x, table_y+38, table_x+table_w, table_y+table_h), fill=(255,255,255), outline=line)
    for xx in [table_x+col1, table_x+col1+col2]:
        d.line((xx, table_y, xx, table_y+table_h), fill=line, width=1)
    d.text((table_x+18, table_y+8), "발명의 명칭", font=f_card, fill=black)
    d.text((table_x+col1+18, table_y+3), "출원번호\n(등록번호)", font=f_small, fill=black, spacing=1)
    d.text((table_x+col1+col2+18, table_y+3), "출원일자\n(등록일자)", font=f_small, fill=black, spacing=1)
    draw_fitted_wrapped(d, (table_x+18, table_y+51), ip["title"] or data.get("original_title", ""), 14, False, black, col1-35, table_h-52, line_gap=3, min_size=10, max_lines=2)
    num_text = f"{ip['application_number']}\n({ip['registration_number']})" if ip['registration_number'] else ip['application_number']
    date_text = f"{ip['application_date']}\n({ip['registration_date']})" if ip['registration_date'] else ip['application_date']
    draw_fitted_wrapped(d, (table_x+col1+18, table_y+50), num_text, 18, False, black, col2-36, table_h-50, line_gap=3, min_size=12, max_lines=2)
    draw_fitted_wrapped(d, (table_x+col1+col2+18, table_y+50), date_text, 18, False, black, col3-36, table_h-50, line_gap=3, min_size=12, max_lines=2)

    y = 1516
    contact_h = 78
    d.rounded_rectangle((X, y, X+CW, y+contact_h), radius=20, fill=(255,255,255), outline=line, width=2)
    d.text((X+28, y+23), "문의처", font=f_sec, fill=primary)
    draw_fitted_wrapped(d, (X+150, y+27), contact, 18, False, black, CW-180, 32, line_gap=4, min_size=12, max_lines=1)

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

def make_pptx_bytes(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str, university_logo: Image.Image | None = None, pium_logo: Image.Image | None = None) -> bytes:
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(14.145)
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    theme = extract_logo_theme(university_logo)
    primary = theme["primary"]; secondary = theme["secondary"]; accent = theme["accent"]
    sky = theme["pale"]; sky2 = theme["pale2"]; line = theme["line"]
    black = theme["black"]; gray = theme["gray"]
    table_header = mix(primary, (255,255,255), 0.88)

    # Header
    add_rect(slide, 0, 0, 1240, 252, fill=sky, outline=sky)
    left_logo_box = make_university_logo_box(university_logo, data.get('university',''), size=(150,150), bg=sky, primary=primary)
    slide.shapes.add_picture(img_bytes(left_logo_box), px(0), px(0), width=px(150), height=px(150))
    pium_canvas = make_transparent_logo_canvas(pium_logo, size=(150,86), padding=4)
    slide.shapes.add_picture(img_bytes(pium_canvas), px(1062), px(34), width=px(150), height=px(86))
    add_textbox(slide, 190, 48, 820, 32, f"PIUM Tech Offer  x  {data.get('university','')}  |  {data.get('department','')}  |  {data.get('professor','')} 교수", 13, False, primary)
    add_textbox(slide, 190, 88, 820, 88, data.get("marketing_title", ""), 24, True, primary)
    add_rect(slide, 190, 186, 820, 46, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, 212, 197, 776, 26, data.get("subtitle", ""), 12, False, gray)

    X=90; CW=1060
    # Application one outer box
    app_y=300; app_h=252
    add_rect(slide, X, app_y, CW, app_h, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+28, app_y+22, 300, 40, "적용분야 / 제품", 15, True, primary)
    col_gap=28; col_w=(CW-56-col_gap*2)//3; inner_y=app_y+76
    for i in range(3):
        app = data.get("applications", [])[:3][i] if i < len(data.get("applications", [])[:3]) else {"name":""}
        x=X+28+i*(col_w+col_gap)
        if i < len(app_imgs):
            slide.shapes.add_picture(img_bytes(fit_image(app_imgs[i], (126,94))), px(x+(col_w-126)//2), px(inner_y), width=px(126), height=px(94))
        add_textbox(slide, x+10, inner_y+108, col_w-20, 54, app.get("name", ""), 11, True, black)

    left_x=X; right_x=X+CW//2+15; box_w=CW//2-15
    y=585; box_h=302
    for x,title,items in [(left_x,"기술개요",data.get("overview",[])[:3]),(right_x,"핵심 차별성",data.get("differentiation",[])[:3])]:
        add_rect(slide, x, y, box_w, box_h, fill=sky2, outline=line, radius=True)
        add_textbox(slide, x+28, y+24, 250, 35, title, 15, True, primary)
        add_textbox(slide, x+34, y+80, box_w-68, 210, "\n".join(["› "+str(v) for v in items]), 10, False, black)

    y=920; comp_h=340
    add_rect(slide, left_x, y, box_w, comp_h, fill=(255,255,255), outline=line, radius=True)
    add_rect(slide, right_x, y, box_w, comp_h, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, left_x+28, y+24, 250, 35, "대표도면", 15, True, primary)
    slide.shapes.add_picture(img_bytes(fit_image(rep_img, (box_w-100,226))), px(left_x+50), px(y+84), width=px(box_w-100), height=px(226))
    add_textbox(slide, right_x+28, y+24, 250, 35, "기술 경쟁력", 15, True, primary)
    add_rect(slide, right_x+32, y+76, box_w-64, 110, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, right_x+52, y+90, 260, 28, "기존기술 한계", 11, True, accent)
    add_textbox(slide, right_x+52, y+122, box_w-105, 52, "\n".join(["• "+str(v) for v in data.get("limitations",[])[:2]]), 8.5, False, black)
    add_rect(slide, right_x+32, y+204, box_w-64, 124, fill=sky2, outline=line, radius=True)
    add_textbox(slide, right_x+52, y+218, 260, 28, "기술적 우위", 11, True, secondary)
    add_textbox(slide, right_x+52, y+250, box_w-105, 62, "\n".join(["▸ "+str(v) for v in data.get("technical_advantages",[])[:2]]), 8.5, False, primary)

    y=1292; ip=normalize_ip(data.get("ip",{})); ip_h=194
    add_rect(slide, X, y, CW, ip_h, fill=sky2, outline=line, radius=True)
    add_textbox(slide, X+28, y+20, 300, 35, "지식재산권 현황", 15, True, primary)
    add_rect(slide, X+28, y+66, CW-56, 96, fill=(255,255,255), outline=line)
    # table header fill overlay
    add_rect(slide, X+28, y+66, CW-56, 38, fill=table_header, outline=line)
    add_textbox(slide, X+46, y+74, 380, 30, "발명의 명칭", 11, True, black)
    add_textbox(slide, X+465, y+70, 250, 36, "출원번호\n(등록번호)", 9, True, black)
    add_textbox(slide, X+760, y+70, 250, 36, "출원일자\n(등록일자)", 9, True, black)
    add_textbox(slide, X+46, y+116, 390, 48, ip["title"] or data.get("original_title",""), 8, False, black)
    add_textbox(slide, X+465, y+116, 250, 48, f"{ip['application_number']}\n({ip['registration_number']})" if ip['registration_number'] else ip['application_number'], 10, False, black)
    add_textbox(slide, X+760, y+116, 250, 48, f"{ip['application_date']}\n({ip['registration_date']})" if ip['registration_date'] else ip['application_date'], 10, False, black)

    y=1516
    add_rect(slide, X, y, CW, 78, fill=(255,255,255), outline=line, radius=True)
    add_textbox(slide, X+28, y+23, 120, 35, "문의처", 15, True, primary)
    add_textbox(slide, X+150, y+27, CW-180, 32, contact, 10, False, black)

    bio=BytesIO(); prs.save(bio); return bio.getvalue()

# -----------------------------------------------------
# Streamlit UI
# -----------------------------------------------------
st.title("PIUM Tech Brief 생성기")
st.caption("특허 명세서 PDF를 업로드하면 카드형 1페이지 Tech Brief, PDF, PPTX를 생성합니다.")

with st.sidebar:
    st.header("입력 정보")
    uploaded_pdf = st.file_uploader("특허 명세서 PDF 업로드", type=["pdf"])
    selected_univ = st.selectbox("대학교", UNIVERSITIES, index=0)
    custom_univ = ""
    if selected_univ == "수기입력":
        custom_univ = st.text_input("대학교 수기입력", placeholder="예: ○○대학교")
    university = custom_univ.strip() if selected_univ == "수기입력" else selected_univ
    department = st.text_input("학과/소속", placeholder="예: 컴퓨터공학과")
    professor = st.text_input("교수명", placeholder="예: 홍길동")

    st.divider()
    st.subheader("문의처")
    org = st.text_input("소속", placeholder="예: 부산대학교 산학협력단")
    name = st.text_input("이름", placeholder="예: 윤재철")
    position = st.text_input("직책", placeholder="예: 차장")
    phone = st.text_input("연락처", placeholder="예: 051.510.2741")
    email = st.text_input("이메일", placeholder="예: example@pusan.ac.kr")

    st.divider()
    make_images = st.checkbox("적용분야 이미지 생성", value=True, help="끄면 이미지 생성 비용이 발생하지 않습니다.")
    use_logos = st.checkbox("상단 로고 자동 삽입", value=True, help="logo.zip 안의 대학 로고와 PIUM 로고를 사용합니다.")
    generate_btn = st.button("Tech Brief 생성", type="primary", use_container_width=True)

for key, default in {
    "data": None, "brief_image": None, "pdf_bytes": None, "pptx_bytes": None,
    "pdf_path": None, "app_imgs": [], "rep_img": None
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
        st.session_state.pdf_path = pdf_path
        st.session_state.rep_img = rep_img

    with st.spinner("GPT로 Tech Brief 텍스트 생성 중..."):
        data = analyze_patent_with_gpt(patent_text, university, department, professor)
        data["university"] = university; data["department"] = department; data["professor"] = professor
        st.session_state.data = data

    app_imgs=[]
    if make_images:
        with st.spinner("적용분야 이미지 생성 중..."):
            for app in data.get("applications", [])[:3]:
                try:
                    app_imgs.append(generate_application_image(app.get("name",""), app.get("description","")))
                except Exception as e:
                    st.warning(f"적용분야 이미지 생성 실패: {e}")
                    app_imgs.append(Image.new("RGB", (1024,1024), "white"))
    else:
        app_imgs=[Image.new("RGB", (1024,1024), "white") for _ in data.get("applications", [])[:3]]
    st.session_state.app_imgs = app_imgs

    with st.spinner("PDF/PPTX 구성 중..."):
        contact = f"{org} {name} {position}  |  {phone}  |  {email}"
        university_logo = get_logo_image(university) if use_logos else None
        pium_logo = get_pium_logo_image() if use_logos else None
        brief = compose_tech_brief(data, rep_img, app_imgs, contact, university_logo, pium_logo)
        st.session_state.brief_image = brief
        st.session_state.pdf_bytes = make_pdf_bytes_from_image(brief)
        st.session_state.pptx_bytes = make_pptx_bytes(data, rep_img, app_imgs, contact, university_logo, pium_logo)

if st.session_state.data is None:
    st.info("왼쪽에서 정보를 입력하고 특허 PDF를 업로드한 뒤 'Tech Brief 생성'을 누르세요.")
else:
    col1, col2 = st.columns([1.15, 0.85], gap="large")
    with col1:
        st.subheader("Tech Brief 미리보기")
        st.image(st.session_state.brief_image, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("PDF 다운로드", st.session_state.pdf_bytes, "PIUM_Tech_Brief.pdf", "application/pdf", type="primary", use_container_width=True)
        with c2:
            st.download_button("PPTX 다운로드(수정용)", st.session_state.pptx_bytes, "PIUM_Tech_Brief.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", use_container_width=True)

    with col2:
        st.subheader("생성 텍스트 직접 수정")
        edited_json = st.text_area("JSON 수정 후 다시 렌더링할 수 있습니다.", value=json.dumps(st.session_state.data, ensure_ascii=False, indent=2), height=720)
        if st.button("수정 내용으로 다시 생성", use_container_width=True):
            try:
                edited = json.loads(edited_json)
            except Exception as e:
                st.error(f"JSON 형식 오류: {e}"); st.stop()
            edited["university"] = university; edited["department"] = department; edited["professor"] = professor
            contact = f"{org} {name} {position}  |  {phone}  |  {email}"
            rep_img = st.session_state.rep_img or extract_representative_drawing(st.session_state.pdf_path)
            university_logo = get_logo_image(university) if use_logos else None
            pium_logo = get_pium_logo_image() if use_logos else None
            brief = compose_tech_brief(edited, rep_img, st.session_state.app_imgs, contact, university_logo, pium_logo)
            st.session_state.data = edited
            st.session_state.brief_image = brief
            st.session_state.pdf_bytes = make_pdf_bytes_from_image(brief)
            st.session_state.pptx_bytes = make_pptx_bytes(edited, rep_img, st.session_state.app_imgs, contact, university_logo, pium_logo)
            st.success("수정 내용이 반영되었습니다.")
            st.rerun()

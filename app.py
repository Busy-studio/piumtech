import os
import re
import json
import base64
import tempfile
from io import BytesIO
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn

# =====================================================
# 1. 기본 설정
# =====================================================

TEXT_MODEL_FIXED = "gpt-4.1-mini"
IMAGE_MODEL_FIXED = "gpt-image-1"

UNIVERSITY_OPTIONS = [
    "부산대학교",
    "국립부경대학교",
    "국립한국해양대학교",
    "동아대학교",
    "동의대학교",
    "동서대학교",
    "동명대학교",
    "신라대학교",
    "울산대학교",
    "경남대학교",
    "경상대학교",
    "창원대학교",
    "인제대학교",
    "수기입력",
]

st.set_page_config(
    page_title="PIUM SMK 생성기",
    page_icon="📄",
    layout="wide",
)

# =====================================================
# 2. OpenAI Client
# =====================================================

@st.cache_resource
def get_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    api_key = str(api_key).strip().replace('"', '').replace("'", "")

    if not api_key.startswith("sk-"):
        st.error("OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Secrets에 API 키를 입력하세요.")
        st.stop()

    return OpenAI(api_key=api_key)

# =====================================================
# 3. 한글 폰트
# =====================================================

@st.cache_resource
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    font_paths = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
        "/Library/Fonts/AppleGothic.ttf",
        "C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf",
    ]

    for path in font_paths:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()

# =====================================================
# 4. PDF 처리
# =====================================================

def save_uploaded_file(uploaded_file) -> str:
    suffix = os.path.splitext(uploaded_file.name)[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name


def extract_patent_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    texts = []
    for page in doc:
        texts.append(page.get_text("text"))
    doc.close()
    return "\n".join(texts)[:50000]


def extract_representative_drawing(pdf_path: str) -> Image.Image:
    doc = fitz.open(pdf_path)

    candidate_pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        if any(k in text for k in ["대표도", "대표 도", "도면1", "도 1"]):
            candidate_pages.append(i)

    page_idx = candidate_pages[-1] if candidate_pages else max(len(doc) - 1, 0)
    page = doc[page_idx]
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()
    return img

# =====================================================
# 5. GPT 분석
# =====================================================

def safe_json_parse(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```json", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"^```", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)

    return json.loads(text)


def analyze_patent_with_gpt(patent_text: str, university_name: str, department: str, professor: str) -> Dict[str, Any]:
    client = get_client()

    prompt = f"""
너는 대학 기술마케팅자료(SMK) 작성 전문가다.
아래 특허 명세서를 바탕으로 1페이지 SMK에 들어갈 내용을 생성하라.

작성 기준:
- 해당 분야 4년제 대학 졸업자가 이해할 수 있는 수준
- 일반 홍보문구가 아니라 기술이전용 SMK 문체
- 짧은 개조식
- 과장 금지
- 기술명은 마케팅용 제목으로 자연스럽게 정리
- 적용분야 제품은 3개
- 기술개요는 3개
- 기존기술 한계는 2개
- 대상기술 차별성은 2개
- 기술적 한계는 기존기술 한계와 겹치더라도 별도 요약 2개로 반드시 작성
- 기술적 우위는 2개
- 등록특허공보인 경우 출원번호, 등록번호, 출원일자, 등록일자를 모두 추출
- 번호/일자를 모르면 빈칸이 아니라 명세서에 있는 가장 가까운 정보를 추출
- 출력은 JSON만

JSON 형식:
{{
  "marketing_title": "",
  "subtitle": "",
  "original_title": "",
  "university": "",
  "department": "",
  "professor": "",
  "applications": [
    {{"name": "", "description": ""}},
    {{"name": "", "description": ""}},
    {{"name": "", "description": ""}}
  ],
  "overview": ["", "", ""],
  "limitations": ["", ""],
  "differentiation": ["", ""],
  "technical_limitations": ["", ""],
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

대학교: {university_name}
학과: {department}
교수명: {professor}

특허 명세서:
{patent_text}
"""

    res = client.responses.create(
        model=TEXT_MODEL_FIXED,
        input=prompt,
        temperature=0.2,
    )

    data = safe_json_parse(res.output_text)
    data["university"] = university_name
    data["department"] = department
    data["professor"] = professor
    return normalize_data(data)


def normalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
    data.setdefault("marketing_title", "")
    data.setdefault("subtitle", "")
    data.setdefault("original_title", "")
    data.setdefault("university", "")
    data.setdefault("department", "")
    data.setdefault("professor", "")
    data.setdefault("applications", [])
    data.setdefault("overview", [])
    data.setdefault("limitations", [])
    data.setdefault("differentiation", [])
    data.setdefault("technical_limitations", data.get("limitations", []))
    data.setdefault("technical_advantages", [])
    data.setdefault("ip", {})

    ip = data["ip"]
    if "number" in ip and not ip.get("application_number") and not ip.get("registration_number"):
        # 이전 버전 JSON 호환
        ip["registration_number"] = ip.get("number", "")
    if "date" in ip and not ip.get("application_date") and not ip.get("registration_date"):
        ip["registration_date"] = ip.get("date", "")

    ip.setdefault("title", data.get("original_title", ""))
    ip.setdefault("application_number", "")
    ip.setdefault("registration_number", "")
    ip.setdefault("application_date", "")
    ip.setdefault("registration_date", "")
    ip.setdefault("applicant", "")
    return data

# =====================================================
# 6. 적용분야 이미지 생성
# =====================================================

def generate_application_image(title: str, desc: str) -> Image.Image:
    client = get_client()

    prompt = f"""
Create a premium technology-transfer brochure illustration for a Korean university SMK sheet.

Application field: {title}
Technical context: {desc}

Visual direction:
- realistic, futuristic, clean color flat-icon hybrid
- product-oriented technology marketing visual, not textbook illustration
- slightly three-dimensional flat icon / isometric product render feel
- crisp edges, modern materials, subtle lighting, high-tech atmosphere
- transparent-background feel on pure white background
- centered object, enough white margin
- no Korean text, no English text, no logo, no watermark
- suitable for a public institution technology brochure
"""

    result = client.images.generate(
        model=IMAGE_MODEL_FIXED,
        prompt=prompt,
        size="1024x1024",
    )

    img_b64 = result.data[0].b64_json
    return Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")

# =====================================================
# 7. 그리기 유틸
# =====================================================

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    lines = []
    for raw in str(text).split("\n"):
        raw = raw.strip()
        if not raw:
            lines.append("")
            continue

        line = ""
        for ch in list(raw):
            test = line + ch
            width = draw.textbbox((0, 0), test, font=font)[2]
            if width <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = ch
        if line:
            lines.append(line)
    return lines


def draw_wrapped(draw, xy, text, font, fill, max_width, line_gap=8):
    x, y = xy
    for line in wrap_text(draw, text, font, max_width):
        draw.text((x, y), line, font=font, fill=fill)
        y += getattr(font, "size", 18) + line_gap
    return y


def fit_image(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    img = img.copy().convert("RGB")
    img.thumbnail(size)
    canvas = Image.new("RGB", size, "white")
    x = (size[0] - img.width) // 2
    y = (size[1] - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


def draw_section_label(d, text: str, y: int, font, blue):
    # 오른쪽 콘텐츠 상단과 최대한 맞춰 보이도록 기준선 정렬
    d.line((55, y - 30, 110, y - 30), fill=blue, width=5)
    d.text((50, y), text, font=font, fill=blue, spacing=6)


def ip_number_text(ip: Dict[str, Any]) -> str:
    app = ip.get("application_number", "")
    reg = ip.get("registration_number", "")
    if app and reg:
        return f"{app}\n({reg})"
    return app or reg


def ip_date_text(ip: Dict[str, Any]) -> str:
    app = ip.get("application_date", "")
    reg = ip.get("registration_date", "")
    if app and reg:
        return f"{app}\n({reg})"
    return app or reg

# =====================================================
# 8. SMK 1페이지 이미지 생성
# =====================================================

def compose_smk(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str) -> Image.Image:
    data = normalize_data(data)
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    navy = (0, 52, 140)
    blue = (24, 90, 180)
    light = (235, 240, 247)
    mint = (0, 170, 160)
    gray = (90, 90, 90)
    black = (30, 30, 30)
    line_gray = (190, 190, 190)

    f_logo = load_font(38, True)
    f_title = load_font(48, True)
    f_sub = load_font(25, False)
    f_h = load_font(30, True)
    f_m = load_font(23, True)
    f_b = load_font(21, False)
    f_s = load_font(18, False)
    f_xs = load_font(16, False)

    # 좌표 기준
    label_x = 50
    content_x = 190
    content_w = 930
    y_app = 320
    y_overview = 590
    y_comp = 900
    y_ip = 1425
    y_contact = 1640

    # Header
    d.rectangle((0, 0, W, 260), fill=light)
    d.rectangle((0, 0, 150, 150), fill=navy)
    d.text((32, 52), "PIUM", font=load_font(30, True), fill="white")

    header_line = f"PIUM Tech Offer  x  {data.get('university', '')} {data.get('department', '')}, {data.get('professor', '')} 교수"
    d.text((180, 42), header_line, font=f_sub, fill=navy)

    draw_wrapped(d, (180, 88), data.get("marketing_title", "기술명"), f_title, navy, 950, 6)
    d.text((180, 215), data.get("subtitle", ""), font=f_sub, fill=gray)

    # Applications
    draw_section_label(d, "적용\n분야\n제품", y_app + 18, f_h, navy)
    apps = data.get("applications", [])[:3]
    xs = [270, 545, 820]
    for i, app in enumerate(apps):
        x = xs[i]
        d.rounded_rectangle((x, y_app, x + 210, y_app + 200), radius=30, outline=blue, width=4)
        if i < len(app_imgs):
            icon = fit_image(app_imgs[i], (138, 112))
            img.paste(icon, (x + 36, y_app + 18))
        draw_wrapped(d, (x + 22, y_app + 138), app.get("name", ""), f_xs, black, 166, 4)
        if i < 2:
            d.line((x + 220, y_app + 100, x + 260, y_app + 100), fill=(150, 190, 230), width=4)

    # Overview
    draw_section_label(d, "기술\n개요", y_overview + 28, f_h, navy)
    rep = fit_image(rep_img, (260, 210))
    img.paste(rep, (230, y_overview + 15))

    y = y_overview + 35
    for item in data.get("overview", [])[:3]:
        y = draw_wrapped(d, (550, y), "› " + item, f_b, black, 600, 8) + 8

    # Competitiveness
    draw_section_label(d, "기술\n경쟁력", y_comp + 40, f_h, navy)
    d.rounded_rectangle((content_x, y_comp, 1120, y_comp + 130), radius=60, fill=(238, 248, 252))
    d.rounded_rectangle((230, y_comp + 30, 390, y_comp + 90), radius=30, outline=mint, width=4)
    d.text((262, y_comp + 46), "기존기술", font=f_m, fill=black)
    d.text((542, y_comp + 45), "▶  기술 차별성  ▶", font=f_m, fill=blue)
    d.rounded_rectangle((940, y_comp + 30, 1100, y_comp + 90), radius=30, outline=blue, width=4)
    d.text((972, y_comp + 46), "대상기술", font=f_m, fill=black)
    d.line((620, y_comp + 155, 620, y_comp + 440), fill=(210, 210, 210), width=2)

    # 상단 bullets
    y1 = y_comp + 165
    for item in data.get("limitations", [])[:2]:
        y1 = draw_wrapped(d, (210, y1), "● " + item, f_b, black, 380, 8) + 20

    y2 = y_comp + 165
    for item in data.get("differentiation", [])[:2]:
        y2 = draw_wrapped(d, (660, y2), "● " + item, f_b, black, 420, 8) + 20

    # 하단 세부 내용
    tag_y = y_comp + 330
    d.rectangle((210, tag_y, 370, tag_y + 40), fill=(174, 220, 213))
    d.text((225, tag_y + 8), "기술적 한계", font=f_s, fill="white")
    d.rectangle((660, tag_y, 820, tag_y + 40), fill=(77, 145, 200))
    d.text((675, tag_y + 8), "기술적 우위", font=f_s, fill="white")

    y = tag_y + 55
    tech_limits = data.get("technical_limitations") or data.get("limitations", [])
    for item in tech_limits[:2]:
        y = draw_wrapped(d, (210, y), "▸ " + item, f_xs, black, 380, 5) + 2

    y = tag_y + 55
    for item in data.get("technical_advantages", [])[:2]:
        y = draw_wrapped(d, (660, y), "▸ " + item, f_xs, navy, 430, 5) + 2

    # IP table
    draw_section_label(d, "지식\n재산권\n현황", y_ip + 25, f_h, navy)
    ip = data.get("ip", {})
    d.rectangle((content_x, y_ip, 1120, y_ip + 70), fill=(235, 235, 235))

    cols = [content_x, 500, 780, 1120]
    headers = ["발명의 명칭", "출원번호\n(등록번호)", "출원일자\n(등록일자)"]
    for j in range(3):
        d.rectangle((cols[j], y_ip, cols[j + 1], y_ip + 70), outline=line_gray)
        draw_wrapped(d, (cols[j] + 45, y_ip + 15), headers[j], f_m, black, cols[j + 1] - cols[j] - 90, 2)

    d.rectangle((content_x, y_ip + 70, 1120, y_ip + 170), outline=line_gray)
    for c in cols[1:-1]:
        d.line((c, y_ip + 70, c, y_ip + 170), fill=line_gray, width=2)

    draw_wrapped(d, (205, y_ip + 86), ip.get("title", data.get("original_title", "")), f_xs, black, 280, 4)
    draw_wrapped(d, (520, y_ip + 88), ip_number_text(ip), f_b, black, 240, 5)
    draw_wrapped(d, (800, y_ip + 88), ip_date_text(ip), f_b, black, 280, 5)

    # Contact
    draw_section_label(d, "문의처", y_contact, f_h, navy)
    d.text((content_x, y_contact + 8), contact, font=f_b, fill=black)

    return img

# =====================================================
# 9. PDF / PPTX 저장
# =====================================================

def make_pdf_bytes_from_image(img: Image.Image) -> bytes:
    a4_w, a4_h = 1240, 1754
    page = Image.new("RGB", (a4_w, a4_h), "white")
    img = img.convert("RGB")
    img.thumbnail((a4_w, a4_h))
    x = (a4_w - img.width) // 2
    y = (a4_h - img.height) // 2
    page.paste(img, (x, y))

    bio = BytesIO()
    page.save(bio, "PDF", resolution=150.0)
    return bio.getvalue()


def pil_to_stream(img: Image.Image, fmt: str = "PNG") -> BytesIO:
    bio = BytesIO()
    img.convert("RGB").save(bio, format=fmt)
    bio.seek(0)
    return bio


def make_pptx_bytes(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str) -> bytes:
    """수정 가능한 텍스트 객체 중심 PPTX 생성. 이미지와 도형은 별도 객체로 삽입."""
    data = normalize_data(data)
    prs = Presentation()
    prs.slide_width = Inches(8.27)   # A4 portrait width
    prs.slide_height = Inches(11.69) # A4 portrait height
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    W, H = 1240, 1754
    SW, SH = 8.27, 11.69

    def X(px): return Inches(px / W * SW)
    def Y(px): return Inches(px / H * SH)
    def CX(px): return Inches(px / W * SW)
    def CY(px): return Inches(px / H * SH)

    navy = RGBColor(0, 52, 140)
    blue = RGBColor(24, 90, 180)
    light = RGBColor(235, 240, 247)
    mint = RGBColor(0, 170, 160)
    black = RGBColor(30, 30, 30)
    gray = RGBColor(90, 90, 90)
    line_gray = RGBColor(190, 190, 190)
    pale = RGBColor(238, 248, 252)
    tag_mint = RGBColor(174, 220, 213)
    tag_blue = RGBColor(77, 145, 200)

    def set_font(run, size, color=black, bold=False):
        run.font.name = "Malgun Gothic"
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        try:
            run._element.rPr.rFonts.set(qn("a:ea"), "Malgun Gothic")
        except Exception:
            pass

    def textbox(px, py, pw, ph, text, size=12, color=black, bold=False, align=None, fill=None):
        shape = slide.shapes.add_textbox(X(px), Y(py), CX(pw), CY(ph))
        tf = shape.text_frame
        tf.clear()
        tf.word_wrap = True
        tf.margin_left = Pt(0)
        tf.margin_right = Pt(0)
        tf.margin_top = Pt(0)
        tf.margin_bottom = Pt(0)
        p = tf.paragraphs[0]
        if align is not None:
            p.alignment = align
        run = p.add_run()
        run.text = str(text or "")
        set_font(run, size, color, bold)
        if fill:
            shape.fill.solid()
            shape.fill.fore_color.rgb = fill
        return shape

    def rect(px, py, pw, ph, fill=None, line=None, radius=False, width=1):
        shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, X(px), Y(py), CX(pw), CY(ph))
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = fill
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = line
            shp.line.width = Pt(width)
        else:
            shp.line.fill.background()
        return shp

    def line(px1, py1, px2, py2, color=blue, width=2):
        shp = slide.shapes.add_connector(1, X(px1), Y(py1), X(px2), Y(py2))
        shp.line.color.rgb = color
        shp.line.width = Pt(width)
        return shp

    def picture(img, px, py, pw, ph):
        stream = pil_to_stream(fit_image(img, (max(1, pw), max(1, ph))))
        slide.shapes.add_picture(stream, X(px), Y(py), width=CX(pw), height=CY(ph))

    def section_label(text, py):
        line(55, py - 30, 110, py - 30, blue, 3)
        textbox(50, py, 90, 110, text, size=17, color=navy, bold=True)

    # Header
    rect(0, 0, W, 260, fill=light)
    rect(0, 0, 150, 150, fill=navy)
    textbox(30, 52, 90, 45, "PIUM", size=21, color=RGBColor(255, 255, 255), bold=True)
    header_line = f"PIUM Tech Offer  x  {data.get('university', '')} {data.get('department', '')}, {data.get('professor', '')} 교수"
    textbox(180, 42, 950, 35, header_line, size=14, color=navy)
    textbox(180, 88, 950, 110, data.get("marketing_title", "기술명"), size=28, color=navy, bold=True)
    textbox(180, 215, 950, 35, data.get("subtitle", ""), size=14, color=gray)

    # Applications
    y_app = 320
    section_label("적용\n분야\n제품", y_app + 18)
    xs = [270, 545, 820]
    for i, app in enumerate(data.get("applications", [])[:3]):
        x = xs[i]
        rect(x, y_app, 210, 200, fill=None, line=blue, radius=True, width=2)
        if i < len(app_imgs):
            picture(app_imgs[i], x + 36, y_app + 18, 138, 112)
        textbox(x + 22, y_app + 138, 166, 45, app.get("name", ""), size=9, color=black, align=PP_ALIGN.CENTER)
        if i < 2:
            line(x + 220, y_app + 100, x + 260, y_app + 100, RGBColor(150, 190, 230), 2)

    # Overview
    y_overview = 590
    section_label("기술\n개요", y_overview + 28)
    picture(rep_img, 230, y_overview + 15, 260, 210)
    y = y_overview + 35
    for item in data.get("overview", [])[:3]:
        textbox(550, y, 600, 52, "› " + item, size=12, color=black)
        y += 62

    # Competitiveness
    y_comp = 900
    section_label("기술\n경쟁력", y_comp + 40)
    rect(190, y_comp, 930, 130, fill=pale, radius=True)
    rect(230, y_comp + 30, 160, 60, fill=None, line=RGBColor(0, 170, 160), radius=True, width=2)
    textbox(262, y_comp + 45, 100, 30, "기존기술", size=13, color=black, bold=True, align=PP_ALIGN.CENTER)
    textbox(542, y_comp + 45, 210, 30, "▶  기술 차별성  ▶", size=13, color=blue, bold=True, align=PP_ALIGN.CENTER)
    rect(940, y_comp + 30, 160, 60, fill=None, line=blue, radius=True, width=2)
    textbox(972, y_comp + 45, 100, 30, "대상기술", size=13, color=black, bold=True, align=PP_ALIGN.CENTER)
    line(620, y_comp + 155, 620, y_comp + 440, RGBColor(210, 210, 210), 1)

    y = y_comp + 165
    for item in data.get("limitations", [])[:2]:
        textbox(210, y, 380, 60, "● " + item, size=11, color=black)
        y += 78

    y = y_comp + 165
    for item in data.get("differentiation", [])[:2]:
        textbox(660, y, 420, 60, "● " + item, size=11, color=black)
        y += 78

    tag_y = y_comp + 330
    rect(210, tag_y, 160, 40, fill=tag_mint)
    textbox(225, tag_y + 8, 120, 24, "기술적 한계", size=10, color=RGBColor(255, 255, 255))
    rect(660, tag_y, 160, 40, fill=tag_blue)
    textbox(675, tag_y + 8, 120, 24, "기술적 우위", size=10, color=RGBColor(255, 255, 255))

    y = tag_y + 55
    tech_limits = data.get("technical_limitations") or data.get("limitations", [])
    for item in tech_limits[:2]:
        textbox(210, y, 380, 38, "▸ " + item, size=9, color=black)
        y += 44

    y = tag_y + 55
    for item in data.get("technical_advantages", [])[:2]:
        textbox(660, y, 430, 38, "▸ " + item, size=9, color=navy)
        y += 44

    # IP table
    y_ip = 1425
    section_label("지식\n재산권\n현황", y_ip + 25)
    ip = data.get("ip", {})
    cols = [190, 500, 780, 1120]
    headers = ["발명의 명칭", "출원번호\n(등록번호)", "출원일자\n(등록일자)"]
    for j in range(3):
        rect(cols[j], y_ip, cols[j + 1] - cols[j], 70, fill=RGBColor(235, 235, 235), line=line_gray)
        textbox(cols[j] + 28, y_ip + 14, cols[j + 1] - cols[j] - 56, 45, headers[j], size=12, color=black, bold=True, align=PP_ALIGN.CENTER)
        rect(cols[j], y_ip + 70, cols[j + 1] - cols[j], 100, fill=None, line=line_gray)
    textbox(205, y_ip + 86, 280, 78, ip.get("title", data.get("original_title", "")), size=9, color=black)
    textbox(520, y_ip + 88, 240, 78, ip_number_text(ip), size=12, color=black)
    textbox(800, y_ip + 88, 280, 78, ip_date_text(ip), size=12, color=black)

    # Contact
    y_contact = 1640
    section_label("문의처", y_contact)
    textbox(190, y_contact + 8, 930, 40, contact, size=12, color=black)

    bio = BytesIO()
    prs.save(bio)
    return bio.getvalue()

# =====================================================
# 10. Streamlit UI
# =====================================================

st.title("PIUM Tech Offer SMK 생성기")
st.caption("특허 명세서 PDF를 업로드하면 GPT가 SMK 내용을 생성하고, 최종 PDF와 수정 가능한 PPTX를 다운로드합니다.")

with st.sidebar:
    st.header("입력 정보")
    uploaded_pdf = st.file_uploader("특허 명세서 PDF 업로드", type=["pdf"])

    university_choice = st.selectbox("대학교", UNIVERSITY_OPTIONS, index=0)
    if university_choice == "수기입력":
        university_name = st.text_input("대학교명 수기입력", placeholder="예: ○○대학교")
    else:
        university_name = university_choice

    department = st.text_input("소속/학과", placeholder="예: 사회환경시스템공학과")
    professor = st.text_input("교수명", placeholder="예: 김원국")

    st.divider()
    st.subheader("문의처")
    org = st.text_input("소속", placeholder="예: 부산대학교 산학협력단")
    name = st.text_input("이름", placeholder="예: 윤재철")
    position = st.text_input("직책", placeholder="예: 차장")
    phone = st.text_input("연락처", placeholder="예: 051.510.2741")
    email = st.text_input("이메일", placeholder="예: example@pusan.ac.kr")

    generate_btn = st.button("SMK 생성", type="primary", use_container_width=True)

for key, default in {
    "smk_data": None,
    "smk_image": None,
    "pdf_bytes": None,
    "pptx_bytes": None,
    "pdf_path": None,
    "rep_img": None,
    "app_imgs": [],
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

if generate_btn:
    if uploaded_pdf is None:
        st.error("특허 명세서 PDF를 업로드하세요.")
        st.stop()
    if not university_name:
        st.error("대학교명을 선택 또는 입력하세요.")
        st.stop()

    with st.spinner("PDF 분석 중..."):
        pdf_path = save_uploaded_file(uploaded_pdf)
        patent_text = extract_patent_text(pdf_path)
        rep_img = extract_representative_drawing(pdf_path)
        st.session_state.pdf_path = pdf_path
        st.session_state.rep_img = rep_img

    with st.spinner("GPT로 SMK 텍스트 생성 중..."):
        data = analyze_patent_with_gpt(patent_text, university_name, department, professor)
        st.session_state.smk_data = data

    app_imgs = []
    with st.spinner("적용분야 이미지 생성 중..."):
        for app in data.get("applications", [])[:3]:
            try:
                app_imgs.append(generate_application_image(app.get("name", ""), app.get("description", "")))
            except Exception as e:
                st.warning(f"적용분야 이미지 생성 실패: {e}")
                app_imgs.append(Image.new("RGB", (1024, 1024), "white"))
        st.session_state.app_imgs = app_imgs

    with st.spinner("SMK 페이지 구성 중..."):
        contact = f"{org} {name} {position}   |   {phone}   |   {email}"
        smk_img = compose_smk(data, rep_img, app_imgs, contact)
        pdf_bytes = make_pdf_bytes_from_image(smk_img)
        pptx_bytes = make_pptx_bytes(data, rep_img, app_imgs, contact)
        st.session_state.smk_image = smk_img
        st.session_state.pdf_bytes = pdf_bytes
        st.session_state.pptx_bytes = pptx_bytes

if st.session_state.smk_data is None:
    st.info("왼쪽 사이드바에서 정보를 입력하고 특허 PDF를 업로드한 뒤 'SMK 생성'을 누르세요.")
else:
    col1, col2 = st.columns([1.1, 0.9], gap="large")

    with col1:
        st.subheader("SMK 미리보기")
        st.image(st.session_state.smk_image, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                label="PDF 다운로드",
                data=st.session_state.pdf_bytes,
                file_name="PIUM_Tech_Offer_SMK.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True,
            )
        with c2:
            st.download_button(
                label="PPTX 다운로드(수정용)",
                data=st.session_state.pptx_bytes,
                file_name="PIUM_Tech_Offer_SMK_editable.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                use_container_width=True,
            )

    with col2:
        st.subheader("생성 텍스트 직접 수정")
        edited_json = st.text_area(
            "JSON 수정 후 아래 버튼을 누르면 수정 내용으로 PDF/PPTX가 다시 렌더링됩니다.",
            value=json.dumps(st.session_state.smk_data, ensure_ascii=False, indent=2),
            height=680,
        )

        if st.button("수정 내용으로 다시 생성", use_container_width=True):
            if not st.session_state.pdf_path:
                st.error("원본 PDF 경로가 없습니다. 다시 업로드 후 생성하세요.")
                st.stop()

            try:
                edited_data = normalize_data(json.loads(edited_json))
            except Exception as e:
                st.error(f"JSON 형식 오류: {e}")
                st.stop()

            edited_data["university"] = university_name
            edited_data["department"] = department
            edited_data["professor"] = professor

            rep_img = st.session_state.rep_img or extract_representative_drawing(st.session_state.pdf_path)
            contact = f"{org} {name} {position}   |   {phone}   |   {email}"

            smk_img = compose_smk(edited_data, rep_img, st.session_state.app_imgs, contact)
            pdf_bytes = make_pdf_bytes_from_image(smk_img)
            pptx_bytes = make_pptx_bytes(edited_data, rep_img, st.session_state.app_imgs, contact)

            st.session_state.smk_data = edited_data
            st.session_state.smk_image = smk_img
            st.session_state.pdf_bytes = pdf_bytes
            st.session_state.pptx_bytes = pptx_bytes

            st.success("수정 내용이 반영되었습니다. PDF/PPTX를 다운로드하세요.")
            st.rerun()

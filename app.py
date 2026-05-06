import os
import re
import json
import base64
import tempfile
from io import BytesIO
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI

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
    page_title="PIUM Tech Offer SMK 생성기",
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
    return "\n".join(texts)[:60000]


def extract_representative_drawing(pdf_path: str) -> Image.Image:
    doc = fitz.open(pdf_path)

    candidate_pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        if "대표도" in text or "대표 도" in text or "도면1" in text or "도 1" in text:
            candidate_pages.append(i)

    page_idx = candidate_pages[-1] if candidate_pages else max(len(doc) - 1, 0)
    page = doc[page_idx]
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()
    return img


def regex_ip_fallback(patent_text: str) -> Dict[str, str]:
    """GPT가 놓친 출원/등록 정보를 명세서 텍스트에서 보강."""
    def find(pattern: str) -> str:
        m = re.search(pattern, patent_text)
        return m.group(1).strip() if m else ""

    title = find(r"\(54\)\s*발명의 명칭\s*([^\n]+)") or find(r"발명의 명칭\s*([^\n]+)")
    app_no = find(r"\(21\)\s*출원번호\s*([0-9\-]+)")
    app_date = find(r"\(22\)\s*출원일자\s*([0-9년월일\.\- ]+)")
    reg_no = find(r"\(11\)\s*등록번호\s*([0-9\-]+)")
    reg_date = find(r"\(24\)\s*등록일자\s*([0-9년월일\.\- ]+)")
    applicant = find(r"\(73\)\s*특허권자\s*([^\n]+)")

    return {
        "title": title,
        "application_number": app_no,
        "registration_number": reg_no,
        "application_date": app_date,
        "registration_date": reg_date,
        "applicant": applicant,
    }


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


def normalize_ip_fields(data: Dict[str, Any], patent_text: str) -> Dict[str, Any]:
    fallback = regex_ip_fallback(patent_text)
    ip = data.get("ip", {}) or {}

    # 구버전 number/date가 있으면 등록번호/등록일자로 임시 매핑
    if ip.get("number") and not ip.get("registration_number"):
        ip["registration_number"] = ip.get("number", "")
    if ip.get("date") and not ip.get("registration_date"):
        ip["registration_date"] = ip.get("date", "")

    for key, val in fallback.items():
        if not ip.get(key) and val:
            ip[key] = val

    ip.setdefault("title", data.get("original_title", ""))
    ip.setdefault("application_number", "")
    ip.setdefault("registration_number", "")
    ip.setdefault("application_date", "")
    ip.setdefault("registration_date", "")
    ip.setdefault("applicant", "")
    data["ip"] = ip
    return data


def analyze_patent_with_gpt(patent_text: str, university: str, department: str, professor: str) -> Dict[str, Any]:
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
- 적용분야 제품은 실제 산업 적용 제품/서비스 관점으로 3개
- 기술개요는 3개
- 기존기술 한계는 반드시 2개 작성
- 대상기술 차별성은 반드시 2개 작성
- 기술적 한계는 기존기술 한계를 더 구체화한 형태로 반드시 2개 작성
- 기술적 우위는 반드시 2개 작성
- 등록공보에 출원번호/등록번호/출원일자/등록일자가 있으면 모두 추출
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

대학교: {university}
학과/소속: {department}
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
    return normalize_ip_fields(data, patent_text)


# =====================================================
# 6. 적용분야 이미지 생성
# =====================================================

def generate_application_image(title: str, desc: str) -> Image.Image:
    client = get_client()

    prompt = f"""
Create a futuristic, realistic, premium flat-icon based technology illustration for a university SMK technology brochure.

Application field: {title}
Application description: {desc}

Visual direction:
- realistic but icon-like premium flat illustration
- futuristic industrial technology mood
- clean colored object illustration, not textbook style
- white or transparent-looking background
- soft depth, subtle 3D isometric feel
- polished Korean public-institution technology brochure design
- no text, no letters, no logo, no watermark
- centered single application scene or product object
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


def draw_wrapped(draw, xy, text, font, fill, max_width, line_gap=8, max_lines: Optional[int] = None):
    x, y = xy
    lines = wrap_text(draw, text, font, max_width)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            lines[-1] = lines[-1].rstrip("…") + "…"
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += getattr(font, "size", 18) + line_gap
    return y


def fit_image(img: Image.Image, size: tuple[int, int], bg: str = "white") -> Image.Image:
    img = img.copy().convert("RGB")
    img.thumbnail(size)
    canvas = Image.new("RGB", size, bg)
    x = (size[0] - img.width) // 2
    y = (size[1] - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


def draw_section_label(d: ImageDraw.ImageDraw, x: int, y: int, label: str, font, color, line_color):
    d.line((x, y, x + 55, y), fill=line_color, width=5)
    d.text((x, y + 34), label, font=font, fill=color, spacing=5)


def draw_bullet_list(d, x, y, items, font, fill, max_width, bullet="●", gap=16, max_lines_per_item=3):
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        d.text((x, y), bullet, font=font, fill=fill)
        y = draw_wrapped(d, (x + 28, y), item, font, fill, max_width - 28, 6, max_lines=max_lines_per_item) + gap
    return y


def get_ip_number_text(ip: Dict[str, Any]) -> str:
    app_no = str(ip.get("application_number", "")).strip()
    reg_no = str(ip.get("registration_number", "")).strip()
    if app_no and reg_no:
        return f"{app_no}\n({reg_no})"
    return app_no or (f"({reg_no})" if reg_no else "")


def get_ip_date_text(ip: Dict[str, Any]) -> str:
    app_date = str(ip.get("application_date", "")).strip()
    reg_date = str(ip.get("registration_date", "")).strip()
    if app_date and reg_date:
        return f"{app_date}\n({reg_date})"
    return app_date or (f"({reg_date})" if reg_date else "")


# =====================================================
# 8. SMK 1페이지 이미지 생성
# =====================================================

def compose_smk(
    data: Dict[str, Any],
    rep_img: Image.Image,
    app_imgs: List[Image.Image],
    contact: str,
    university: str,
) -> Image.Image:
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    navy = (0, 52, 140)
    blue = (24, 90, 180)
    light = (235, 240, 247)
    mint = (0, 170, 160)
    gray = (88, 88, 88)
    black = (32, 32, 32)
    line_gray = (190, 190, 190)
    soft_blue = (235, 247, 252)

    f_logo = load_font(40, True)
    f_top = load_font(27, False)
    f_title = load_font(48, True)
    f_sub = load_font(25, False)
    f_label = load_font(29, True)
    f_m = load_font(23, True)
    f_b = load_font(21, False)
    f_s = load_font(18, False)
    f_xs = load_font(16, False)

    label_x = 54
    content_x = 190
    content_w = 930

    # Header
    d.rectangle((0, 0, W, 260), fill=light)
    d.rectangle((0, 0, 150, 150), fill=navy)
    d.text((30, 52), "PIUM", font=f_logo, fill="white")

    header_meta = f"PIUM Tech Offer  x  {university} {data.get('department', '')}, {data.get('professor', '')} 교수"
    d.text((180, 42), header_meta, font=f_top, fill=navy)
    draw_wrapped(d, (180, 86), data.get("marketing_title", "기술명"), f_title, navy, 950, 5, max_lines=2)
    d.text((180, 215), data.get("subtitle", ""), font=f_sub, fill=gray)

    # Section Y coordinates: 좌측 라벨과 우측 콘텐츠 시작점을 맞춤
    y_app = 320
    y_overview = 600
    y_comp = 890
    y_ip = 1455
    y_contact = 1645

    draw_section_label(d, label_x, y_app - 25, "적용\n분야\n제품", f_label, navy, blue)
    draw_section_label(d, label_x, y_overview - 25, "기술\n개요", f_label, navy, blue)
    draw_section_label(d, label_x, y_comp - 25, "기술\n경쟁력", f_label, navy, blue)
    draw_section_label(d, label_x, y_ip - 25, "지식\n재산권\n현황", f_label, navy, blue)
    draw_section_label(d, label_x, y_contact - 25, "문의처", f_label, navy, blue)

    # Applications
    apps = data.get("applications", [])[:3]
    card_w, card_h = 215, 200
    gap = 65
    xs = [content_x + 80, content_x + 80 + card_w + gap, content_x + 80 + (card_w + gap) * 2]
    for i, app in enumerate(apps):
        x = xs[i]
        d.rounded_rectangle((x, y_app, x + card_w, y_app + card_h), radius=30, outline=blue, width=4)
        if i < len(app_imgs):
            icon = fit_image(app_imgs[i], (138, 110), bg="white")
            img.paste(icon, (x + 38, y_app + 18))
        draw_wrapped(d, (x + 22, y_app + 135), app.get("name", ""), f_xs, black, card_w - 44, 4, max_lines=2)
        if i < 2:
            d.line((x + card_w + 10, y_app + 100, x + card_w + 50, y_app + 100), fill=(150, 190, 230), width=4)

    # Overview
    rep = fit_image(rep_img, (270, 230))
    img.paste(rep, (content_x + 55, y_overview + 10))
    y = y_overview + 20
    for item in data.get("overview", [])[:3]:
        y = draw_wrapped(d, (content_x + 410, y), "〉 " + item, f_b, black, 560, 8, max_lines=2) + 8

    # Competitiveness redesigned block
    d.rounded_rectangle((content_x, y_comp, content_x + content_w, y_comp + 120), radius=55, fill=soft_blue)
    d.rounded_rectangle((content_x + 42, y_comp + 30, content_x + 205, y_comp + 88), radius=29, outline=mint, width=4)
    d.text((content_x + 77, y_comp + 46), "기존기술", font=f_m, fill=black)
    d.text((content_x + 365, y_comp + 44), "▶  기술 차별성  ▶", font=f_m, fill=blue)
    d.rounded_rectangle((content_x + 745, y_comp + 30, content_x + 905, y_comp + 88), radius=29, outline=blue, width=4)
    d.text((content_x + 775, y_comp + 46), "대상기술", font=f_m, fill=black)

    col_left_x = content_x + 25
    col_right_x = content_x + 500
    col_w = 395
    d.line((content_x + 465, y_comp + 155, content_x + 465, y_comp + 515), fill=(210, 210, 210), width=2)

    # 기존기술 한계
    y_left = y_comp + 150
    y_left = draw_bullet_list(d, col_left_x, y_left, data.get("limitations", [])[:2], f_b, black, col_w, bullet="●", gap=20, max_lines_per_item=3)

    d.rectangle((col_left_x, y_comp + 365, col_left_x + 165, y_comp + 405), fill=(174, 220, 213))
    d.text((col_left_x + 18, y_comp + 374), "기술적 한계", font=f_s, fill="white")
    tech_lims = data.get("technical_limitations") or data.get("limitations", [])
    draw_bullet_list(d, col_left_x, y_comp + 425, tech_lims[:2], f_s, black, col_w, bullet="▸", gap=10, max_lines_per_item=2)

    # 대상기술 차별성 / 우위
    y_right = y_comp + 150
    y_right = draw_bullet_list(d, col_right_x, y_right, data.get("differentiation", [])[:2], f_b, black, col_w, bullet="●", gap=20, max_lines_per_item=3)

    d.rectangle((col_right_x, y_comp + 365, col_right_x + 165, y_comp + 405), fill=(77, 145, 200))
    d.text((col_right_x + 18, y_comp + 374), "기술적 우위", font=f_s, fill="white")
    draw_bullet_list(d, col_right_x, y_comp + 425, data.get("technical_advantages", [])[:2], f_s, navy, col_w, bullet="▸", gap=10, max_lines_per_item=2)

    # IP table
    ip = data.get("ip", {})
    table_x = content_x
    table_y = y_ip
    table_w = content_w
    header_h = 70
    body_h = 100
    cols = [table_x, table_x + 330, table_x + 625, table_x + table_w]
    headers = ["발명의 명칭", "출원번호\n(등록번호)", "출원일자\n(등록일자)"]

    d.rectangle((table_x, table_y, table_x + table_w, table_y + header_h), fill=(235, 235, 235))
    for j in range(3):
        d.rectangle((cols[j], table_y, cols[j + 1], table_y + header_h), outline=line_gray)
        draw_wrapped(d, (cols[j] + 70, table_y + 12), headers[j], f_m, black, cols[j + 1] - cols[j] - 80, 2, max_lines=2)

    d.rectangle((table_x, table_y + header_h, table_x + table_w, table_y + header_h + body_h), outline=line_gray)
    for c in cols[1:-1]:
        d.line((c, table_y + header_h, c, table_y + header_h + body_h), fill=line_gray, width=2)

    draw_wrapped(d, (cols[0] + 20, table_y + header_h + 22), ip.get("title", data.get("original_title", "")), f_xs, black, 285, 4, max_lines=3)
    draw_wrapped(d, (cols[1] + 35, table_y + header_h + 24), get_ip_number_text(ip), f_b, black, 245, 6, max_lines=2)
    draw_wrapped(d, (cols[2] + 35, table_y + header_h + 24), get_ip_date_text(ip), f_b, black, 300, 6, max_lines=2)

    # Contact
    draw_wrapped(d, (content_x, y_contact + 30), contact, f_b, black, 900, 8, max_lines=2)

    return img


# =====================================================
# 9. PDF 저장: A4 세로 1페이지
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


# =====================================================
# 10. Streamlit UI
# =====================================================

st.title("PIUM Tech Offer SMK 생성기")
st.caption("특허 등록공보/명세서 PDF를 업로드하면 GPT가 SMK 내용을 생성하고, 최종 PDF를 A4 세로 1페이지로 다운로드합니다.")

with st.sidebar:
    st.header("입력 정보")
    uploaded_pdf = st.file_uploader("특허 명세서/등록공보 PDF 업로드", type=["pdf"])

    university_choice = st.selectbox("대학교 선택", UNIVERSITY_OPTIONS, index=0)
    custom_university = ""
    if university_choice == "수기입력":
        custom_university = st.text_input("대학교명 수기입력", placeholder="예: ○○대학교")
    university_name = custom_university.strip() if university_choice == "수기입력" else university_choice

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

if "smk_data" not in st.session_state:
    st.session_state.smk_data = None
if "smk_image" not in st.session_state:
    st.session_state.smk_image = None
if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
if "pdf_path" not in st.session_state:
    st.session_state.pdf_path = None
if "app_imgs" not in st.session_state:
    st.session_state.app_imgs = []
if "rep_img" not in st.session_state:
    st.session_state.rep_img = None
if "university_name" not in st.session_state:
    st.session_state.university_name = university_name

if generate_btn:
    if uploaded_pdf is None:
        st.error("특허 명세서/등록공보 PDF를 업로드하세요.")
        st.stop()
    if not university_name:
        st.error("대학교명을 선택하거나 수기입력하세요.")
        st.stop()

    with st.spinner("PDF 분석 중..."):
        pdf_path = save_uploaded_file(uploaded_pdf)
        patent_text = extract_patent_text(pdf_path)
        rep_img = extract_representative_drawing(pdf_path)
        st.session_state.pdf_path = pdf_path
        st.session_state.rep_img = rep_img
        st.session_state.university_name = university_name

    with st.spinner("GPT로 SMK 텍스트 생성 중..."):
        data = analyze_patent_with_gpt(patent_text, university_name, department, professor)
        data["university"] = university_name
        data["department"] = department
        data["professor"] = professor
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
        contact_parts = [org, name, position, phone, email]
        contact = "   |   ".join([p for p in contact_parts if str(p).strip()])
        smk_img = compose_smk(data, rep_img, app_imgs, contact, university_name)
        pdf_bytes = make_pdf_bytes_from_image(smk_img)
        st.session_state.smk_image = smk_img
        st.session_state.pdf_bytes = pdf_bytes

if st.session_state.smk_data is None:
    st.info("왼쪽 사이드바에서 정보를 입력하고 특허 PDF를 업로드한 뒤 'SMK 생성'을 누르세요.")
else:
    col1, col2 = st.columns([1.1, 0.9], gap="large")

    with col1:
        st.subheader("SMK 미리보기")
        st.image(st.session_state.smk_image, use_container_width=True)

        st.download_button(
            label="PDF 다운로드",
            data=st.session_state.pdf_bytes,
            file_name="PIUM_Tech_Offer_SMK.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )

    with col2:
        st.subheader("생성 텍스트 직접 수정")
        edited_json = st.text_area(
            "JSON 수정 후 아래 버튼을 누르면 수정 내용으로 다시 렌더링됩니다.",
            value=json.dumps(st.session_state.smk_data, ensure_ascii=False, indent=2),
            height=680,
        )

        if st.button("수정 내용으로 다시 생성", use_container_width=True):
            if not st.session_state.pdf_path:
                st.error("원본 PDF 경로가 없습니다. 다시 업로드 후 생성하세요.")
                st.stop()

            try:
                edited_data = json.loads(edited_json)
            except Exception as e:
                st.error(f"JSON 형식 오류: {e}")
                st.stop()

            edited_data["university"] = university_name
            edited_data["department"] = department
            edited_data["professor"] = professor

            rep_img = st.session_state.rep_img or extract_representative_drawing(st.session_state.pdf_path)
            contact_parts = [org, name, position, phone, email]
            contact = "   |   ".join([p for p in contact_parts if str(p).strip()])

            smk_img = compose_smk(
                edited_data,
                rep_img,
                st.session_state.app_imgs,
                contact,
                university_name,
            )
            pdf_bytes = make_pdf_bytes_from_image(smk_img)

            st.session_state.smk_data = edited_data
            st.session_state.smk_image = smk_img
            st.session_state.pdf_bytes = pdf_bytes

            st.success("수정 내용이 반영되었습니다. 화면을 새로고침하지 말고 PDF를 다운로드하세요.")
            st.rerun()

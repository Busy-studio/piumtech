import os
import re
import json
import base64
import tempfile
from io import BytesIO
from typing import Any, Dict, List

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI

# =====================================================
# 1. 기본 설정
# =====================================================

TEXT_MODEL_FIXED = "gpt-4.1-mini"
IMAGE_MODEL_FIXED = "gpt-image-1"

st.set_page_config(
    page_title="PNU SMK 생성기",
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

    # 최후 fallback: 한글은 깨질 수 있음
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
        if "대표도" in text or "대표 도" in text or "도면1" in text or "도 1" in text:
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


def analyze_patent_with_gpt(patent_text: str, department: str, professor: str) -> Dict[str, Any]:
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
- 기술적 우위는 2개
- 지식재산권 현황은 명세서에서 추출
- 출력은 JSON만

JSON 형식:
{{
  "marketing_title": "",
  "subtitle": "",
  "original_title": "",
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
  "technical_advantages": ["", ""],
  "ip": {{
    "title": "",
    "number": "",
    "date": "",
    "applicant": ""
  }}
}}

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

    return safe_json_parse(res.output_text)


# =====================================================
# 6. 적용분야 이미지 생성
# =====================================================

def generate_application_image(title: str, desc: str) -> Image.Image:
    client = get_client()

    prompt = f"""
Korean university technology marketing brochure illustration.

Application field:
{title}

Description:
{desc}

Style:
clean colored realistic technical illustration,
professional public-institution report style,
white background,
simple composition,
no text,
no logo,
no watermark.
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


def fit_image(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    img = img.copy().convert("RGB")
    img.thumbnail(size)
    canvas = Image.new("RGB", size, "white")
    x = (size[0] - img.width) // 2
    y = (size[1] - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


# =====================================================
# 8. SMK 1페이지 이미지 생성
# =====================================================

def compose_smk(data: Dict[str, Any], rep_img: Image.Image, app_imgs: List[Image.Image], contact: str) -> Image.Image:
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
    f_title = load_font(50, True)
    f_sub = load_font(26, False)
    f_h = load_font(30, True)
    f_m = load_font(23, True)
    f_b = load_font(21, False)
    f_s = load_font(18, False)
    f_xs = load_font(16, False)

    # Header
    d.rectangle((0, 0, W, 260), fill=light)
    d.rectangle((0, 0, 150, 150), fill=navy)
    d.text((32, 52), "PNU", font=f_logo, fill="white")

    d.text(
        (180, 42),
        f"PNU Tech Offer  x  부산대학교 {data.get('department', '')}, {data.get('professor', '')} 교수",
        font=f_sub,
        fill=navy,
    )

    draw_wrapped(d, (180, 88), data.get("marketing_title", "기술명"), f_title, navy, 950, 6)
    d.text((180, 215), data.get("subtitle", ""), font=f_sub, fill=gray)

    # Left labels
    labels = [
        ("적용\n분야\n제품", 330),
        ("기술\n개요", 620),
        ("기술\n경쟁력", 930),
        ("지식\n재산권\n현황", 1430),
        ("문의처", 1635),
    ]
    for txt, y in labels:
        d.line((55, y - 35, 110, y - 35), fill=blue, width=5)
        d.text((50, y), txt, font=f_h, fill=navy, spacing=6)

    # Applications
    apps = data.get("applications", [])[:3]
    xs = [270, 545, 820]
    for i, app in enumerate(apps):
        x = xs[i]
        d.rounded_rectangle((x, 320, x + 210, 520), radius=30, outline=blue, width=4)

        if i < len(app_imgs):
            icon = fit_image(app_imgs[i], (130, 105))
            img.paste(icon, (x + 40, 338))

        draw_wrapped(d, (x + 22, 455), app.get("name", ""), f_xs, black, 166, 4)

        if i < 2:
            d.line((x + 220, 420, x + 260, 420), fill=(150, 190, 230), width=4)

    # Overview
    rep = fit_image(rep_img, (280, 230))
    img.paste(rep, (220, 600))

    y = 610
    for item in data.get("overview", [])[:3]:
        y = draw_wrapped(d, (550, y), "› " + item, f_b, black, 600, 8) + 8

    # Competitiveness
    d.rounded_rectangle((190, 910, 1120, 1040), radius=60, fill=(238, 248, 252))
    d.rounded_rectangle((230, 940, 390, 1000), radius=30, outline=mint, width=4)
    d.text((262, 956), "기존기술", font=f_m, fill=black)
    d.text((542, 955), "▶  기술 차별성  ▶", font=f_m, fill=blue)
    d.rounded_rectangle((940, 940, 1100, 1000), radius=30, outline=blue, width=4)
    d.text((972, 956), "대상기술", font=f_m, fill=black)
    d.line((620, 1065, 620, 1345), fill=(210, 210, 210), width=2)

    y1 = 1070
    for item in data.get("limitations", [])[:2]:
        y1 = draw_wrapped(d, (210, y1), "● " + item, f_b, black, 380, 8) + 28

    y2 = 1070
    for item in data.get("differentiation", [])[:2]:
        y2 = draw_wrapped(d, (660, y2), "● " + item, f_b, black, 420, 8) + 28

    d.rectangle((210, 1285, 370, 1325), fill=(174, 220, 213))
    d.text((225, 1293), "기술적 한계", font=f_s, fill="white")
    d.rectangle((660, 1285, 820, 1325), fill=(77, 145, 200))
    d.text((675, 1293), "기술적 우위", font=f_s, fill="white")

    y = 1335
    for item in data.get("technical_advantages", [])[:2]:
        y = draw_wrapped(d, (660, y), "▸ " + item, f_s, navy, 430, 6) + 4

    # IP table
    ip = data.get("ip", {})
    d.rectangle((190, 1460, 1120, 1530), fill=(235, 235, 235))

    cols = [190, 500, 780, 1120]
    headers = ["발명의 명칭", "출원/등록번호", "출원/등록일자"]
    for j in range(3):
        d.rectangle((cols[j], 1460, cols[j + 1], 1530), outline=line_gray)
        d.text((cols[j] + 42, 1480), headers[j], font=f_m, fill=black)

    d.rectangle((190, 1530, 1120, 1630), outline=line_gray)
    for c in cols[1:-1]:
        d.line((c, 1530, c, 1630), fill=line_gray, width=2)

    draw_wrapped(d, (205, 1545), ip.get("title", data.get("original_title", "")), f_xs, black, 280, 4)
    draw_wrapped(d, (520, 1555), ip.get("number", ""), f_b, black, 240, 5)
    draw_wrapped(d, (800, 1555), ip.get("date", ""), f_b, black, 280, 5)

    # Contact
    d.text((190, 1665), contact, font=f_b, fill=black)

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

st.title("PNU Tech Offer SMK 생성기")
st.caption("특허 명세서 PDF를 업로드하면 GPT가 SMK 내용을 생성하고, 최종 PDF를 A4 세로 1페이지로 다운로드합니다.")

with st.sidebar:
    st.header("입력 정보")
    uploaded_pdf = st.file_uploader("특허 명세서 PDF 업로드", type=["pdf"])
    department = st.text_input("학과", placeholder="예: 사회환경시스템공학과")
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

if generate_btn:
    if uploaded_pdf is None:
        st.error("특허 명세서 PDF를 업로드하세요.")
        st.stop()

    with st.spinner("PDF 분석 중..."):
        pdf_path = save_uploaded_file(uploaded_pdf)
        patent_text = extract_patent_text(pdf_path)
        rep_img = extract_representative_drawing(pdf_path)
        st.session_state.pdf_path = pdf_path

    with st.spinner("GPT로 SMK 텍스트 생성 중..."):
        data = analyze_patent_with_gpt(patent_text, department, professor)
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
        contact = f"{org} {name} {position}   |   {phone}   |   {email}"
        smk_img = compose_smk(data, rep_img, app_imgs, contact)
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
            file_name="PNU_Tech_Offer_SMK.pdf",
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

            edited_data["department"] = department
            edited_data["professor"] = professor

            rep_img = extract_representative_drawing(st.session_state.pdf_path)
            contact = f"{org} {name} {position}   |   {phone}   |   {email}"

            smk_img = compose_smk(edited_data, rep_img, st.session_state.app_imgs, contact)
            pdf_bytes = make_pdf_bytes_from_image(smk_img)

            st.session_state.smk_data = edited_data
            st.session_state.smk_image = smk_img
            st.session_state.pdf_bytes = pdf_bytes

            st.success("수정 내용이 반영되었습니다. 화면을 새로고침하지 말고 PDF를 다운로드하세요.")
            st.rerun()

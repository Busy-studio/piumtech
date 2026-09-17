from pathlib import Path
import re
import urllib.request

RAW_URL = "https://raw.githubusercontent.com/Busy-studio/piumtech/main/app.py"

def load_source():
    here = Path(__file__).resolve().parent
    for name in ["app.py", "app_original.py"]:
        p = here / name
        if p.exists():
            return p.read_text(encoding="utf-8"), p
    print("현재 폴더에 app.py가 없어 GitHub main의 app.py를 내려받습니다.")
    with urllib.request.urlopen(RAW_URL, timeout=30) as r:
        return r.read().decode("utf-8"), None

def insert_once(text: str, marker: str, block: str, identity: str) -> str:
    if identity in text:
        return text
    if marker not in text:
        raise RuntimeError(f"Marker not found: {marker}")
    return text.replace(marker, block + marker, 1)

def patch_models(text: str) -> str:
    text = text.replace(
        'TEXT_MODEL_FIXED = "gpt-4.1-mini"',
        'TEXT_MODEL_FIXED = os.getenv("TEXT_MODEL", "gpt-5.6-luna")\n'
        'TEXT_REASONING_EFFORT = os.getenv("TEXT_REASONING_EFFORT", "none")'
    )
    text = text.replace(
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-1-mini")',
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-2.5-flare")'
    )
    text = text.replace(
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2")',
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2.5-sunburst")'
    )
    return text

def patch_text_wrapper(text: str):
    count = text.count("client.responses.create(")
    text = text.replace("client.responses.create(", "_text_response_create(client, ")

    helper = '''
def _text_response_create(client, **kwargs):
    """
    GPT-5.6 Luna text-call wrapper for PIUM SMK.

    Cost/data policy:
    - store=False: do not persist response state.
    - prompt_cache_options={"mode":"explicit"} with no explicit breakpoints:
      disables GPT-5.6's implicit prompt-cache write.
    - reasoning effort defaults to 'none' to avoid unnecessary reasoning tokens.
    """
    kwargs.setdefault("model", TEXT_MODEL_FIXED)
    kwargs.setdefault("store", False)
    kwargs.setdefault("prompt_cache_options", {"mode": "explicit"})
    kwargs.setdefault("reasoning", {"effort": TEXT_REASONING_EFFORT})
    return client.responses.create(**kwargs)

'''
    text = insert_once(text, "LANGUAGE_OPTIONS = {", helper, "_text_response_create(")
    return text, count

def patch_icon_helpers(text: str) -> str:
    helper = '''
def _normalize_application_item(item: Any) -> Tuple[str, str]:
    if isinstance(item, dict):
        title = str(
            item.get("title")
            or item.get("name")
            or item.get("application")
            or item.get("product")
            or item.get("label")
            or ""
        ).strip()
        desc = str(
            item.get("desc")
            or item.get("description")
            or item.get("summary")
            or item.get("detail")
            or ""
        ).strip()
        return title, desc
    return str(item or "").strip(), ""


def _pad_icon_to_square(img: Image.Image, size: int = 512, bg: str = "white") -> Image.Image:
    im = img.convert("RGBA")
    w, h = im.size
    if w == 0 or h == 0:
        return Image.new("RGB", (size, size), bg)

    margin = max(24, size // 10)
    max_w = size - margin * 2
    max_h = size - margin * 2
    scale = min(max_w / w, max_h / h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    im = im.resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    x = (size - nw) // 2
    y = (size - nh) // 2
    canvas.alpha_composite(im, (x, y))
    return canvas.convert("RGB")


def _build_line_badge_application_icon_prompt(title: str, desc: str, primary_hex: str, accent_hex: str) -> str:
    return f"""
Create ONE simple brochure icon for a university technology handout.

Application title: {title}
Description: {desc}

Mandatory style:
- clean minimal 2-color outline icon
- line icon + simple pictogram style
- NOT 3D, NOT isometric, NOT realistic
- one centered symbol only
- use very simple geometry and clear silhouette
- icon should occupy about 55-65% of the square canvas
- place the icon inside or over a very light mint circular badge
- badge must be simple, flat, and subtle
- white outer background only

Strictly forbidden:
- no panel
- no card
- no poster
- no signboard
- no screen UI
- no monitor
- no mockup
- no device frame
- no pedestal
- no base bar
- no black underline
- no dark shadow strip
- no room or scene
- no text, letters, numbers, labels, logos, watermark

Color rules:
- main stroke color: {primary_hex}
- secondary accent color: {accent_hex}
- keep the palette very limited
- use white and very light gray only as neutral colors
- do not use generic blue as the main color

Output character:
- polished public-sector brochure icon
- legible at small size
- stable, simple, neat
""".strip()


def _generate_single_application_icon(
    client,
    title: str,
    desc: str,
    primary: Tuple[int, int, int],
    accent: Tuple[int, int, int],
    primary_hex: str,
    accent_hex: str,
) -> Image.Image:
    prompt = _build_line_badge_application_icon_prompt(title, desc, primary_hex, accent_hex)
    result = client.images.generate(
        model=ASSET_IMAGE_MODEL_FIXED,
        prompt=prompt,
        size="1024x1024",
    )
    img_b64 = result.data[0].b64_json
    icon = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    icon = clean_dark_background(icon)
    icon = _smart_trim_visual(icon, threshold=249, padding=26)
    icon = recolor_icon_palette(icon, primary, accent)
    icon = _pad_icon_to_square(icon, size=512)
    return icon


def _generate_application_icons(
    client,
    apps: List[Any],
    primary: Tuple[int, int, int],
    accent: Tuple[int, int, int],
    primary_hex: str,
    accent_hex: str,
) -> List[Image.Image]:
    normalized = [_normalize_application_item(a) for a in (apps or [])]
    normalized = [(t, d) for (t, d) in normalized if t]
    icons: List[Image.Image] = []
    for title, desc in normalized[:3]:
        try:
            icons.append(
                _generate_single_application_icon(
                    client, title, desc, primary, accent, primary_hex, accent_hex
                )
            )
        except Exception:
            icons.append(Image.new("RGB", (512, 512), "white"))
    return icons

'''
    return insert_once(
        text,
        "# -----------------------------------------------------\n# Image utilities",
        helper,
        "_build_line_badge_application_icon_prompt("
    )

def patch_multi_icon_body(text: str):
    # Original 3-split generation block
    old = '''    result = client.images.generate(model=ASSET_IMAGE_MODEL_FIXED, prompt=prompt, size="1536x1024")
    img_b64 = result.data[0].b64_json
    sheet = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    sheet = clean_dark_background(sheet)
    w, h = sheet.size
    icons = []
    for i in range(3):
        crop = sheet.crop((i*w//3, 0, (i+1)*w//3, h))
        icons.append(recolor_icon_palette(crop, primary, accent))
    return icons'''
    new = '    return _generate_application_icons(client, apps, primary, accent, primary_hex, accent_hex)'
    if old in text:
        return text.replace(old, new, 1), True

    # If an earlier patch already changed it, keep as-is.
    if new in text:
        return text, True

    pattern = re.compile(
        r'^\s*result = client\.images\.generate\(model=ASSET_IMAGE_MODEL_FIXED, prompt=prompt, size="1536x1024"\)\n'
        r'\s*img_b64 = result\.data\[0\]\.b64_json\n'
        r'\s*sheet = Image\.open\(BytesIO\(base64\.b64decode\(img_b64\)\)\)\.convert\("RGB"\)\n'
        r'\s*sheet = clean_dark_background\(sheet\)\n'
        r'\s*w, h = sheet\.size\n'
        r'\s*icons = \[\]\n'
        r'\s*for i in range\(3\):\n'
        r'\s*crop = sheet\.crop\(\(i\*w//3, 0, \(i\+1\)\*w//3, h\)\)\n'
        r'\s*icons\.append\(recolor_icon_palette\(crop, primary, accent\)\)\n'
        r'\s*return icons',
        re.M
    )
    text2, n = pattern.subn(new, text, count=1)
    return text2, bool(n)

def main():
    src, _ = load_source()
    text = patch_models(src)
    text, text_call_count = patch_text_wrapper(text)
    text = patch_icon_helpers(text)
    text, icon_body_patched = patch_multi_icon_body(text)

    required = [
        'gpt-5.6-luna',
        'gpt-image-2.5-flare',
        'gpt-image-2.5-sunburst',
        '_build_line_badge_application_icon_prompt(',
        '_generate_application_icons(',
        'prompt_cache_options',
        'store", False',
    ]
    missing = [x for x in required if x not in text]
    if missing:
        raise RuntimeError(f"Patch validation failed. Missing: {missing}")
    if text_call_count < 1:
        raise RuntimeError("No client.responses.create calls found; source layout may have changed.")
    if not icon_body_patched:
        raise RuntimeError("Could not patch the application-icon generation block.")

    out = Path(__file__).resolve().parent / "app_patched.py"
    out.write_text(text, encoding="utf-8", newline="\n")

    print()
    print("완료:", out)
    print(f"텍스트 Responses 호출 {text_call_count}곳을 공통 래퍼로 전환")
    print("텍스트: gpt-5.6-luna / reasoning=none / store=False / implicit prompt cache 비활성")
    print("일반 이미지: gpt-image-2.5-flare")
    print("프리미엄 이미지: gpt-image-2.5-sunburst")
    print("적용제품 아이콘: 단순 2색 라인 아이콘 + 연한 원형 배지 방식으로 전환")
    print()
    print("GitHub에 올릴 때 app_patched.py를 app.py로 이름 변경해서 업로드하세요.")

if __name__ == "__main__":
    main()

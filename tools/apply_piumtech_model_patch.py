from pathlib import Path
import re
import subprocess
import sys

APP = Path("app.py")


def replace_required(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"{label}: expected source text not found")
    return text.replace(old, new, 1)


def patch_models(text: str) -> str:
    text = replace_required(
        text,
        'TEXT_MODEL_FIXED = "gpt-4.1-mini"',
        'TEXT_MODEL_FIXED = os.getenv("TEXT_MODEL", "gpt-5.6-luna")\nTEXT_REASONING_EFFORT = os.getenv("TEXT_REASONING_EFFORT", "none")',
        "text model",
    )
    text = replace_required(
        text,
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-1-mini")',
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-2.5-flare")',
        "asset image model",
    )
    text = replace_required(
        text,
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2")',
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2.5-sunburst")\nPREMIUM_IMAGE_QUALITY = os.getenv("PREMIUM_IMAGE_QUALITY", "xhigh")',
        "premium image model",
    )
    return text


def patch_text_calls(text: str) -> str:
    if "def _text_response_create(client, **kwargs):" not in text:
        text = text.replace("client.responses.create(", "_text_response_create(client, ")
        helper = '''\n\ndef _text_response_create(client, **kwargs):\n    \"\"\"Cost-conscious GPT-5.6 Luna Responses wrapper.\"\"\"\n    kwargs.setdefault("model", TEXT_MODEL_FIXED)\n    kwargs.setdefault("store", False)\n    kwargs.setdefault("prompt_cache_options", {"mode": "explicit"})\n    kwargs.setdefault("reasoning", {"effort": TEXT_REASONING_EFFORT})\n    return client.responses.create(**kwargs)\n\n'''
        marker = "LANGUAGE_OPTIONS = {"
        if marker not in text:
            raise RuntimeError("LANGUAGE_OPTIONS marker not found")
        text = text.replace(marker, helper + marker, 1)
    return text


SINGLE_ICON_FUNCTION = r'''def generate_application_image(title: str, desc: str, university_logo: Image.Image | None = None) -> Image.Image:
    client = get_client()
    theme = extract_logo_theme(university_logo)
    primary = theme["primary"]
    accent = theme["accent"]
    primary_hex = rgb_to_hex(primary)
    accent_hex = rgb_to_hex(accent)
    prompt = f"""
Create ONE simple application/product icon for a Korean university technology-transfer brochure.
Application: {title}
Description: {desc}

MANDATORY VISUAL DIRECTION:
- minimal two-color OUTLINE ICON / pictogram
- simple flat vector line-art, NOT 3D, NOT isometric, NOT realistic
- one clear symbolic object only, recognizable at small size
- place the symbol on a very pale mint circular badge
- white outer background only
- the icon itself should occupy roughly 55-65% of the square canvas
- clean public-sector brochure aesthetic

STRICTLY FORBIDDEN:
- no panel, no card, no poster, no signboard
- no monitor, no screen UI, no dashboard, no device mockup
- no floating frame, no pedestal, no base bar, no underline
- no black shadow strip, no dark vignette
- no room, no landscape, no surrounding scene
- no text, letters, numbers, labels, logos, watermark

COLOR RULES:
- main stroke / main fill color: {primary_hex}
- secondary accent: {accent_hex}
- use only the university brand color family plus white / very light gray
- do not default to generic blue

COMPOSITION:
- centered and visually balanced
- crisp medium-weight outline
- simple geometry with generous white margin
"""
    result = client.images.generate(
        model=ASSET_IMAGE_MODEL_FIXED,
        prompt=prompt,
        size="1024x1024",
    )
    img_b64 = result.data[0].b64_json
    img = Image.open(BytesIO(base64.b64decode(img_b64))).convert("RGB")
    img = clean_dark_background(img)
    img = _smart_trim_visual(img, threshold=249, padding=24)
    img = recolor_icon_palette(img, primary, accent)
    return fit_image(img, (1024, 1024), bg=(255,255,255), trim=False)
'''

SET_ICON_FUNCTION = r'''def generate_application_images_set(apps: List[Dict[str, Any]], university_logo: Image.Image | None = None) -> List[Image.Image]:
    \"\"\"Generate each application icon independently to avoid unstable 3-way sheet cropping.\"\"\"
    icons: List[Image.Image] = []
    for app in apps[:3]:
        title = str(app.get("name", "") or "")
        desc = str(app.get("description", "") or "")
        try:
            icons.append(generate_application_image(title, desc, university_logo=university_logo))
        except Exception:
            icons.append(Image.new("RGB", (1024,1024), "white"))
    return icons
'''


def patch_application_icons(text: str) -> str:
    start = text.find("def generate_application_image(")
    if start < 0:
        raise RuntimeError("generate_application_image not found")
    set_start = text.find("def generate_application_images_set(", start)
    if set_start < 0:
        raise RuntimeError("generate_application_images_set not found")
    utilities_marker = "# -----------------------------------------------------\n# Image utilities"
    end = text.find(utilities_marker, set_start)
    if end < 0:
        raise RuntimeError("Image utilities marker not found")
    replacement = SINGLE_ICON_FUNCTION + "\n\n" + SET_ICON_FUNCTION + "\n\n"
    return text[:start] + replacement + text[end:]


def patch_premium_quality(text: str) -> str:
    target = '''            result = client.images.edit(\n                model=PREMIUM_IMAGE_MODEL_FIXED,\n                image=files,\n                prompt=prompt,\n                size='1024x1536',\n            )'''
    replacement = '''            result = client.images.edit(\n                model=PREMIUM_IMAGE_MODEL_FIXED,\n                image=files,\n                prompt=prompt,\n                size='1024x1536',\n                quality=PREMIUM_IMAGE_QUALITY,\n            )'''
    if replacement not in text:
        if target not in text:
            raise RuntimeError("premium images.edit block not found")
        text = text.replace(target, replacement, 1)
    text = text.replace(
        "번들된 premium_infographic_reference.png를 gpt-image-2로 직접 참고해",
        "번들된 premium_infographic_reference.png를 gpt-image-2.5-sunburst로 직접 참고해",
    )
    return text


def validate(text: str) -> None:
    required = [
        'TEXT_MODEL_FIXED = os.getenv("TEXT_MODEL", "gpt-5.6-luna")',
        'TEXT_REASONING_EFFORT = os.getenv("TEXT_REASONING_EFFORT", "none")',
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-2.5-flare")',
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2.5-sunburst")',
        'PREMIUM_IMAGE_QUALITY = os.getenv("PREMIUM_IMAGE_QUALITY", "xhigh")',
        'prompt_cache_options',
        'kwargs.setdefault("store", False)',
        'minimal two-color OUTLINE ICON',
        'Generate each application icon independently',
        'quality=PREMIUM_IMAGE_QUALITY',
    ]
    missing = [x for x in required if x not in text]
    if missing:
        raise RuntimeError(f"validation failed; missing: {missing}")

    forbidden = [
        'TEXT_MODEL_FIXED = "gpt-4.1-mini"',
        'ASSET_IMAGE_MODEL_FIXED = os.getenv("ASSET_IMAGE_MODEL", "gpt-image-1-mini")',
        'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2")',
        '3개 적용분야 아이콘을 한 번에 생성 후 3등분',
    ]
    present = [x for x in forbidden if x in text]
    if present:
        raise RuntimeError(f"validation failed; legacy settings remain: {present}")


def main() -> None:
    if not APP.exists():
        raise RuntimeError("app.py not found")
    text = APP.read_text(encoding="utf-8")
    text = patch_models(text)
    text = patch_text_calls(text)
    text = patch_application_icons(text)
    text = patch_premium_quality(text)
    validate(text)
    APP.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run([sys.executable, "-m", "py_compile", str(APP)], check=True)
    print("PIUMTech patch applied and py_compile passed")


if __name__ == "__main__":
    main()

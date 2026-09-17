from pathlib import Path
import re
import sys
import urllib.request

RAW_URL = "https://raw.githubusercontent.com/Busy-studio/piumtech/main/app.py"

def load_source():
    here = Path(__file__).resolve().parent
    candidates = [
        here / "app.py",
        here / "app_original.py",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8"), p
    print("현재 폴더에 app.py가 없어 GitHub main의 app.py를 내려받습니다.")
    with urllib.request.urlopen(RAW_URL, timeout=30) as r:
        data = r.read().decode("utf-8")
    return data, None

def patch(text: str) -> str:
    original = text

    # 1) Model routing
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

    # 2) Replace all current text calls with a single cost-conscious wrapper.
    # Do this BEFORE injecting the wrapper so the wrapper's own SDK call is not replaced.
    count = text.count("client.responses.create(")
    text = text.replace("client.responses.create(", "_text_response_create(client, ")

    helper = r
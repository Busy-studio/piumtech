from pathlib import Path
import re

def load_source():
    here = Path(__file__).resolve().parent
    for name in ["app.py", "app_original.py"]:
        p = here / name
        if p.exists():
            return p.read_text(encoding="utf-8"), p
    raise FileNotFoundError("같은 폴더에 app.py 또는 app_original.py를 넣어주세요.")

def main():
    text, src_path = load_source()

    # 1) Optional env-based quality constant
    if 'PREMIUM_IMAGE_QUALITY = os.getenv("PREMIUM_IMAGE_QUALITY", "xhigh")' not in text:
        marker = 'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2.5-sunburst")'
        if marker in text:
            text = text.replace(
                marker,
                marker + '\nPREMIUM_IMAGE_QUALITY = os.getenv("PREMIUM_IMAGE_QUALITY", "xhigh")',
                1
            )
        else:
            # fallback for older file
            marker2 = 'PREMIUM_IMAGE_MODEL_FIXED = os.getenv("PREMIUM_IMAGE_MODEL", "gpt-image-2")'
            if marker2 in text:
                text = text.replace(
                    marker2,
                    marker2 + '\nPREMIUM_IMAGE_QUALITY = os.getenv("PREMIUM_IMAGE_QUALITY", "xhigh")',
                    1
                )

    # 2) Insert quality into premium images.edit call if missing
    if 'quality=PREMIUM_IMAGE_QUALITY' not in text:
        pattern = re.compile(
            r'(result\s*=\s*client\.images\.edit\(\s*\n'
            r'\s*model\s*=\s*PREMIUM_IMAGE_MODEL_FIXED,\s*\n'
            r'(?:.*\n)*?'
            r'\s*size\s*=\s*"1024x1536",\s*\n)'
            r'(\s*\))',
            re.M
        )
        text2, n = pattern.subn(r'\1    quality=PREMIUM_IMAGE_QUALITY,\n\2', text, count=1)
        if n:
            text = text2
        else:
            # more permissive fallback: any premium images.edit call
            pattern2 = re.compile(
                r'(client\.images\.edit\(\s*\n'
                r'\s*model\s*=\s*PREMIUM_IMAGE_MODEL_FIXED,\s*\n'
                r'(?:.*\n)*?)'
                r'(\s*\))',
                re.M
            )
            def repl(m):
                block = m.group(1)
                if 'quality=' in block:
                    return m.group(0)
                return block + '    quality=PREMIUM_IMAGE_QUALITY,\n' + m.group(2)
            text2, n = pattern2.subn(repl, text, count=1)
            if n:
                text = text2

    if 'quality=PREMIUM_IMAGE_QUALITY' not in text:
        raise RuntimeError("프리미엄 images.edit 호출을 찾지 못했습니다. 파일 구조가 바뀌었을 수 있습니다.")

    out = Path(__file__).resolve().parent / "app_patched.py"
    out.write_text(text, encoding="utf-8", newline="\n")

    print()
    print("완료:", out)
    print("프리미엄 이미지 quality 설정 추가 완료")
    print('PREMIUM_IMAGE_QUALITY 기본값: "xhigh"')
    print()
    print("GitHub에 올릴 때 app_patched.py를 app.py로 이름 변경해서 업로드하세요.")

if __name__ == "__main__":
    main()

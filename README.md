# PNU SMK Generator - Streamlit

특허 명세서 PDF를 업로드하면 OpenAI API를 활용해 1페이지 SMK 초안을 생성하고 PDF로 다운로드하는 Streamlit 앱입니다.

## GitHub 업로드 파일

```text
app.py
requirements.txt
packages.txt
.gitignore
.streamlit/secrets.toml.example
```

## Streamlit Cloud 배포 방법

1. GitHub 저장소 생성
2. 위 파일 업로드
3. Streamlit Cloud에서 해당 저장소 연결
4. App file path는 `app.py`로 설정
5. Streamlit Cloud의 Settings > Secrets에 아래 값 입력

```toml
OPENAI_API_KEY = "sk-본인_API키"
```

## 로컬 실행

```bash
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

## 주의

- API 키는 절대 GitHub에 올리지 마세요.
- 한글 폰트는 `packages.txt`의 `fonts-nanum`으로 Streamlit Cloud에서 설치되도록 구성했습니다.

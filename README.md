# PIUM Tech Offer SMK 생성기

## Streamlit Cloud 설정

1. 이 폴더의 파일을 GitHub 저장소에 업로드합니다.
2. Streamlit Cloud에서 앱을 연결합니다.
3. `Settings > Secrets`에 아래 값을 입력합니다.

```toml
OPENAI_API_KEY = "sk-본인_API키"
```

## 포함 기능

- 특허 명세서 PDF 업로드
- 대학교 선택 또는 수기입력
- GPT 기반 SMK 텍스트 생성
- 적용분야 이미지 생성
- A4 1페이지 PDF 다운로드
- 수정 가능한 PPTX 다운로드
- JSON 직접 수정 후 PDF/PPTX 재렌더링

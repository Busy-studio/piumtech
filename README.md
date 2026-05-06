# PIUM Tech Offer SMK 생성기 v4

## 반영사항

- 적용분야 이미지 프롬프트를 흰색 배경/미래형 컬러 플래티콘 스타일로 강화
- 생성 이미지가 검정 배경으로 나오는 경우 가장자리와 연결된 어두운 배경을 자동으로 흰색 처리
- 적용분야제품 카드, 기술개요, 기술경쟁력, 지식재산권 현황의 기준 x/y 좌표 재정렬
- 본문/표/라벨 폰트 크기와 섹션 간격 조정
- PDF 다운로드 및 수정용 PPTX 다운로드 유지

## Streamlit Cloud 설정

1. 이 폴더의 파일을 GitHub 저장소에 업로드합니다.
2. Streamlit Cloud에서 앱을 연결합니다.
3. `Settings > Secrets`에 아래 값을 입력합니다.

```toml
OPENAI_API_KEY = "sk-본인_API키"
```

## 파일 구성

- `app.py`
- `requirements.txt`
- `packages.txt`
- `.streamlit/secrets.toml.example`
- `.gitignore`

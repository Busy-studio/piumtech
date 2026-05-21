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

## Premium Reference Mode
- 번들된 `assets/reference/premium_infographic_reference.png` 를 기준 레퍼런스로 사용
- "프리미엄 레퍼런스 기반 이미지/PDF로 변환" 버튼 추가
- 생성 후 대학 로고 / PIUM 로고 / QR 코드 / 대표도면은 원본 그대로 후합성
- AI 생성 실패 시 기존 고품질 인포그래픽 렌더러로 자동 fallback


## Image model routing
- Asset/application images use `gpt-image-1-mini` by default via `ASSET_IMAGE_MODEL_FIXED`.
- Reference-based premium infographic generation uses `gpt-image-2` by default via `PREMIUM_IMAGE_MODEL_FIXED`.
- You can override them with environment variables: `ASSET_IMAGE_MODEL`, `PREMIUM_IMAGE_MODEL`.

## Premium Reference Asset Mode Update
- 프리미엄 모드는 후합성을 사용하지 않고 reference 이미지들을 함께 전달합니다.
- 전달 reference: premium reference / 현재 SMK / 대학 로고 / PIUM+QR 카드 / 대표도면.
- gpt-image-2가 기존 자산을 자연스럽게 활용해 프리미엄 디자인을 생성합니다.
- 적용분야 이미지는 gpt-image-1-mini, 프리미엄 최종 이미지는 gpt-image-2를 사용합니다.


## Premium Template Overlay Mode
- 프리미엄 변환은 `gpt-image-2`가 레퍼런스 기반의 텍스트 없는 배경/카드 템플릿을 생성합니다.
- 로고, QR, 대표도면, 제목/본문/IP/문의처, 시장그래프는 코드로 정확히 합성합니다.
- 기존의 "완성본 전체를 AI가 다시 그리는 방식"에서 발생하던 QR 깨짐, 로고 변형, 텍스트 오탈자 문제를 줄이기 위한 구조입니다.

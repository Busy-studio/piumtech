# Premium Reference Usage

이 폴더의 `premium_infographic_reference.png` 는 프리미엄 인포그래픽 생성의 **단일 기준 레퍼런스 이미지**입니다.

## 동작 방식
- 앱의 `프리미엄 레퍼런스 기반 이미지/PDF로 변환` 버튼은 이 이미지를 직접 참고하여 AI 기반 프리미엄 인포그래픽을 생성합니다.
- 생성 후 아래 요소는 **원본 그대로 다시 합성**합니다.
  - 대학 로고
  - PIUM 로고
  - QR 코드
  - 대표도면
- 따라서 시각적 스타일은 레퍼런스 기반으로 고급화하되, 정확성이 필요한 핵심 시각 요소는 변형을 최소화합니다.
- AI 생성이 실패하면 기존 규칙 기반 고품질 렌더러로 자동 fallback 됩니다.

## 파일 경로
- `assets/reference/premium_infographic_reference.png`

## Image model routing update
- 일반 적용분야/제품 이미지 생성: `gpt-image-1-mini`
- 프리미엄 레퍼런스 기반 최종 인포그래픽 생성: `gpt-image-2`
- 시장현황 그래프는 이미지 모델이 아니라 코드 기반 막대그래프로 생성하는 방향을 유지합니다.
- 필요 시 환경변수로 모델명을 바꿀 수 있습니다.
  - `ASSET_IMAGE_MODEL`
  - `PREMIUM_IMAGE_MODEL`

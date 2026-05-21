# Premium Reference Mode

`premium_infographic_reference.png` 는 프리미엄 인포그래픽 생성의 단일 디자인 레퍼런스입니다.

## 이번 버전의 생성 방식
- `gpt-image-2`에 아래 reference들을 함께 전달합니다.
  1. `premium_infographic_reference.png` — 프리미엄 디자인 기준
  2. 현재 생성된 기본 SMK 이미지 — 실제 내용/구성 기준
  3. 대학 로고 원본 — 좌측 상단 로고 기준
  4. PIUM+QR 카드 원본 — 우측 상단 카드 기준
  5. 대표도면 원본 — 대표도면 영역 기준
- 별도 좌표 기반 후합성은 사용하지 않습니다.
- 프롬프트에서 기존 자산을 그대로 활용하고, 디자인만 프리미엄화하도록 지시합니다.
- 생성 실패 시 기존 규칙 기반 고품질 렌더러로 자동 fallback 됩니다.

## 모델 라우팅
- 적용분야/제품 이미지: `gpt-image-1-mini`
- 프리미엄 최종 인포그래픽: `gpt-image-2`
- 시장현황 그래프: 코드 기반 생성

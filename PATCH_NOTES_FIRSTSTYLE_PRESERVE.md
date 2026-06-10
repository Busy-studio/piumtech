# First-style premium preserve patch

## 방향
- 가장 처음의 프리미엄 AI 생성 스타일을 유지
- 별도의 후합성(overlay) 없이 생성 결과를 그대로 사용
- 대학 로고, PIUM+QR 블록, 대표도면은 참조 이미지 기반으로 최대한 보존

## 주요 변경 사항
1. 프리미엄 프롬프트에서 로고 / PIUM+QR / 대표도면 보존을 더 강하게 지시
2. 생성 후 `apply_mandatory_premium_corrections(...)` 후합성 파이프라인 제거
3. 프리미엄 결과는 `gpt-image-2`가 생성한 이미지를 그대로 사용
4. 기존 생성 해상도 `1024x1536` 유지

## 수정 파일
- app.py

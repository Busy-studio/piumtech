PIUMTECH 모델 + 적용제품 아이콘 패치

적용 내용
1) 텍스트
- gpt-5.6-luna
- reasoning 기본 none
- store=False
- prompt_cache_options={"mode":"explicit"} 로 implicit prompt cache write 방지

2) 이미지
- 일반 이미지: gpt-image-2.5-flare
- 프리미엄 이미지: gpt-image-2.5-sunburst

3) 적용제품 아이콘 로직 수정
- 기존: 3개 아이콘을 한 장(1536x1024)으로 생성 후 3등분 crop
- 변경: 적용제품마다 개별 1:1 아이콘 생성
- 스타일 강화:
  * Flaticon류 벡터 아이콘
  * panel/card/screen/mockup/base bar 금지
  * 흰 배경
  * 아이콘을 캔버스에서 크게 차지하도록 유도
- 후처리:
  * clean_dark_background
  * _smart_trim_visual
  * 학교 컬러 recolor
  * 정사각 캔버스 중앙 정렬

사용법
1. 현재 app.py를 이 폴더에 넣기
   (없으면 GitHub main의 app.py를 내려받으려고 시도합니다.)
2. APPLY_PATCH.cmd 실행
3. 생성된 app_patched.py를 app.py로 이름 변경
4. GitHub 또는 배포 서버에 업로드

주의
- 배포 환경에 TEXT_MODEL, ASSET_IMAGE_MODEL, PREMIUM_IMAGE_MODEL 환경변수가 이미 있으면
  그 값이 코드 기본값보다 우선합니다.
- 기존 3분할 생성 블록이 많이 바뀐 경우 자동 패치가 실패할 수 있습니다.

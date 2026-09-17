PIUMTECH 모델 업데이트 패치

적용 내용
- 텍스트: gpt-5.6-luna
- 일반 이미지: gpt-image-2.5-flare
- 프리미엄 이미지: gpt-image-2.5-sunburst
- Responses API store=False
- GPT-5.6 implicit prompt cache breakpoint 비활성:
  prompt_cache_options={"mode":"explicit"} + explicit breakpoint 미사용
- reasoning 기본 none
- 기존 temperature / web_search / OCR 입력 흐름 유지

사용법
1. 가능하면 현재 GitHub의 app.py를 이 폴더에 같이 둡니다.
   (없으면 PATCH_APP.py가 GitHub main의 app.py를 직접 내려받습니다.)
2. Windows에서는 APPLY_PATCH.cmd 실행
   또는 python PATCH_APP.py 실행
3. 생성된 app_patched.py를 app.py로 이름 변경해 GitHub에 업로드
4. requirements.txt도 requirements_patched.txt 내용으로 교체 권장

주의
- 환경변수 TEXT_MODEL / TEXT_REASONING_EFFORT / ASSET_IMAGE_MODEL /
  PREMIUM_IMAGE_MODEL 이 배포 환경에 이미 설정돼 있으면 그 값이 코드 기본값보다 우선합니다.

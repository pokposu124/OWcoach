# 이 폴더

사용자가 제공한 `overwatch_stats_2026-10-15.zip`(Overwatch Wiki 수집본, 2026-10-15)에서 조회에 필요한 파일만 넣었다.
`sha256sum -c CHECKSUMS.sha256`로 원본과 같음을 확인했다(2026-10-15). `CHECKSUMS.sha256`에는 여기 넣지 않은
`overwatch_heroes.json`(같은 내용이 `overwatch.sqlite`의 `data_json`에 있음)과 재수집 스크립트(`collect.py`, `audit.py`,
`build_database.py`, `validate.py`, `requirements.txt`)도 적혀 있다. 재수집이 필요하면 원본 zip을 쓴다.

분석기는 `owcoach/gamedata.py`로 읽기 전용 조회한다. 규칙: `CLAUDE_USAGE.md`. 출처·라이선스: `LICENSE-DATA.md`.

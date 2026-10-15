# OWcoach — 작업 규칙

## 게임 수치
- 영웅·스킬 수치(쿨타임, 지속 시간, 피해·치유량, 체력, 궁극기 비용, 특전)는 **`analyzer/data/overwatch_stats/`의 수집본을 기준**으로 한다 (사용자 지시 2026-10-15).
  모델 기억으로 수치를 쓰거나 빈 값을 채우지 않는다.
- 사용 규칙은 `analyzer/data/overwatch_stats/CLAUDE_USAGE.md`를 먼저 읽고 따른다. 요약:
  - 그 경기의 영웅·스킬만 조회한다(`owcoach/gamedata.py`: `get_hero_stats`, `get_ability_stats`, `get_perks`, `get_review_items`).
  - 값에는 단위·조건·상태(`wiki_only` / `official_component_matched`)·검토 필요 여부·고정 개정 링크를 함께 둔다.
  - 기본 수치, 특전으로 바뀐 수치, 영상에서 관측한 현재 상태(체력·남은 쿨타임)를 구분한다. 선택한 특전은 확인될 때만 반영한다.
  - `review_required` / 검토 항목 수치는 확정 판단의 근거로 쓰지 않는다.
  - 경기 패치를 확인할 수 없으면 수치는 "확인이 필요한 전제"로 표시한다. 문서 수정 시각을 패치 번호로 쓰지 않는다.
- 데이터 라이선스: Overwatch Wiki CC BY-NC-SA 3.0 (`LICENSE-DATA.md`). 출처 표기를 유지한다.

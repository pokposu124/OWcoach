# Overwatch 영웅 스탯 데이터베이스

2026-10-15 수집본입니다. Overwatch Wiki의 현재 영웅 54명에 대해 기본 체력, 무기, 일반/지속/궁극기, 특전, 조건별 수치와 상호작용을 정리했습니다. 기본 조회 조건은 **일반 5대5 역할 고정 PvP의 기본 형태**입니다.

## 바로 사용하기

- `overwatch_heroes.json`: 모든 영웅, 능력, 특전, 수치, 효과, 조건과 출처. Claude Code에 직접 제공할 수 있습니다.
- `overwatch.sqlite`: 정규화된 테이블과 전문 검색 인덱스. Python 표준 라이브러리만으로 조회할 수 있습니다.
- `coverage_report.md`: 수집 범위, 영웅별 목록, 검증 결과와 검토 대상.
- `CLAUDE_USAGE.md`: AI 피드백 시스템에 붙일 때 사용할 규칙과 예시.
- `schema.sql`: SQLite 구조.
- `query.py`: 읽기 전용 조회 도구.
- `collect.py`, `audit.py`, `build_database.py`, `validate.py`: 재수집, 대조, DB 생성 및 검증 도구.
- `hero_localizations.json`: OverFast API에서 확보한 영웅의 영문 식별자/한국어 표시명. 스킬 이름과 원문의 효과 설명은 영문입니다.
- `LICENSE-DATA.md`: 출처와 데이터 이용조건.

압축을 푼 디렉터리에서 실행합니다. 조회에는 Python 3.10 이상만 있으면 됩니다.

```bash
python query.py hero tracer
python query.py hero 트레이서
python query.py ability tracer Recall
python query.py ability dva "Micro Missiles"
python query.py search "invulnerable"
python query.py review
python validate.py
```

```python
import sqlite3
db = sqlite3.connect("file:overwatch.sqlite?mode=ro", uri=True)
rows = db.execute("""
    SELECT hero_name, ability, cooldown_s, max_charges, revision_url
    FROM cooldowns WHERE hero_id = ?
""", ("tracer",)).fetchall()
```

## 수집 범위와 상태

영웅 페이지의 현재 `Abilities` 및 `Perks` 카드만 추출했습니다. 과거 변경 이력, 삭제된 스킬/특전, 스토리/전략 문단은 현재 능력 목록으로 넣지 않았습니다. 스타디움/클래식/6대6 전용 카드도 제외했습니다. 다만 탱커 체력의 자유 역할·6대6·파일럿 형태와 같은 조건별 원문은 `hitpoints.*.variants`에 보존했습니다. 이는 전체 모드의 완성 DB가 아닙니다.

일반적인 `abilities`에는 무기, 일반 스킬, 궁극기 외에 역할·세부 역할 지속 능력도 포함됩니다. `perks`는 별도로 분리했습니다. 표에 표시되지 않은 방어력·보호막은 0으로 해석하되 `inferred_absent_zero`로 명시했습니다. 누락·복합 표기 때문에 단일 값으로 결정하지 못한 필드는 `null`입니다.

`patch_id`는 전체 영웅에 대해 미확정인 `null`로 남겼습니다. 문서 수정 시각이나 수집 날짜를 게임 패치 날짜로 바꾸지 않았습니다. 각 영웅의 `source`에는 고정 개정 링크, 개정 ID, 수정 시각, 수집 시각, 원본 SHA-256이 들어 있습니다.

## 공식 패치 대조

최근 3개월(2026년 8~10월)의 공식 패치 페이지를 확보해 변경 이력을 인덱싱했습니다. **원문의 모든 변경을 수치별로 전수 검증한 것은 아닙니다.** 개별 대조 범위는 다음과 같습니다.

- 10월 13일 핫픽스의 수치 변경 12개: 12개 일치.
- 10월 6일의 주요 수치·솜브라/로드호그 개편·특전 추가/삭제와 8월 11일 트레이서 시간 역행: 선택한 항목 대조.
- 전체 대조 기록 54개 중 수치/텍스트 조건 37개 일치, 능력 존재/제거 16개 일치, 모호한 표기 1개 미해결.
- 위키 내부 설명/수치 관계의 별도 검토 항목 1개.
- 한조 `Dragon Fury` 카드의 `Major Perk` 오표기는 블리자드 공식 영웅 페이지의 `Minor Perk` 분류로 교정했습니다. 원래 값과 교정 근거는 `classification_correction`에 보존했습니다.

`official_checks`는 **선택한 수치·조건만** 검증합니다. `official_component_matched`는 해당 스탯의 다른 범위·거리·강화 조건까지 확인했다는 뜻이 아닙니다. 대조하지 않은 수치와 효과는 `wiki_only`입니다. 해당 영웅 전체를 공식 검증된 것으로 취급하면 안 됩니다.

검토 대상은 독트린 `Deliverance`의 효과 지속/감쇠 시간 구분과 로드호그 `Hogdrogen Exposure`의 치유 설명/표기값 관계입니다. `query.py review`, SQL의 `review_queue`, `manual_reviews`, 능력의 `review_required`를 사용해 확인할 수 있습니다. 미해결 값을 임의로 고치거나 조용히 덮어쓰지 않았습니다.

## 수치를 해석하는 규칙

- `stats[].text`와 `components[].text`가 조건을 포함한 원문 표기입니다.
- `components[].numbers`는 등장 순서대로 추출한 숫자입니다. 자동으로 최솟값/최댓값 또는 계산식으로 해석하지 마세요. `5.75 - 1.725 (per bullet)`는 거리 감소를 나타내며, `0.144 + 0.192 seconds`는 단계별 시간입니다.
- 하나의 조건 없는 단일 수치만 `scalar`로 제공합니다. 복합 조건은 `null`입니다.
- `cooldown.seconds`는 기본 쿨타임 또는 충전당 재충전 시간입니다. 경기에서 현재 남은 쿨타임이 아닙니다.
- 궁극기 충전 비용을 일반 스킬 쿨타임에 넣지 않았습니다.
- 피해 계산에는 발당/발사당/총합, 거리 감소, 치명타, 방어력, 강화, 특전과 `effect_notes`의 예외를 함께 확인해야 합니다.
- 상호작용 아이콘은 내부 이미지의 대체 텍스트가 아니라 실제 카드의 설명 툴팁을 사용했습니다. 일부 아이콘의 내부 이미지 설명은 반대 의미를 표시하므로 그대로 사용하면 안 됩니다.
- 영문 수치·조건은 보존했습니다. 한국어 자유 번역 과정에서 효과 예외나 단위가 바뀌지 않게 하기 위함입니다.

## 재수집

수집/파싱에만 `lxml`이 필요합니다. 서비스 실행, 조회, SQLite 이용에는 필요하지 않습니다.

```bash
python -m pip install lxml
python collect.py --cache raw --out collected.json --as-of 2026-10-15 --refresh
python audit.py --source collected.json --out overwatch_heroes.json --localization hero_localizations.json
python build_database.py --source overwatch_heroes.json --out overwatch.sqlite
python validate.py
```

`--refresh`는 원문 캐시를 갱신합니다. 생략하면 기존 캐시를 재사용합니다. `--as-of`는 공식 패치 비교의 날짜 경계이며, 위키의 과거 개정본을 자동 복원하는 옵션이 아닙니다. 과거 날짜로 실행한다고 당시의 완전한 스탯을 얻을 수는 없습니다.

`audit.py`의 검증 규칙은 이 수집본의 최신 공식 패치인 **2026-10-13**에 맞췄습니다. 더 새로운 패치가 나오면 중단하도록 했으므로, Claude Code에서 공식 변경을 확인하고 규칙을 갱신해야 합니다. 현재 위키 수집 자체와 검증 규칙의 최신성은 별개입니다.

수집기는 robots.txt를 확인하고 요청 간 1초 이상 간격을 둡니다. 접근 거부나 제한 응답이 나오면 우회하거나 반복 재시도하지 않습니다. 원문 HTML 캐시는 재현/분석용 로컬 중간 파일이며 이 배포 묶음에는 포함하지 않았습니다.

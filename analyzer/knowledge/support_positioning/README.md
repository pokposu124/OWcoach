# 지원 영웅 포지셔닝·코칭 지식베이스

2026-10-18 작성 · v0.1.0 · 모든 기준은 `draft` · 일반 5대5 역할 고정

공통 포지셔닝 원칙 15개와 지원 영웅 16명의 개별 기준 128개를 정리했다. 개별 기준은 포지셔닝 48개, 일반 스킬 64개, 궁극기 16개다. **포지셔닝 기준은 총 63개**다. 스킬·궁극기 항목은 위치 판단의 원인과 기회비용을 함께 검토하기 위한 확장이다.

## 읽는 순서

1. [공통 원칙과 조사 출처](common_principles.md): 15개 원칙, 적용 조건·예외, 관찰 데이터 설계.
2. [영웅별 상세 기준](hero_criteria.md): 16명 각각의 좋은 판단·실수 후보·예외·필요한 영상 근거.
3. [영웅별 YAML](criteria/), [공통 YAML](common_criteria.yaml): 기존 `Criterion` 모델에 맞춘 기준 조각.
4. [출처와 데이터 개정 기록](sources.yaml): 확인한 내용 범위, DB 고정 개정 링크·라이선스.
5. [합성 검토 예시](review_examples.yaml): 실제 경기 정답이 아닌 경계 사례.

## 영웅별 포지셔닝 초점

| 영웅 | 포지셔닝 기준 3개 | YAML |
|---|---|---|
| 아나 | 치료 시야와 엄폐를 함께 유지 / 탱커 전진에 맞춘 단계적 이동 / 다이브 접근로와 도움받을 경로 확보 | [ana.yaml](criteria/ana.yaml) |
| 바티스트 | 치유 탄착점과 공격 사선을 함께 확보 / 고지대의 탈출 가치 평가 / 방어 수단에 맞춰 측면 체류 조절 | [baptiste.yaml](criteria/baptiste.yaml) |
| 브리기테 | 보호할 아군과 적 진입로 사이 연결 / 격려를 위해 감수한 노출 평가 / 방패 밀쳐내기의 도착 위치 | [brigitte.yaml](criteria/brigitte.yaml) |
| 독트린 | 중거리 지원과 구조받을 거리 / 강화 이동을 포함한 탈출 계획 / 고지대 진입 후 버틸 수 있는가 | [doctrine.yaml](criteria/doctrine.yaml) |
| 일리아리 | 본인·아군·태양석의 지원 연결 / 치료 광선이 필요한 순간의 거리 / 측면 압박 후 분출의 도착점 | [illari.yaml](criteria/illari.yaml) |
| 제트팩 캣 | 비행 높이보다 공중 엄폐 경로 / 연료를 고려한 왕복 경로 / 기동성과 실제 지원 공백의 균형 | [jetpack-cat.yaml](criteria/jetpack-cat.yaml) |
| 주노 | 치료가 실질적으로 연결되는 중거리 / 공중 체류 중 사선 관리 / 활공의 끝에 남는 지원 위치 | [juno.yaml](criteria/juno.yaml) |
| 키리코 | 측면 압박과 치료 복귀 조건 / 순보 대상이 실제로 안전한가 / 벽타기로 지원 각도와 퇴로 변경 | [kiriko.yaml](criteria/kiriko.yaml) |
| 라이프위버 | 구출받을 아군이 도착할 위치 확보 / 단상 위에서도 엄폐와 탈출 확인 / 전선 이동에 맞춘 후방 재배치 | [lifeweaver.yaml](criteria/lifeweaver.yaml) |
| 루시우 | 오라를 받을 아군과 자신의 역할 결정 / 벽타기의 엄폐 연결과 착지 / 후방 압박의 종료와 복귀 | [lucio.yaml](criteria/lucio.yaml) |
| 메르시 | 광선 유지와 본체 엄폐 / 수호천사의 착지점과 다음 경로 / 체공이 만드는 노출 관리 | [mercy.yaml](criteria/mercy.yaml) |
| 미즈키 | 오라 수혜와 측면 통제를 함께 평가 / 다이브를 피하며 아군과의 연결 유지 / 귀환 인형 위치의 안전성 | [mizuki.yaml](criteria/mizuki.yaml) |
| 모이라 | 아군을 치료할 위치와 안전한 접근 / 소멸 종료 위치의 안전성 / 측면 압박의 가치와 복귀 | [moira.yaml](criteria/moira.yaml) |
| 솜브라 | 지원형 솜브라의 교전 연결 / Cyberspace와 엄폐를 함께 활용 / 위치변환기 도착점과 지원 복귀 | [sombra.yaml](criteria/sombra.yaml) |
| 우양 | 아군 지원과 파도 경로를 열어 두기 / 이동·도약 이후의 엄폐와 합류 / 유도 공격 중 주변 확인 | [wuyang.yaml](criteria/wuyang.yaml) |
| 젠야타 | 위협이 닿기 전 엄폐 간 이동 / 짧은 측면 압박과 아군 구조 거리 / 구슬 유지와 본체 노출의 균형 | [zenyatta.yaml](criteria/zenyatta.yaml) |

## AI 평가 순서

1. 경기 모드·영웅·패치·선택 특전과 현재 HUD 상태를 확인한다. 정적 DB와 관측 현재 상태를 분리한다.
2. 장면의 준비·교전·추격·후퇴·재집결 단계와 목표물 접촉 필요를 확인한다.
3. 관측 사실을 먼저 기록한다. 화면에 아군이 안 보인다는 사실만으로 본인이 아군보다 앞에 있다고 단정하지 않는다.
4. 해당 영웅의 조건에 맞는 기준만 골라 기여·위협·실행 가능한 대안을 비교한다. 적 위치나 쿨타임이 화면 밖이면 unknown으로 둔다.
5. 예외와 필요한 근거를 확인한다. 쿨타임 사용 확인은 대상·명중·효과 도달·전술적 가치를 증명하지 않는다.
6. 관측 → 이유 → 가능한 다음 행동의 순서로 짧은 피드백을 쓴다. 동일한 진입의 위치·스킬 문제를 중복 감점하지 않는다.

좋은 신호/나쁜 신호는 등급 점수가 아니다. `excellent`(탁월)는 명확한 가치와 어려운 선택의 근거, `best`(최선)는 당시 선택지 비교, `blunder`(블런더)는 회피 가능한 큰 손실·위험과 대안의 근거가 필요하다. 이 구분은 검수 제안이며 현재 코드의 등급 정의를 변경하지 않는다. 핵심 정보 부족은 `withheld`(판단 보류)다. 실제 결과와 결정 당시의 판단을 구분한다. 검수 전 초안을 승인된 확정 판정으로 표시하지 않는다.

## 저장소 연결 방법과 현재 한계

현재 `owcoach/heroes.py`의 `active_criteria()`는 `deprecated`만 제외한다. **`draft`도 활성화되므로 이 폴더를 `analyzer/heroes/`로 그대로 복사하지 않는다.** 이 파일들은 `HeroProfile` 전체가 아니라 `criteria` 배열을 담은 검수용 조각이다. 이 변경은 런타임 로더·프롬프트·HUD 설정·승인된 아나 기준을 수정하지 않는다.

영웅 프로필에 반영할 때에는 다음 순서로 진행한다.

1. 기준을 코치가 실제 영상으로 검수하고 충돌·중복을 정리한다. 이번 자료는 학습 완료 모델이나 검증된 정답 데이터가 아니다.
2. `ability_refs`의 로컬 ID와 DB ID를 연결한다. 이것은 ID 매핑이며 HUD 관측 구현이 아니다. 실제 프로필의 `abilities`와 해당 영웅의 HUD 위치·효과 표식을 구현하고 검증한다.
3. 조건·예외는 이미 `description`과 `cannot_judge_when`에 포함되어 있다. 현재 `criteria_for_prompt()`가 전달하지 않는 새 필드에만 조건을 넣지 않는다.
4. 기존 `common.yaml`·코칭 프롬프트·후처리의 아나 중심 가정을 검토한다. 혼자 보이는 측면 플레이·공중 이동·오라 영웅을 동일한 후방 규칙으로 감점하지 않는다. 여기의 공통 기준은 기존 공통 파일을 대체하지 않는다.
5. 명확한 실수·타당한 예외·판단 불가 사례를 각각 실제 영상으로 수집한다. 같은 경기의 인접 클립을 학습·평가로 나눠 누출시키지 않는다. 영웅·맵·조합·패치별 오탐과 판단 보류율을 따로 점검한다.

아나 기존 기준과의 연결은 아래와 같다. 신규 항목을 별도 감점으로 중복 적용하지 않는다.

| 기존 기준 | 이번 초안 | 통합 검토 |
|---|---|---|
| `ana.positioning.backline_angle` / `cover_access` | `care_angle` | 지원 시야·엄폐·적 사선을 함께 확인 |
| `ana.positioning.follow_tank` | `rotation_window` | 따라갈 수 있는 경로와 무리한 진입 예외 추가 |
| `ana.positioning.disengage_on_pressure` | `dive_spacing` | 이탈 방향과 아군에게 도움받을 경로 확인 |
| `ana.positioning.face_team_while_moving` | 공통 P02·P08·P09 | 순간적인 카메라 회전과 실제 지원 단절 구분 |
| `ana.skill.sleep_dart_usage` | `ana.skill_usage.sleep_priority` | 사용 여부 외 위협과 후속 근거 확인 |
| `ana.skill.biotic_grenade_usage` | `grenade_pressure` / `grenade_rescue` | 공격·구조 목적별 조건 분리 |
| `ana.ultimate.nano_boost_usage` | `ana.ultimate_usage.nano_window` | 대상의 행동 가능성과 구조용 예외 확인 |

## 패치와 출처 처리

지원 영웅 목록과 스킬 ID는 저장소의 2026-10-15 SQLite 수집본을 따른다. 수집 날짜는 경기 패치가 아니다. 솜브라는 이 DB에 수록된 지원 역할 개편 후 키트이며 과거 공격 역할에 이 기준을 적용하지 않는다. 메르시의 Flash Heal 등 현재 DB에 있는 스킬을 기준으로 작성했다.

정확한 거리·피해량·치유량·쿨타임·지속시간은 이 초안에 상수로 넣지 않았다. 필요한 경우 `overwatch_stats`의 단위·조건·검증 상태·개정 링크와 경기 패치를 함께 조회한다. `review_required` 값은 확정 판단 근거에서 제외한다. 특히 독트린 Deliverance의 검토 항목을 해소하기 전 지속시간 기반 결론을 내리지 않는다.

커뮤니티 출처는 공개 의견이며 공식 전술 정답이 아니다. 영상·리플레이·외부 전사문은 직접 검토한 것으로 표시하지 않았다. 출처별 직접 확인 범위는 `sources.yaml`과 공통 원칙 문서에 구분했다. 각 YAML의 `source.type: editorial_synthesis`는 좋은/나쁜 신호·예외가 이 프로젝트를 위해 작성한 설계임을 나타낸다.

게임 데이터 출처: Overwatch Wiki contributors, **CC BY-NC-SA 3.0**. 고정 개정 URL·수집 시각·원본 해시는 `sources.yaml`에 보존했다. 원 데이터 이용 조건은 [LICENSE-DATA.md](../../data/overwatch_stats/LICENSE-DATA.md)를 따른다. 이 문서로 원 데이터의 이용 허용 범위를 확장하지 않는다.

## 검증

저장소 루트에서 다음 명령으로 YAML 스키마·전체 지원 영웅 포함·ID 중복·스킬 DB 연결·출처·예시 참조를 확인한다.

```sh
python analyzer/knowledge/support_positioning/validate.py
```

스키마 검증 통과는 전술적 정확성이나 영상 분석 정확도 검증을 의미하지 않는다. 실제 경기 평가·HUD 인식·회귀 영상 분석은 이번 자료 추가 작업에서 수행하지 않았다.

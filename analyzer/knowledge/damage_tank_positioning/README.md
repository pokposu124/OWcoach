# 딜러·탱커 포지셔닝 및 코칭 지식베이스

2026-10-18 · v0.1.0 · 일반 5대5 역할 고정 · 전 항목 `draft`

이 자료는 OWcoach의 지원가 기준팩에 이어 사용할 **검수용 지식베이스**다. 지원가 파일을 포함하거나 덮어쓰지 않는다. 분석기·프롬프트·기존 승인 기준을 변경하지 않는다.

| 구성 | 범위 | 기준 수 |
|---|---|---:|
| 딜러 공통 포지셔닝 | 역할 공통 원칙 | 12 |
| 탱커 공통 포지셔닝 | 역할 공통 원칙 | 12 |
| 딜러 개별 기준 | 23명 × 기본 6개 + 안란 부활 궁 | 139 |
| 탱커 개별 기준 | 15명 × 기본 6개 + D.Mon·D.Va 재탑승 궁 | 92 |
| 합계 | 포지셔닝 138 · 스킬 76 · 궁극기 41 | 255 |

영웅당 기본 구성은 **포지셔닝 3개, 위치 판단과 연결되는 핵심 스킬 2개, 궁극기 1개**다. 별도 부활·재탑승 궁극기는 추가 항목으로 분리했다. 모든 스킬·특전·상성·전장별 좌표를 빠짐없이 다룬 공략은 아니다.

## 파일 안내

- [딜러 공통 원칙](damage_common.md) / [탱커 공통 원칙](tank_common.md)
- [딜러 영웅별 상세 기준](damage_heroes.md) / [탱커 영웅별 상세 기준](tank_heroes.md)
- [영웅별 YAML](criteria/) / [딜러 공통 YAML](damage_common.yaml) / [탱커 공통 YAML](tank_common.yaml)
- [출처·DB 고정 개정·근거 수준](sources.yaml)
- [합성 경계 사례](review_examples.yaml): 실제 경기에서 관측한 정답이 아님
- [Claude Code 전달 지시](CLAUDE_CODE_HANDOFF.md)

## 영웅별 포지셔닝 초점

| 역할 | 영웅 | 핵심 위치 판단 | YAML |
|---|---|---|---|
| 딜러 | 안란 | 화염 공격을 연결할 접근 경로 / 돌진 종료 후 이탈 공간 / 화상 압박의 체류 시간 | [anran.yaml](criteria/anran.yaml) |
| 딜러 | 애쉬 | 엄폐 있는 중장거리 사격각 / 발각된 저격각의 재배치 / 충격 샷건 이후의 거리 | [ashe.yaml](criteria/ashe.yaml) |
| 딜러 | 바스티온 | 강습 형태를 활용할 사선 준비 / 강습 종료 후 재배치 / 큰 피격 면적을 줄이는 코너 | [bastion.yaml](criteria/bastion.yaml) |
| 딜러 | 캐서디 | 유효 거리에서 유지할 짧은 측면 / 측면 방어와 공격 기여의 균형 / 구르기 종료 위치 | [cassidy.yaml](criteria/cassidy.yaml) |
| 딜러 | 에코 | 공중 엄폐를 연결하는 공격 경로 / 접착 폭탄과 광선을 연결할 거리 / 비행 종료와 활공 착지 | [echo.yaml](criteria/echo.yaml) |
| 딜러 | 엠레 | 점사 사격과 엄폐의 반복 / 패시브 회복을 위한 안전한 단절 / 강화 기동 중 복귀 경로 | [emre.yaml](criteria/emre.yaml) |
| 딜러 | 프레야 | 상승 후 조준할 각도와 엄폐 / 조준 갱신과 탈출 수단 비교 / 발각된 사격각 교체 | [freja.yaml](criteria/freja.yaml) |
| 딜러 | 겐지 | 벽타기로 진입 전 위치 준비 / 질풍참 종료와 초기화 의존 / 근접 압박의 체류 한계 | [genji.yaml](criteria/genji.yaml) |
| 딜러 | 한조 | 엄폐 있는 사격각과 근접 위협 / 벽타기 후 고지대의 실용성 / 이단뛰기의 회피 방향 | [hanzo.yaml](criteria/hanzo.yaml) |
| 딜러 | 정크랫 | 반사 공격을 활용할 엄폐 / 기습 거리와 빠져나올 길 / 지뢰 이동의 착지 위험 | [junkrat.yaml](criteria/junkrat.yaml) |
| 딜러 | 메이 | 근접 압박과 고드름 사격의 거리 / 빙벽이 바꾸는 아군과의 연결 / 급속 빙결 종료 후 안전 | [mei.yaml](criteria/mei.yaml) |
| 딜러 | 파라 | 건물을 이용한 공중 사선 제한 / 연료·이동기를 고려한 착지 / 측면 기습과 아군 압박 동시성 | [pharah.yaml](criteria/pharah.yaml) |
| 딜러 | 리퍼 | 근접 화력을 위한 엄폐 접근 / 그림자 밟기의 도착 위험 / 망령화 종료 후의 퇴로 | [reaper.yaml](criteria/reaper.yaml) |
| 딜러 | 시온 | 근거리 공격과 팀 진입의 동시성 / 바이크의 진입과 하차 위치 / 회피 후 추가 생명력에 대한 의존 | [shion.yaml](criteria/shion.yaml) |
| 딜러 | 시에라 | 연속 사격과 엄폐 노출의 비용 / 앵커 드론의 도착 지점 / 표식 대상에 대한 실제 사격 연결 | [sierra.yaml](criteria/sierra.yaml) |
| 딜러 | 소전 | 충전 확보와 유효 표적 사선 / 슬라이드·도약 종료 지점 / 측면 체류와 이동기 상태 | [sojourn.yaml](criteria/sojourn.yaml) |
| 딜러 | 솔저: 76 | 고지대에서 지속 가능한 사격 / 질주로 각도 갱신 / 생체장에 묶이지 않는 위치 | [soldier-76.yaml](criteria/soldier-76.yaml) |
| 딜러 | 시메트라 | 광선 유지와 코너 압박 / 순간이동기 양쪽의 안전 / 구체 견제에서 광선으로 전환 | [symmetra.yaml](criteria/symmetra.yaml) |
| 딜러 | 토르비욘 | 본체와 포탑의 교차 압박 / 포탑 수리와 본체 기여 비용 / 과부하 종료 전 위치 조절 | [torbjorn.yaml](criteria/torbjorn.yaml) |
| 딜러 | 트레이서 | 아군 교전과 겹치는 측면 공격 / 점멸을 포함한 왕복 경로 / 시간 역행이 돌려놓을 위치 | [tracer.yaml](criteria/tracer.yaml) |
| 딜러 | 벤데타 | 검 사거리까지 접근할 엄폐 / 검 추적 이동의 종료 공간 / 전방 방어와 측면 노출 | [vendetta.yaml](criteria/vendetta.yaml) |
| 딜러 | 벤처 | 잠복 진입 전 가까운 엄폐 확보 / 잠복 종료 지점과 적 대기 / 임시 보호막과 교전 종료 | [venture.yaml](criteria/venture.yaml) |
| 딜러 | 위도우메이커 | 장거리 사선과 노출 대상 / 발각된 위치의 재사용 비용 / 갈고리 이후 착지와 탈출 | [widowmaker.yaml](criteria/widowmaker.yaml) |
| 탱커 | D.Mon | 메카 근접 압박과 아군 지원 / 추진 연료를 고려한 왕복 / 메카 파괴 후 파일럿 위치 | [dmon.yaml](criteria/dmon.yaml) |
| 탱커 | 도미나 | 중거리 압박과 후퇴 코너 / 분절 방벽의 빈틈과 본체 위치 / 벽 충돌을 유도할 지형 | [domina.yaml](criteria/domina.yaml) |
| 탱커 | 둠피스트 | 진입 전 근접 엄폐에서 준비 / 이동기 순서와 실제 퇴로 / 후방 구조와 적 압박의 선택 | [doomfist.yaml](criteria/doomfist.yaml) |
| 탱커 | D.Va | 고지대 거부와 아군 활용 / 부스터 종료 위치와 메카 보존 / 적 각도 견제·다이브·후방 보호 선택 | [dva.yaml](criteria/dva.yaml) |
| 탱커 | 해저드 | 지형을 이용한 짧은 도약 진입 / 벽을 사이에 둔 공격과 생존 / 가드의 전방과 측면 관리 | [hazard.yaml](criteria/hazard.yaml) |
| 탱커 | 정커퀸 | 근접 압박이 가능한 코너 교환 / 상처 회복과 실제 피해 비교 / 외침 종료 후 위치 | [junker-queen.yaml](criteria/junker-queen.yaml) |
| 탱커 | 마우가 | 큰 몸체의 노출과 사격 공간 / 돌진 종료 후 치료 연결 / 심장 효과 종료 후 자리 조절 | [mauga.yaml](criteria/mauga.yaml) |
| 탱커 | 오리사 | 방어 자원을 아끼는 코너 유지 / 탱커 대치와 주변 각도 통제 / 방어기 공백 전에 위치 조절 | [orisa.yaml](criteria/orisa.yaml) |
| 탱커 | 라마트라 | 형태에 맞는 교전 거리 / 네메시스 종료 후 지원 공간 / 방어 방향과 아군 기여 | [ramattra.yaml](criteria/ramattra.yaml) |
| 탱커 | 라인하르트 | 망치가 닿는 코너를 단계적으로 확보 / 모서리를 넘기 전 아군 연결 / 돌진의 끝과 취소 가능 위치 | [reinhardt.yaml](criteria/reinhardt.yaml) |
| 탱커 | 로드호그 | 갈고리로 데려올 공간 확보 / 갈고리 측면각과 팀 공백 / 회복 중 이동과 적 방해 | [roadhog.yaml](criteria/roadhog.yaml) |
| 탱커 | 시그마 | 초구체 유효 거리와 접근 대응 / 짧은 측면으로 사선 분산 / 방벽·흡수 소모 후 위치 | [sigma.yaml](criteria/sigma.yaml) |
| 탱커 | 윈스턴 | 도약 전 준비 위치와 체력 / 진입과 다음 도약 사이 생존 / 점프할 표적과 분리할 지원선 | [winston.yaml](criteria/winston.yaml) |
| 탱커 | 레킹볼 | 정찰·준비·타격의 연결 / 통과 후 회복·재진입 경로 / 교란이 팀 기여로 이어지는가 | [wrecking-ball.yaml](criteria/wrecking-ball.yaml) |
| 탱커 | 자리야 | 방벽 주기와 코너 전진 / 광선이 닿는 경로로 접근 / 아군 방벽을 줄 시야와 거리 | [zarya.yaml](criteria/zarya.yaml) |

## AI가 판정할 때 지킬 조건

1. 관측 사실을 먼저 기록한다. 아군이 화면에 없다는 것과 아군 지원이 불가능하다는 것은 다르다. 적이 안 보인다고 없다고 단정하지 않는다.
2. 준비·교전·추격·철수·재집결 단계와 목표물 접촉 필요를 확인한다. 한 프레임만 보고 장시간 고립·무기여라고 판단하지 않는다.
3. 적용 조건에 맞는 공통·영웅 기준만 불러온다. 현재 영웅·형태·자원·아군 조합에 맞는 후보 행동을 비교한다.
4. 좋은/나쁜 신호를 예외와 함께 해석한다. 탈출기 사용 확인은 안전한 도착을, 명중은 유효한 후속을 자동으로 증명하지 않는다.
5. 대안은 실제로 접근 가능해야 한다. 화면 밖 계단·회복팩·적 쿨타임을 만들어내지 않는다. 자기 HUD도 가림·시각 효과가 있으면 unknown으로 둔다.
6. 사망·처치 결과와 결정 당시의 판단을 분리한다. 확실한 목표물·교환 이득을 생존만으로 평가하지 않는다. 한 진입에서 발생한 위치·스킬 문제는 원인 중심으로 묶어 중복 감점을 피한다.
7. 피드백은 관측 → 위험 또는 이득 → 실행 가능한 다음 행동 순서로 작성한다. 시간은 `n분 n초`로 표시한다.

서비스 분류는 `positioning`, `skill_usage`, `ultimate_usage`만 사용한다. 별도 에임 분류는 만들지 않는다. 등급은 저장소의 `excellent` / `best` / `blunder` / `withheld`를 유지한다. 여기의 신호를 고정 점수로 환산하거나 예시의 내부 검토 상태를 등급으로 자동 변환하지 않는다.

## 출처·패치와 한계

영웅 목록·스킬 ID·메커니즘은 저장소의 **2026-10-15 SQLite 수집본**을 기준으로 했다. 수집일은 경기 패치 번호가 아니다. 딜러 목록에 솜브라가 없는 이유는 이 수집본에서 지원 역할로 분류하기 때문이다. 과거 영상에는 당시 역할·키트를 확인해야 한다. 리퍼의 Dire Triggers, 로드호그의 Trash Compactor 등도 이 수집본 기준이다.

피해·치유량, 사거리, 쿨타임, 지속시간, 특전 수치를 모델 기억으로 넣지 않았다. 필요한 숫자는 `overwatch_stats` 조회 결과의 단위·조건·검증 상태·review_required·고정 개정 링크를 함께 사용한다. `review_required` 항목과 미확인 패치·형태·특전에 의존하는 판정은 보류한다. HUD의 현재 체력·남은 자원과 정적 DB 값을 구분한다.

자료의 전술 조건·좋은 신호·나쁜 신호·예외는 **작성자 설계**다. 커뮤니티 직접 근거가 있는 영웅도 일부 원칙을 참고했을 뿐 전체 세부 기준이 검증된 것은 아니다. 영웅별 자료를 충분히 확보하지 못한 경우 `role_principles_and_mechanics_only_editorial`로 표시했다. 개인 의견·작성자 자기소개 랭크·추천 수를 정확성 보증으로 사용하지 않았다. 연결 영상·리플레이는 직접 분석하지 않았다.

실제 경기의 코치 검수, 모델 학습·파인튜닝, HUD 인식, 코칭 정확도 평가는 수행하지 않았다. 6대6·스타디움·특수 모드에는 별도 검증 없이 적용하지 않는다.

게임 데이터 출처는 **Overwatch Wiki contributors / CC BY-NC-SA 3.0**이다. `sources.yaml`에 영웅별 고정 개정 URL·수집 시각·해시·라이선스를 보존했다. 원 데이터 이용 조건은 [LICENSE-DATA.md](../../data/overwatch_stats/LICENSE-DATA.md)를 따른다. 이 자료는 원 데이터의 이용 허용 범위를 확장하지 않는다.

## 저장소 통합

ZIP 내부 경로를 유지해 저장소 최상위에 풀면 `analyzer/knowledge/damage_tank_positioning/`에 들어간다. 파일은 `HeroProfile` 전체가 아닌 **Criterion 배열을 담은 검수용 조각**이다.

기존 로더의 `active_criteria()`는 `deprecated`만 제외하므로 **draft도 자동 분석에 사용될 수 있다**. 따라서 파일을 `analyzer/heroes/`에 그대로 복사하지 않는다. `approved`로 일괄 변경하지도 않는다.

실제 연결 시에는 코치 검수 뒤에 기준을 선별하고, 영웅 프로필·스킬 ID·HUD 위치·대상/효과 표식을 구현해야 한다. `ability_refs`는 스킬 ID 대응표이지 HUD 관측 구현이 아니다. 현재 코드의 아나 중심 공통 기준·프롬프트·후처리 가정을 점검해야 한다. 특히 독립적인 딜러 측면과 탱커 선진입을 아나의 후방 기준으로 감점하지 않는다.

조건·예외·필수 근거는 현재 프롬프트가 전달하는 `description`과 `cannot_judge_when`에 넣었다. 메타데이터만 추가해 놓고 모델에 전달되지 않는 상황을 피한다. 현재 `criteria_for_prompt()`의 상태·출처 전달 범위도 통합할 때 확인한다.

실제 영상 검수는 명확한 이득·명확한 실수·타당한 예외·판단 불가를 모두 포함한다. 같은 경기의 인접 클립을 학습·평가로 분리하지 않는다. 영웅·맵·조합·패치별 오탐과 판단 보류율을 따로 확인한다.

## 검증 명령

저장소 루트에서:

```sh
python analyzer/knowledge/damage_tank_positioning/validate.py
```

검증 범위는 `Criterion` 필드·지원 역할 외 영웅 포함·ID 중복·스킬 DB 연결·출처·예시 참조다. 스키마 통과는 전술 또는 모델 정확도 검증이 아니다.

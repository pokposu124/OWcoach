# OWcoach

오버워치 플레이 영상(HUD가 보이는 본인 1인칭, 최대 5분)을 넣으면 AI가 시간대별로 포지셔닝·생존·스킬 사용 피드백을 주는 서비스.

**현재 단계: 1단계 — 로컬 영상 분석 프로토타입** (`analyzer/`). 웹 UI, 클라우드 저장소, 작업 큐는 실제 영상 분석을 검증한 뒤 추가한다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | 영상 전처리 및 AI 분석 프로토타입 (로컬 파일 → 결과 JSON) | 구현됨. 실제 Gemini(`gemini-3.6-flash`) 호출로 끝까지 실행 확인(아나 영상 1개, 1280x720) |
| 2 | 실제 영상으로 분석 결과 검수 | 진행 중. 영상 1개로 검수: 팀·영웅 판별, HUD 스킬 사용 대조, 나노 대상 판별은 실제와 일치 확인. 모델의 스킬 사용 기록은 약 절반이 HUD와 불일치(코드가 걸러냄). 코칭 기준 코치 검수 전이라 단정 표시 0 |
| 3 | 업로드·분석 상태·결과 화면 (Next.js) | 미착수 |
| 4 | 재시도·비용 기록·삭제 | 단계별 캐시/재시도·사용량 기록은 로컬에 있음. 웹/저장소 연동은 미착수 |

## 구조

```
analyzer/
  owcoach/                공통 분석 코드 (영웅 무관)
    media.py              ffprobe/ffmpeg: 입력 확인, 프록시, 구간 자르기, 근거 프레임 추출
    pipeline.py           5단계 파이프라인, 단계별 저장·캐시·잠금, 사용량·비용
    postprocess.py        후보 장면 계획, 원본 시간 환산, 검증, 단정 제한, 중복 제거
    providers/            AI 제공자 분리 (base 인터페이스, gemini, mock)
    schemas.py            단계별 모델 응답 스키마 + 최종 결과 스키마
    report.py             코치 검수용 HTML 리포트
  config/analysis.yaml    샘플링 간격, 앞뒤 분석 범위, 개수 제한 등 모든 조정값
  config/pricing.yaml     확인된 단가만 입력 (비어 있으면 비용 미계산)
  heroes/ana.yaml         영웅별 코칭 기준 (초안, 코치 검수 필요)
  prompts/*.md            버전이 붙은 프롬프트
  schemas/analysis_result.schema.json   결과 JSON Schema (python -m owcoach schema 로 생성)
docs/COACH_REVIEW.md      코치 검수 방법
```

### 첫 지원 영웅

요청서에 영웅 이름이 비어 있어(`[영웅 이름]`) **아나(ana)** 로 초안을 만들었다. 수면총·생체 수류탄처럼 사용 판단이 화면에 드러나는 스킬과, 엄폐·생존 판단이 핵심이라 포지셔닝/생존/스킬 사용 세 범위를 모두 검증하기 좋다고 판단했다. 다른 영웅으로 시작하려면 `heroes/ana.yaml` 형식대로 `heroes/<id>.yaml` 을 만들고 `--hero <id>` 로 실행하면 된다. 공통 코드는 바꿀 필요가 없다.

## 분석 단계

1. **입력 확인** — ffprobe로 길이(≤300초), 해상도(≥1280x720), 형식, 크기 확인. 불충족 시 한국어 사유와 함께 중단.
2. **전체 흐름 분석** — 원본과 타임라인이 같은 720p 프록시를 `overview.fps`(기본 1fps)로 분석. 영웅 일치 여부, HUD 판독 가능 여부, 사건 타임라인, 후보 장면(최대 `max_candidates`).
3. **후보 장면 재분석** — 후보 구간을 `pre_seconds`/`post_seconds` 만큼 넓혀 FFmpeg로 잘라내고 `detail.fps`(기본 4fps)로 다시 분석. 모델은 클립 기준 시간으로 답하고, 코드가 `구간 시작 + t` 로 **원본 시간으로 환산**한다. 구간 밖 시간은 버린다.
3b. **HUD 대조 (AI 없음)** — 상세 분석이 기록한 스킬/궁극기 사용(`ability_events`)마다, 원본 영상의 HUD 고정 영역(스킬 칸 2개, 궁극기 원판)에서 "준비됨 → 사용됨" 전환이 그 시각 ±`search_seconds` 안에 있는지 픽셀로 확인해 `verified / contradicted / unverifiable` 로 표시한다(`owcoach/hud.py`, 설정 `hud_check`). 나노 강화제처럼 대상이 있는 스킬은, 사용이 확인된 직후 아군 팀 목록 칸의 초상화 위에 뜨는 흰 강화 화살표를 찾아 대상(`target_check`)을 판별한다(화살표가 안 보이면 '대상 확인 불가'). 숫자(쿨다운 초·궁극기 %)는 읽지 않는다 — OCR(rapidocr)은 이 HUD 숫자를 자주 놓쳐 쓰지 않았다. 영역 좌표는 기본 HUD 배치의 1280x720 녹화 1개로만 보정되어 있다.
4. **코칭 기준 적용** — 영상 없이 관찰 기록 + 영웅 기준만 텍스트로 넘겨 observation / assessment / suggestion 을 분리해 작성.
5. **검증·정리 (AI 없음)** — 스키마 검증, 영상 길이·분석 구간 범위 확인, 근거 프레임을 원본에서 실제로 추출해 존재 확인, 존재하지 않는 기준 제거, 중복 제거, 최대 5개 선택, 근거 없는 과제 제거.

단정적 피드백(`display_mode: assertive`)이 되려면: 유효한 코칭 기준 ≥1, 추출된 근거 프레임 ≥1, `judgement != withheld`, 결과(사망)만을 근거로 하지 않음, 확신도 ≥ `min_confidence_for_assertive`, 불확실성 high 아님, 그리고 기본 설정에서는 **코치 검수된(approved) 기준**(`require_approved_criteria_for_assertive: true`)과 **HUD로 확인된 스킬/궁극기 사용 ≥1, HUD 불일치 0**(`require_hud_verification_for_assertive: true`). 하나라도 어기면 `tentative`(참고·보류)로 표시하고 `display_reasons` 에 이유를 남긴다. 모델이 스스로 매기는 확신도는 첫 실제 실행에서 틀린 항목도 1.0이어서 단독 근거로 쓰지 않는다. HUD 조건은 **스킬 사용 피드백에만** 적용한다. 포지셔닝·생존은 별개 차원으로, HUD 대조를 붙이지도 근거로 쓰지도 않으며 `verification: model_observation`(모델 관찰, 코드 미확인)으로 표시된다. 스킬 사용 피드백의 `hud_checks` 에 대조 결과가 남는다. 사용자에게 보이는 시간은 모두 "n분 n초"(1분 미만은 "n초")로 표기하며, 모델이 쓴 문장의 "132.75초" 같은 표기도 변환한다(JSON의 숫자 필드는 초 단위 유지).

전체 흐름 분석 전에 **팀 판별** 단계(`team`)가 원본에서 프레임 몇 장(`team.frame_fractions`)을 골라 팀 목록·플레이어 HUD·킬피드 영역만 잘라 확대한 이미지로 아군/적 판별 근거(`team_identification`)와 팀 목록 초상화로 판독한 영웅 목록(`roster`)을 기록한다. 영상 전체(저해상도)로 읽게 했더니 팀 목록 이름을 지어냈기 때문이다(실측). **영웅은 모델이 아니라 코드가 판독한다**: 팀 목록의 초상화 영역을 참고 초상화(`config/hero_portraits/`, 사용자 제공 영웅 선택 화면 초상화 54종)와 여러 배율로 이미지 대조(정규화 상관)해 가장 잘 맞는 영웅을 고르고, 점수·2위와의 차이로 확신도를 매기며 낮으면 `불명`으로 둔다(`owcoach/portraits.py`). 모델은 참고표를 줘도 실행마다 다른 영웅을 high 확신도로 답했다. 결과는 `result.json` 의 `team` 에 남고, 전체 흐름·상세 분석 프롬프트에 전달된다. 팀 색은 사용자 설정으로 바뀔 수 있어 "빨강=적"을 가정하지 않는다.

### 결과 JSON

`runs/<run_id>/result.json`. 피드백 항목 필수 필드: `start_time_seconds`, `end_time_seconds`, `category`, `observation`, `assessment`, `suggestion`, `evidence_frame_timestamps`, `uncertainty`, `hero`, `patch_version` + `judgement`, `confidence`, `criterion_ids`, `criteria_review_status`, `display_mode`, `display_reasons`, `evidence_frames`. 전체 스키마는 `analyzer/schemas/analysis_result.schema.json`.

결과에는 `analysis_mode`(`real`/`mock`), 모델·프롬프트·영웅 기준·설정 버전, 단계별 처리 시간, 호출별 토큰 사용량(입력/출력/thinking/모달리티별, 캐시 재사용 여부), 비용(확인된 단가가 있을 때만, 항상 추정 표시)이 기록된다.

### 재실행과 중복 비용 방지

- 작업 ID(`--run-id`, 기본값 `영상 sha256 앞 12자-영웅`)별로 `runs/<run_id>/stages/*.json` 에 단계 결과를 저장한다.
- 같은 작업을 다시 실행하면 입력 지문(모델·프롬프트 버전·설정·영웅 기준·상위 단계)이 같은 단계는 **API를 다시 호출하지 않고** 재사용한다. 실패한 단계부터 이어서 실행된다.
- 설정을 바꾸면 영향받는 단계만 다시 실행된다. `--rerun overview,detail,coaching` 으로 강제 재실행.
- 같은 작업 ID를 동시에 실행하면 파일 잠금으로 막는다.
- `run.json` 에 단계별 상태(`running/done/failed`)와 실패 이유가 남는다.

## 실행 방법

필요: Python 3.11+, FFmpeg/ffprobe.

```bash
cd analyzer
pip install -r requirements.txt

python -m owcoach heroes                      # 지원 영웅 목록
python -m owcoach probe /path/to/video.mp4    # 입력 조건만 확인 (API 호출 없음)

# 모의 실행: API 호출 없음. 결과에 analysis_mode=mock, 경고 배너 표시
python -m owcoach analyze /path/to/video.mp4 --hero ana --provider mock

# 실제 분석 (Gemini)
export GEMINI_API_KEY=...
export OWCOACH_GEMINI_MODEL=<공식 문서에서 확인한 모델 ID>
python -m owcoach analyze /path/to/video.mp4 --hero ana --patch "<게임 패치 버전>"

python -m owcoach report runs/<run_id>        # 검수 리포트 재생성
python -m pytest                              # 테스트 (FFmpeg로 합성 영상을 만들어 mock으로 전체 흐름 검증)
```

출력: `analyzer/runs/<run_id>/result.json`, `report.html`(검수 리포트), `evidence/*.jpg`(근거 프레임), `media/`(프록시·클립), `stages/`(단계별 원응답·사용량).

### 환경 변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | 실제 분석 시 | Google AI Studio API 키 (`GOOGLE_API_KEY` 도 인식) |
| `OWCOACH_GEMINI_MODEL` | 실제 분석 시 | 모델 ID. 코드에 기본값 없음. [공식 모델 목록](https://ai.google.dev/gemini-api/docs/models)에서 영상 입력 지원 모델을 확인해 입력 |
| `OWCOACH_CONFIG` | 선택 | 다른 analysis.yaml 경로 |
| `OWCOACH_PRICING` | 선택 | 다른 pricing.yaml 경로 |

### Gemini 사용 방식

공식 `google-genai` SDK(2.30.0에서 확인): `client.files.upload` → `files.get` 으로 `ACTIVE` 될 때까지 대기 → `Part(file_data=..., video_metadata=VideoMetadata(fps=...))` 와 프롬프트로 `models.generate_content`(`response_mime_type="application/json"`, `response_json_schema`) → 분석 후 `files.delete` 로 업로드 파일 즉시 삭제. 429/5xx는 지수 백오프로 재시도, 스키마 불일치 응답은 오류 내용을 붙여 1회 재요청.

공급자를 바꾸려면 `owcoach/providers/base.py` 의 `AnalysisProvider`(`analyze_video`, `generate_json`)를 구현하고 `providers/__init__.py` 에 등록하면 된다.

### 데이터 사용

- 업로드 영상은 모델 학습에 쓰지 않는다. 이 코드는 학습·파인튜닝 경로가 없다.
- 단, **Gemini API 무료 등급은 입력 데이터가 Google 제품 개선에 사용될 수 있다**(Google 약관 기준). 실제 사용자 영상에는 결제가 연결된 유료 등급 키를 사용하고, 최신 약관을 직접 확인할 것.
- Gemini Files API에 올린 파일은 분석 직후 삭제한다(`delete_uploaded_files: true`).
- 로컬 산출물은 `runs/<run_id>/` 디렉터리를 지우면 모두 삭제된다. 보관 기간 설정과 삭제 API는 웹 단계에서 구현한다.

## 다음 확장 시 제안 (지금은 구현하지 않음)

OCR·객체 탐지는 넣지 않았다. 실제 영상 검수에서 같은 인식 오류(예: 쿨다운 숫자, 궁극기 % 오독)가 반복되면 그 항목만 HUD 고정 영역 크롭 + OCR 같은 전용 처리를 추가하는 것을 제안한다. 근거: 검수 리포트의 "관찰 사실: 틀림" 코멘트.

## 실제 영상 검수에서 확인된 것 (아나, 1280x720, 1개 영상)

- 영상 전체를 1fps 저해상도로 본 모델은 팀 목록 이름을 지어냈다 → 팀 목록을 확대한 정지 이미지로 따로 판독(이름·팀 정확).
- 모델은 영웅 초상화를 실행마다 다르게, high 확신도로 틀렸다(참고표를 줘도) → 코드 초상화 대조로 10명 모두 일치.
- 모델이 기록한 스킬/궁극기 사용 시각은 약 절반이 틀렸다(쿨다운 중 사용, 사용 안 한 궁극기 등) → HUD 픽셀 대조로 `contradicted` 처리, 코칭 근거에서 제외.
- 나노 강화제 대상: 모델은 실행마다 다른 대상을 적었다 → 팀 목록 강화 화살표로 판별(136초 → 파라, 일치).
  대상이 받자마자 사망해 궁이 끊기면 화살표가 뜨지 않으며, 이때는 '대상 확인 불가'가 된다(249.5초 → 솔저, 실제로 끊김).
- 모델이 매기는 확신도는 틀린 항목에도 0.95~1.0이라 단정 표시 근거로 쓰지 않는다.
- HUD·초상화 좌표는 이 영상의 기본 HUD 배치에서만 보정했다. 다른 해상도·HUD 설정 영상으로 추가 검증 필요.

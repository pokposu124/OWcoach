"""Schemas for model responses (per stage) and for the final analysis result.

Model-response schemas are passed to the provider as JSON Schema (structured output) and the
responses are validated again here, so a provider that ignores the schema still cannot leak
malformed data into later stages.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal["skill_usage", "positioning", "ultimate_usage"]
Visibility = Literal["clear", "partial", "unclear"]
Level = Literal["low", "medium", "high"]

RESULT_SCHEMA_VERSION = "1.4.0"


# ---------------------------------------------------------------- stage 2: overview (model output)

class TimelineEvent(BaseModel):
    time_seconds: float
    event: str = Field(description="화면에서 확인된 사건 (한국어)")
    visible_evidence: str = Field(description="그 사건을 확인한 화면 근거")


class SceneCandidate(BaseModel):
    start_time_seconds: float
    end_time_seconds: float
    category: Category
    reason_observed: str = Field(description="후보로 고른 이유. 관찰 사실만")
    priority: int = Field(description="1(낮음)~5(높음)")


class TeamIdentification(BaseModel):
    player_name: str = Field(description="좌하단 HUD의 플레이어 이름. 읽지 못하면 빈 문자열")
    ally_side: Literal["left", "right", "unknown"] = Field(description="상단 팀 목록 중 플레이어 이름이 있는 쪽")
    ally_color: str = Field(description="이 영상에서 아군 표시 색 (예: 빨강, 파랑). 모르면 '불명'")
    enemy_color: str = Field(description="이 영상에서 적 표시 색. 모르면 '불명'")
    evidence: str = Field(description="판단 근거 (팀 목록, 킬피드 등 화면에서 본 것)")


class RosterEntry(BaseModel):
    slot: int = Field(description="상단 팀 목록에서 왼쪽 끝부터 1~10 (왼쪽 팀 1~5, 오른쪽 팀 6~10)")
    player_name: str = Field(description="상단 팀 목록에 표시된 플레이어 이름")
    team: Literal["ally", "enemy", "unknown"]


class KillFeedEntry(BaseModel):
    time_seconds: float = Field(description="그 줄이 처음 보인 이미지의 시각(원본 기준 초)")
    killer_name: str = Field(description="처치한 사람 이름. 없으면 빈 문자열")
    victim_name: str


class KillFeedResponse(BaseModel):
    entries: list[KillFeedEntry]


class KillerResponse(BaseModel):
    killer_name: str = Field(description="플레이어를 처치한 사람의 이름(킬피드·'○○에게 죽음' 그대로). 못 읽으면 빈 문자열")
    evidence: str


class TeamResponse(BaseModel):
    team_identification: TeamIdentification
    roster: list[RosterEntry] = Field(description="상단 팀 목록의 플레이어별 팀·영웅 (최대 10명)")


class OverviewResponse(BaseModel):
    hero_confirmed: bool
    hero_evidence: str
    hud_readable: Literal["yes", "partial", "no"]
    video_quality_notes: str
    timeline: list[TimelineEvent]
    candidates: list[SceneCandidate]


# ---------------------------------------------------------------- stage 3: detail (model output)

class DetailObservation(BaseModel):
    time_seconds: float
    end_time_seconds: float | None = None
    fact: str
    visibility: Visibility


class HudReading(BaseModel):
    time_seconds: float
    item: Literal["health", "ability_cooldown", "ultimate_charge", "ammo", "other"]
    value: str
    confidence: Level


class AbilityEvent(BaseModel):
    time_seconds: float = Field(description="플레이어가 스킬/궁극기를 사용한 시각")
    ability_id: str = Field(description="스킬 정보에 나온 id")
    target: str = Field(description="대상 플레이어 이름(머리 위 이름·화면 메시지로 확인). 모르면 빈 문자열")
    evidence: str = Field(description="사용을 확인한 화면 근거")


class MomentResponse(BaseModel):
    target: str = Field(description="스킬을 쓴 대상·방향. 모르면 '불명'")
    counter_active: Literal["yes", "no", "unclear"]
    counter_name: str = Field(description="켜져 있던 대응 스킬 이름. 없으면 빈 문자열")
    counter_evidence: str = Field(description="대응 스킬 판단의 화면 근거")
    result: str = Field(description="사용 직후 결과")


class SituationSample(BaseModel):
    """What a coach checks every few seconds (first Ana video coach feedback: follow the tank, face the
    team while moving, payload, enemy D.Va / Hana)."""
    time_seconds: float
    tank: str = Field(description="아군 탱커의 위치: '앞 멀리'(시야·치유가 닿기 어려움) / '앞 가까이' / '옆' / '뒤' / '안 보임'")
    tank_name: str = Field(description="그 탱커의 플레이어 이름 또는 영웅. 모르면 빈 문자열")
    player_moving: Literal["yes", "no", "unclear"] = Field(description="플레이어가 자리를 옮기는 중인지")
    facing_allies: Literal["yes", "no", "unclear"] = Field(description="화면에 아군이 보이는 방향을 보고 있는지 (이동 중 본대를 등지면 no)")
    payload: str = Field(description="화물: '전진' / '멈춤' / '후진' / '안 보임' / '해당 없음'")
    enemy_dva: str = Field(description="적 D.Va: '메카' / '송하나(본체)' / '없음·안 보임' (적에 D.Va가 없으면 '해당 없음')")
    player_vs_team: str = Field(default="불명", description="아군 본대 대비 플레이어 위치: '후방' / '같은 선' / '앞' / '불명'")
    allies_visible: int = Field(default=-1, description="화면에 보이는 아군 수 (모르면 -1)")
    danger: str = Field(default="없음", description="위험: '없음' / '피격 중' / '근접한 적' (여럿이면 쉼표로). 체력이 낮아도 앞에 적이 없으면 '없음'")
    low_health: bool = Field(default=False, description="체력이 절반 이하인지 (위험 판단과 별개)")
    note: str = Field(description="코칭에 쓸 만한 그 순간의 상황 한 줄 (예: 시그마가 혼자 앞으로 나감). 없으면 빈 문자열")


class DetailResponse(BaseModel):
    observations: list[DetailObservation]
    situation_track: list[SituationSample] = Field(description="약 3초마다 기록한 상황 (탱커 위치, 시선, 화물, 적 D.Va)")
    hud_readings: list[HudReading]
    ability_events: list[AbilityEvent] = Field(description="플레이어 본인의 스킬/궁극기 사용 (기본 사격 제외)")
    not_determinable: list[str]
    outcome: str
    key_moment_times: list[float]


# ---------------------------------------------------------------- stage 4: coaching (model output)

class CoachingItem(BaseModel):
    scene_id: str
    start_time_seconds: float
    end_time_seconds: float
    category: Category
    observation: str = Field(description="관찰 기록에 있는 사실만")
    assessment: str = Field(description="기준을 적용한 판단")
    suggestion: str = Field(description="다음에 할 구체적 행동")
    game_rule: str = Field(default="", description="판단에 쓴 게임 규칙·수치 (게임 수치 표의 값을 단위·조건과 함께). 없으면 빈 문자열")
    criterion_ids: list[str]
    grade: Literal["excellent", "best", "blunder", "withheld"] = Field(
        description="excellent=탁월, best=최선, blunder=블런더, withheld=근거 부족으로 판단 보류")
    matched_sign: str = Field(description="등급의 근거가 된 기준의 good_signs(탁월·최선) 또는 bad_signs(블런더) 문구를 그대로 옮김. withheld 면 빈 문자열")
    confidence: float = Field(description="0.0~1.0")
    uncertainty: str = Field(description="불확실한 점. 없으면 빈 문자열")
    uncertainty_level: Level
    relies_on_outcome_only: bool
    evidence_frame_timestamps: list[float]


class CoachingActionItem(BaseModel):
    task: str
    why: str
    related_scene_ids: list[str]


class CoachingResponse(BaseModel):
    items: list[CoachingItem]
    action_items: list[CoachingActionItem]


# ---------------------------------------------------------------- final result

class HudCheck(BaseModel):
    time_seconds: float = Field(description="모델이 말한 사용 시각 (원본 기준)")
    ability_id: str
    hud_slot: str | None
    status: Literal["verified", "contradicted", "unverifiable", "not_checked"]
    hud_use_time_seconds: float | None = None
    note: str
    claimed_by_model: str = Field(description="모델이 기록한 근거/대상")
    target_check: dict | None = Field(default=None, description="대상 판별 결과 (나노 강화제 등: 팀 목록 칸 강화 화살표)")
    moment_check: dict | None = Field(default=None, description="사용 순간 확인 (대상, 적 대응 스킬 활성 여부, 결과)")
    death_after_seconds: float | None = Field(default=None, description="무력화 스킬 사용 후 플레이어가 죽기까지 걸린 시간(초)")
    ally_health: dict | None = Field(default=None, description="사용 순간 아군 체력 (팀 목록 체력바 비율, 치명상 아군)")
    heal_check: dict | None = Field(default=None, description="수류탄을 맞은 아군·자신 (머리 위 체력바 노란 테두리, 내 HUD 아이콘 노랑)")
    antiheal_check: dict | None = Field(default=None, description="치유 차단을 받은 플레이어 (팀 목록 체력바 보라 테두리)")
    kill_check: dict | None = Field(default=None, description="사용 직후 처치 관여 (화면 처치 알림 + 팀 목록 적 칸 변화)")
    reflect_check: dict | None = Field(default=None, description="반사 판별 (생체 수류탄: 직후 자신에게 치유 차단 아이콘 = 튕겨내기 등에 반사됨)")


class EvidenceFrame(BaseModel):
    timestamp_seconds: float
    path: str = Field(description="run 디렉터리 기준 상대 경로")


class FeedbackItem(BaseModel):
    id: str
    start_time_seconds: float
    end_time_seconds: float
    category: Category
    observation: str
    assessment: str
    suggestion: str
    evidence_frame_timestamps: list[float]
    uncertainty: str
    hero: str
    patch_version: str
    # --- additional fields
    grade: Literal["excellent", "best", "blunder", "withheld"] = Field(
        description="탁월(excellent) / 최선(best) / 블런더(blunder) / 판단 보류(withheld)")
    confidence: float
    uncertainty_level: Level
    criterion_ids: list[str]
    criteria_review_status: Literal["draft", "approved", "none"]
    display_mode: Literal["assertive", "tentative"]
    display_reasons: list[str] = Field(description="tentative 로 표시된 이유")
    evidence_frames: list[EvidenceFrame]
    scene_id: str
    hud_checks: list[HudCheck] = Field(default_factory=list, description="스킬·궁극기 사용 피드백에만: 이 피드백 기준의 스킬 사용 주장과 HUD 대조 결과")
    verification: Literal["hud", "model_observation"] = Field(
        default="model_observation",
        description="hud: 스킬/궁극기 사용이 HUD 대조로 확인됨 / model_observation: 모델 관찰(포지셔닝 등, 코드로 확인하지 않음)")
    game_rule: str = Field(default="", description="판단에 쓴 게임 규칙·수치 (Overwatch Wiki 수집본, 단위·조건 포함)")
    kill_feed: list[dict] = Field(default_factory=list, description="이 피드백 시간대의 킬피드 (처치자 > 죽은 사람, 팀·영웅은 팀 목록과 대조)")

    @model_validator(mode="after")
    def _check_times(self) -> "FeedbackItem":
        if self.start_time_seconds < 0 or self.end_time_seconds < self.start_time_seconds:
            raise ValueError("invalid time range")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence out of range")
        return self


class ActionItem(BaseModel):
    task: str
    why: str
    related_feedback_ids: list[str]


class DiscardedItem(BaseModel):
    reason: str
    scene_id: str | None = None
    summary: str


class StageUsage(BaseModel):
    stage: str
    model: str
    prompt_version: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    input_tokens_by_modality: dict[str, int] = Field(default_factory=dict)
    latency_seconds: float
    attempts: int = 1                # API 호출 시도 횟수 (1 = 재시도 없음)
    retry_errors: list[str] = Field(default_factory=list)  # 재시도를 일으킨 오류 (예: "503 ...")
    from_cache: bool = False


class CostSummary(BaseModel):
    estimated: bool = True
    currency: str | None = None
    total: float | None = None
    full_run_total: float | None = Field(default=None, description="캐시 없이 처음부터 분석했을 때의 비용 (재사용한 단계 포함)")
    note: str


class VideoInfo(BaseModel):
    file_name: str
    sha256: str
    duration_seconds: float
    width: int
    height: int
    fps: float | None
    codec: str | None


class Versions(BaseModel):
    pipeline: str
    result_schema: str
    provider: str
    model: str
    sdk: str | None
    prompts: dict[str, str]
    hero_profile: str
    hero_profile_review_status: str
    config_fingerprint: str
    game_data: dict | None = Field(default=None, description="게임 수치 자료 (수집일, 공식 패치 대조 범위, 출처·라이선스)")


class AnalysisResult(BaseModel):
    schema_version: str = RESULT_SCHEMA_VERSION
    run_id: str
    analysis_mode: Literal["real", "mock"]
    notice: str | None = None
    hero: str
    hero_display_name: str
    game_patch_version: str | None
    criteria_patch_version: str
    video: VideoInfo
    player_deaths: list[dict] = Field(default_factory=list, description="감지한 플레이어 사망 (time_seconds, source, killer: 킬피드로 확인한 처치자)")
    skill_uses: list[dict] = Field(default_factory=list,
                                   description="HUD로 확인한 스킬·궁극기 사용과 코드 판별 결과(대상·명중), 순간 확인(모델) 포함")
    hud_ultimate_uses_seconds: list[float] = Field(default_factory=list, description="HUD로 감지한 궁극기 사용 시각(초)")
    skipped_hero_spans: list[dict] = Field(default_factory=list,
                                           description="플레이어가 이 영웅 프로필 외 영웅으로 플레이해 분석하지 않은 구간 (hero, start_seconds, end_seconds; 팀 목록 초상화로 코드 확인)")
    team: dict | None = Field(default=None, description="팀 판별 단계 결과 (team_identification, roster, 사용한 프레임 시각)")
    overview: dict
    feedback: list[FeedbackItem]
    action_items: list[ActionItem]
    discarded: list[DiscardedItem]
    versions: Versions
    timings_seconds: dict[str, float]
    usage: list[StageUsage]
    cost: CostSummary
    warnings: list[str]
    created_at: str

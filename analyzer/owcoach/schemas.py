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

RESULT_SCHEMA_VERSION = "1.2.0"


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


class DetailResponse(BaseModel):
    observations: list[DetailObservation]
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
    criterion_ids: list[str]
    grade: Literal["excellent", "best", "blunder", "withheld"] = Field(
        description="excellent=탁월, best=최선, blunder=블런더, withheld=근거 부족으로 판단 보류")
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

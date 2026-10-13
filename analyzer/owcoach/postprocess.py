"""Deterministic post-processing (stage 5): scene planning, time conversion, validation,
removal of unsupported assertions and duplicates. No AI calls here."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .heroes import HeroProfile
from .schemas import (
    ActionItem,
    AnalysisResult,
    CoachingResponse,
    DetailResponse,
    DiscardedItem,
    EvidenceFrame,
    FeedbackItem,
    OverviewResponse,
)


# ------------------------------------------------------------------ scene planning

@dataclass
class Scene:
    scene_id: str
    window_start: float  # original-video seconds
    window_end: float
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    priority: int = 0

    @property
    def duration(self) -> float:
        return round(self.window_end - self.window_start, 3)

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id, "window_start": self.window_start, "window_end": self.window_end,
            "categories": self.categories, "reasons": self.reasons, "priority": self.priority,
        }


def plan_scenes(overview: OverviewResponse, duration: float, cfg: dict, max_candidates: int,
                discarded: list[DiscardedItem]) -> list[Scene]:
    valid = []
    for c in overview.candidates:
        if c.start_time_seconds < 0 or c.end_time_seconds > duration or c.end_time_seconds < c.start_time_seconds:
            discarded.append(DiscardedItem(
                reason="후보 장면 시간이 영상 범위를 벗어남",
                summary=f"{c.start_time_seconds}-{c.end_time_seconds}s {c.category}: {c.reason_observed}",
            ))
            continue
        valid.append(c)
    valid.sort(key=lambda c: (-c.priority, c.start_time_seconds))
    for c in valid[max_candidates:]:
        discarded.append(DiscardedItem(reason="후보 장면 최대 개수 초과", summary=c.reason_observed))
    chosen = sorted(valid[:max_candidates], key=lambda c: c.start_time_seconds)

    windows: list[Scene] = []
    for c in chosen:
        start = max(0.0, round(c.start_time_seconds - cfg["pre_seconds"], 3))
        end = min(duration, round(c.end_time_seconds + cfg["post_seconds"], 3))
        last = windows[-1] if windows else None
        if (last and start - last.window_end <= cfg["merge_gap_seconds"]
                and max(end, last.window_end) - last.window_start <= cfg["max_window_seconds"]):
            last.window_end = max(last.window_end, end)
            last.categories = sorted(set(last.categories) | {c.category})
            last.reasons.append(c.reason_observed)
            last.priority = max(last.priority, c.priority)
        else:
            if end - start > cfg["max_window_seconds"]:
                end = round(start + cfg["max_window_seconds"], 3)
            windows.append(Scene("", start, end, [c.category], [c.reason_observed], c.priority))
    for i, s in enumerate(windows, 1):
        s.scene_id = f"scene-{i:02d}"
    return windows


def detail_to_original(scene: Scene, detail: DetailResponse, tolerance: float,
                       warnings: list[str]) -> dict:
    """Convert clip-relative times to original-video times and drop out-of-window entries."""

    def conv(t: float | None) -> float | None:
        if t is None:
            return None
        if t < -tolerance or t > scene.duration + tolerance:
            return None
        return round(min(max(t, 0.0), scene.duration) + scene.window_start, 3)

    obs = []
    for o in detail.observations:
        t = conv(o.time_seconds)
        if t is None:
            warnings.append(f"{scene.scene_id}: 구간 밖 관찰 시간({o.time_seconds}s) 제외")
            continue
        obs.append({**o.model_dump(), "time_seconds": t, "end_time_seconds": conv(o.end_time_seconds)})
    hud = []
    for h in detail.hud_readings:
        t = conv(h.time_seconds)
        if t is not None:
            hud.append({**h.model_dump(), "time_seconds": t})
    keys = [t for t in (conv(k) for k in detail.key_moment_times) if t is not None]
    return {
        **scene.to_dict(),
        "observations": obs,
        "hud_readings": hud,
        "not_determinable": detail.not_determinable,
        "outcome": detail.outcome,
        "key_moment_times": keys,
    }


# ------------------------------------------------------------------ feedback finalisation

def _overlap_ratio(a: FeedbackItem, b: FeedbackItem) -> float:
    inter = min(a.end_time_seconds, b.end_time_seconds) - max(a.start_time_seconds, b.start_time_seconds)
    shortest = min(a.end_time_seconds - a.start_time_seconds, b.end_time_seconds - b.start_time_seconds)
    if shortest <= 0:  # point-like item: duplicate if it lies inside the other
        return 1.0 if inter >= 0 else 0.0
    return max(0.0, inter) / shortest


def finalize_feedback(
    coaching: CoachingResponse,
    scenes: dict[str, dict],
    hero: HeroProfile,
    duration: float,
    out_cfg: dict,
    extract_evidence: Callable[[float], str | None],
    discarded: list[DiscardedItem],
) -> tuple[list[FeedbackItem], list[ActionItem]]:
    tol = out_cfg["window_tolerance_seconds"]
    candidates: list[FeedbackItem] = []

    for item in coaching.items:
        summary = f"{item.start_time_seconds}-{item.end_time_seconds}s {item.category}: {item.observation[:80]}"
        scene = scenes.get(item.scene_id)
        if scene is None:
            discarded.append(DiscardedItem(reason="존재하지 않는 장면 참조", scene_id=item.scene_id, summary=summary))
            continue
        s, e = item.start_time_seconds, item.end_time_seconds
        if s < 0 or e > duration or e < s:
            discarded.append(DiscardedItem(reason="시간 범위가 영상 길이를 벗어남", scene_id=item.scene_id, summary=summary))
            continue
        if s < scene["window_start"] - tol or e > scene["window_end"] + tol:
            discarded.append(DiscardedItem(reason="분석한 장면 구간 밖의 시간", scene_id=item.scene_id, summary=summary))
            continue

        frames: list[EvidenceFrame] = []
        for t in sorted(set(item.evidence_frame_timestamps)):
            if not (0 <= t <= duration) or not (scene["window_start"] - tol <= t <= scene["window_end"] + tol):
                continue
            path = extract_evidence(t)
            if path:
                frames.append(EvidenceFrame(timestamp_seconds=t, path=path))

        valid_criteria = [c for c in item.criterion_ids if hero.criterion(c)]
        statuses = {hero.criterion(c).status for c in valid_criteria}
        review_status = "none" if not valid_criteria else ("approved" if statuses == {"approved"} else "draft")

        judgement = item.judgement
        reasons = []
        if not valid_criteria:
            reasons.append("적용할 코칭 기준을 찾을 수 없음")
        if not frames:
            reasons.append("확인된 근거 프레임 없음")
        if item.relies_on_outcome_only:
            reasons.append("결과(사망 등)만을 근거로 한 판단")
            if judgement == "needs_improvement":
                judgement = "withheld"
        if judgement == "withheld":
            reasons.append("근거 부족으로 판단 보류")
        if item.confidence < out_cfg["min_confidence_for_assertive"]:
            reasons.append(f"확신도 낮음({item.confidence:.2f})")
        if item.uncertainty_level == "high":
            reasons.append("불확실성 높음")
        if out_cfg["require_approved_criteria_for_assertive"] and review_status != "approved":
            reasons.append("코치 검수되지 않은 기준")

        candidates.append(FeedbackItem(
            id="", scene_id=item.scene_id,
            start_time_seconds=s, end_time_seconds=e, category=item.category,
            observation=item.observation, assessment=item.assessment, suggestion=item.suggestion,
            evidence_frame_timestamps=[f.timestamp_seconds for f in frames], evidence_frames=frames,
            uncertainty=item.uncertainty, uncertainty_level=item.uncertainty_level,
            hero=hero.hero_id, patch_version=hero.patch_version,
            judgement=judgement, confidence=max(0.0, min(1.0, item.confidence)),
            criterion_ids=valid_criteria, criteria_review_status=review_status,
            display_mode="tentative" if reasons else "assertive", display_reasons=reasons,
        ))

    # assertive first, then by confidence; drop duplicates of already-kept items
    candidates.sort(key=lambda f: (f.display_mode != "assertive", -f.confidence, f.start_time_seconds))
    kept: list[FeedbackItem] = []
    for f in candidates:
        dup = next((k for k in kept if k.category == f.category
                    and _overlap_ratio(k, f) >= out_cfg["dedup_overlap_ratio"]), None)
        if dup:
            discarded.append(DiscardedItem(reason="중복 피드백", scene_id=f.scene_id, summary=f.observation[:80]))
            continue
        if len(kept) >= out_cfg["max_feedback_items"]:
            discarded.append(DiscardedItem(reason="피드백 최대 개수 초과", scene_id=f.scene_id, summary=f.observation[:80]))
            continue
        kept.append(f)
    kept.sort(key=lambda f: f.start_time_seconds)
    for i, f in enumerate(kept, 1):
        f.id = f"fb-{i:02d}"

    actions: list[ActionItem] = []
    for a in coaching.action_items:
        related = [f.id for f in kept if f.scene_id in a.related_scene_ids and f.display_mode == "assertive"]
        if not related:
            discarded.append(DiscardedItem(reason="근거가 되는 단정적 피드백이 없는 과제", summary=a.task))
            continue
        if len(actions) >= out_cfg["max_action_items"]:
            discarded.append(DiscardedItem(reason="과제 최대 개수 초과", summary=a.task))
            continue
        actions.append(ActionItem(task=a.task, why=a.why, related_feedback_ids=related))
    return kept, actions


# ------------------------------------------------------------------ final validation

def validate_result(result: AnalysisResult, run_dir: Path) -> list[str]:
    """Independent re-check of the final result. Returns error strings (empty = valid)."""
    errors = []
    AnalysisResult.model_validate(result.model_dump())  # schema
    d = result.video.duration_seconds
    ids = set()
    for f in result.feedback:
        if f.id in ids:
            errors.append(f"{f.id}: duplicate id")
        ids.add(f.id)
        if not (0 <= f.start_time_seconds <= f.end_time_seconds <= d):
            errors.append(f"{f.id}: time range outside video ({f.start_time_seconds}-{f.end_time_seconds}, duration {d})")
        if f.evidence_frame_timestamps != [e.timestamp_seconds for e in f.evidence_frames]:
            errors.append(f"{f.id}: evidence timestamps do not match extracted frames")
        for e in f.evidence_frames:
            p = run_dir / e.path
            if not p.is_file() or p.stat().st_size == 0:
                errors.append(f"{f.id}: evidence frame missing: {e.path}")
            if not 0 <= e.timestamp_seconds <= d:
                errors.append(f"{f.id}: evidence frame outside video: {e.timestamp_seconds}")
        if f.display_mode == "assertive" and (not f.criterion_ids or not f.evidence_frames or f.judgement == "withheld"):
            errors.append(f"{f.id}: assertive feedback without criterion/evidence")
    for a in result.action_items:
        for r in a.related_feedback_ids:
            if r not in ids:
                errors.append(f"action item references unknown feedback {r}")
    return errors

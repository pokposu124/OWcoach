"""Deterministic post-processing (stage 5): scene planning, time conversion, validation,
removal of unsupported assertions and duplicates. No AI calls here."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .heroes import HeroProfile
from .timefmt import fmt_range, fmt_time, normalize_times

HUD_CATEGORIES = {"skill_usage", "ultimate_usage"}
CATEGORY_KO = {"skill_usage": "스킬 사용", "positioning": "포지셔닝", "ultimate_usage": "궁극기 사용"}
from .schemas import (
    ActionItem,
    AnalysisResult,
    CoachingResponse,
    DetailResponse,
    DiscardedItem,
    EvidenceFrame,
    FeedbackItem,
    HudCheck,
    OverviewResponse,
    SceneCandidate,
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
                discarded: list[DiscardedItem], forced: list[SceneCandidate] | None = None) -> list[Scene]:
    """forced: candidates that must be analysed (player deaths, HUD-detected ultimates). They are always
    kept and do not count toward max_candidates, which limits the overview model's own picks only."""
    forced = [c for c in (forced or []) if 0 <= c.start_time_seconds <= c.end_time_seconds <= duration]
    valid = []
    for c in overview.candidates:
        if c.start_time_seconds < 0 or c.end_time_seconds > duration or c.end_time_seconds < c.start_time_seconds:
            discarded.append(DiscardedItem(
                reason="후보 장면 시간이 영상 범위를 벗어남",
                summary=f"{fmt_range(c.start_time_seconds, c.end_time_seconds)} {c.category}: {c.reason_observed}",
            ))
            continue
        valid.append(c)
    valid.sort(key=lambda c: (-c.priority, c.start_time_seconds))
    for c in valid[max_candidates:]:
        discarded.append(DiscardedItem(reason="후보 장면 최대 개수 초과", summary=c.reason_observed))
    chosen = sorted([*valid[:max_candidates], *forced], key=lambda c: c.start_time_seconds)

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
            warnings.append(f"{scene.scene_id}: 구간 밖 관찰 시간(구간 기준 {fmt_time(o.time_seconds)}) 제외")
            continue
        obs.append({**o.model_dump(), "time_seconds": t, "end_time_seconds": conv(o.end_time_seconds)})
    hud = []
    for h in detail.hud_readings:
        t = conv(h.time_seconds)
        if t is not None:
            hud.append({**h.model_dump(), "time_seconds": t})
    keys = [t for t in (conv(k) for k in detail.key_moment_times) if t is not None]
    events = []
    for a in detail.ability_events:
        t = conv(a.time_seconds)
        if t is None:
            warnings.append(f"{scene.scene_id}: 구간 밖 스킬 사용 시간(구간 기준 {fmt_time(a.time_seconds)}) 제외")
            continue
        events.append({**a.model_dump(), "time_seconds": t})
    return {
        **scene.to_dict(),
        "observations": obs,
        "hud_readings": hud,
        "ability_events": events,
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
    hud_enabled: bool = True,
    death_times: list[float] | None = None,
) -> tuple[list[FeedbackItem], list[ActionItem]]:
    """max_feedback_items limits ordinary feedback only: a HUD-verified ultimate use and feedback that
    runs up to a player death (ends at most 3 s before it) are always kept on top of that limit."""
    tol = out_cfg["window_tolerance_seconds"]
    candidates: list[FeedbackItem] = []

    for item in coaching.items:
        summary = f"{fmt_range(item.start_time_seconds, item.end_time_seconds)} {item.category}: {normalize_times(item.observation[:80])}"
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

        grade = item.grade
        unverifiable = set(out_cfg.get("unverifiable_criteria", []))
        if valid_criteria and set(valid_criteria) <= unverifiable and grade != "withheld":
            grade = "withheld"
            unverifiable_note = "근거(적 대응 스킬 활성)가 코드로 확인되지 않는 모델 관찰이라 판단 보류"
        else:
            unverifiable_note = ""
        reasons = []
        if not valid_criteria:
            reasons.append("적용할 코칭 기준을 찾을 수 없음")
        if not frames:
            reasons.append("확인된 근거 프레임 없음")
        if item.relies_on_outcome_only:
            reasons.append("결과(사망 등)만을 근거로 한 판단")
            if grade == "blunder":
                grade = "withheld"
        if unverifiable_note:
            reasons.append(unverifiable_note)
        elif grade == "withheld":
            reasons.append("근거 부족으로 판단 보류")
        if item.confidence < out_cfg["min_confidence_for_assertive"]:
            reasons.append(f"확신도 낮음({item.confidence:.2f})")
        if item.uncertainty_level == "high":
            reasons.append("불확실성 높음")
        if out_cfg["require_approved_criteria_for_assertive"] and review_status != "approved":
            reasons.append("코치 검수되지 않은 기준")

        # HUD evidence belongs to skill / ultimate feedback only. Positioning is judged on the model's
        # observation alone: a grenade verified in the same window says nothing about where the player
        # stood, so it must neither raise nor lower those items.
        checks: list[HudCheck] = []
        if item.category in HUD_CATEGORIES:
            # only the abilities this item's criteria are about (a nano item is not judged by a grenade claim)
            abilities = {hero.criterion(c).ability for c in valid_criteria if hero.criterion(c).ability}
            checks = [HudCheck(time_seconds=ev["time_seconds"], ability_id=ev["ability_id"],
                               claimed_by_model=" / ".join(x for x in (ev.get("target"), ev.get("evidence")) if x),
                               **ev["hud_check"])
                      for ev in scene.get("ability_events", [])
                      if "hud_check" in ev and s - tol <= ev["time_seconds"] <= e + tol
                      and (not abilities or ev["ability_id"] in abilities)]
            bad = [c for c in checks if c.status == "contradicted"]
            if bad:
                reasons.append("HUD 대조 불일치: " + "; ".join(f"{c.ability_id} {fmt_time(c.time_seconds)} — {c.note}" for c in bad))
            if (out_cfg.get("require_hud_verification_for_assertive") and hud_enabled
                    and not any(c.status == "verified" for c in checks)):
                reasons.append("HUD로 확인된 스킬/궁극기 사용 근거 없음")
        verified = item.category in HUD_CATEGORIES and any(c.status == "verified" for c in checks)
        missed = [c for c in checks if (c.target_check or {}).get("status") == "missed"]
        if missed and grade in ("best", "excellent"):
            grade = "withheld"
            reasons.append("; ".join(f"{c.ability_id} {fmt_time(c.hud_use_time_seconds or c.time_seconds)}: 코드 확인 결과 빗나감 "
                                     f"({c.target_check.get('note', '')}), 명중을 전제로 한 판정 보류" for c in missed))
        doubtful = [c for c in checks if c.death_after_seconds is not None and c not in missed]
        if doubtful and grade in ("best", "excellent"):
            grade = "withheld"
            reasons.append("; ".join(f"{c.ability_id} {fmt_time(c.hud_use_time_seconds or c.time_seconds)} 사용 {c.death_after_seconds:g}초 뒤 사망: "
                                     "명중했다면 대상이 행동하지 못했어야 하므로 명중 여부 미확인, 판단 보류" for c in doubtful))
        if item.category in out_cfg.get("hold_categories", []):
            reasons.append(f"{CATEGORY_KO[item.category]}은(는) 코드로 확인할 수 없어 보류")

        candidates.append(FeedbackItem(
            id="", scene_id=item.scene_id,
            start_time_seconds=s, end_time_seconds=e, category=item.category,
            observation=normalize_times(item.observation), assessment=normalize_times(item.assessment),
            suggestion=normalize_times(item.suggestion),
            evidence_frame_timestamps=[f.timestamp_seconds for f in frames], evidence_frames=frames,
            uncertainty=normalize_times(item.uncertainty), uncertainty_level=item.uncertainty_level,
            hero=hero.hero_id, patch_version=hero.patch_version,
            grade=grade, confidence=max(0.0, min(1.0, item.confidence)),
            criterion_ids=valid_criteria, criteria_review_status=review_status,
            display_mode="tentative" if reasons else "assertive", display_reasons=reasons,
            hud_checks=checks, verification="hud" if verified else "model_observation",
        ))

    def must_keep(f: FeedbackItem) -> bool:
        if f.category == "ultimate_usage" and f.verification == "hud":
            return True
        return any(f.start_time_seconds <= t + 1 and f.end_time_seconds >= t - 3 for t in death_times or [])

    # must-keep first, then assertive, then by confidence; drop duplicates of already-kept items
    candidates.sort(key=lambda f: (not must_keep(f), f.display_mode != "assertive", -f.confidence, f.start_time_seconds))
    kept: list[FeedbackItem] = []
    ordinary = 0
    for f in candidates:
        dup = next((k for k in kept if k.category == f.category
                    and _overlap_ratio(k, f) >= out_cfg["dedup_overlap_ratio"]), None)
        if dup:
            discarded.append(DiscardedItem(reason="중복 피드백", scene_id=f.scene_id, summary=f.observation[:80]))
            continue
        if not must_keep(f):
            if ordinary >= out_cfg["max_feedback_items"]:
                discarded.append(DiscardedItem(reason="피드백 최대 개수 초과", scene_id=f.scene_id, summary=f.observation[:80]))
                continue
            ordinary += 1
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
        actions.append(ActionItem(task=normalize_times(a.task), why=normalize_times(a.why), related_feedback_ids=related))
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
        if f.display_mode == "assertive" and (not f.criterion_ids or not f.evidence_frames or f.grade == "withheld"):
            errors.append(f"{f.id}: assertive feedback without criterion/evidence")
    for a in result.action_items:
        for r in a.related_feedback_ids:
            if r not in ids:
                errors.append(f"action item references unknown feedback {r}")
    return errors

"""Deterministic mock provider for pipeline/UI testing WITHOUT calling any AI API.

Results produced with this provider are marked analysis_mode="mock" and must never be reported
as a real analysis. The responses deliberately include a few bad items (out-of-range times,
an unknown criterion, an outcome-only judgement, a duplicate) so post-processing is exercised.
"""

from __future__ import annotations

import json

from .base import AnalysisProvider, ImageRequest, ProviderResponse, TextRequest, Usage, VideoRequest


class MockProvider(AnalysisProvider):
    name = "mock"
    is_mock = True

    def __init__(self, settings: dict | None = None):
        self.calls: list[str] = []

    @property
    def model(self) -> str:
        return "mock-model"

    def _resp(self, data) -> ProviderResponse:
        return ProviderResponse(
            data=data, raw_text=json.dumps(data, ensure_ascii=False), usage=Usage(),
            model=self.model, latency_seconds=0.0,
        )

    def analyze_video(self, req: VideoRequest) -> ProviderResponse:
        stage = req.context["stage"]
        self.calls.append(stage)
        if stage == "overview":
            return self._resp(self._overview(req.context["duration"]))
        if stage == "detail":
            return self._resp(self._detail(req.context["clip_duration"]))
        raise ValueError(stage)

    def analyze_images(self, req: ImageRequest) -> ProviderResponse:
        self.calls.append(req.context["stage"])
        return self._resp({
            "team_identification": {"player_name": "MOCK", "ally_side": "left", "ally_color": "파랑",
                                    "enemy_color": "빨강", "evidence": "[모의] 왼쪽 팀 목록에 플레이어 이름"},
            "roster": [
                {"slot": 1, "player_name": "MOCK", "team": "ally"},
                {"slot": 6, "player_name": "ENEMY1", "team": "enemy"},
            ],
        })

    def generate_json(self, req: TextRequest) -> ProviderResponse:
        self.calls.append(req.context["stage"])
        return self._resp(self._coaching(req.context["scenes"]))

    # ------------------------------------------------------------------

    @staticmethod
    def _overview(d: float) -> dict:
        def at(frac: float) -> float:
            return round(d * frac, 1)

        return {
            "hero_confirmed": True,
            "hero_evidence": "[모의] 무기와 스킬 아이콘으로 확인",
            "hud_readable": "yes",
            "video_quality_notes": "[모의] 화질 양호",
            "timeline": [
                {"time_seconds": at(0.1), "event": "[모의] 교전 시작", "visible_evidence": "[모의] 적 체력바 표시"},
                {"time_seconds": at(0.5), "event": "[모의] 본인 체력 급감", "visible_evidence": "[모의] 체력 바 감소"},
            ],
            "candidates": [
                {"start_time_seconds": at(0.2), "end_time_seconds": at(0.2) + 3, "category": "skill_usage",
                 "reason_observed": "[모의] 수면총 아이콘 쿨다운 표시", "priority": 4},
                {"start_time_seconds": at(0.5), "end_time_seconds": at(0.5) + 4, "category": "survival",
                 "reason_observed": "[모의] 체력 급감 후 이동", "priority": 5},
                {"start_time_seconds": at(0.8), "end_time_seconds": at(0.8) + 2, "category": "positioning",
                 "reason_observed": "[모의] 개활지 체류", "priority": 3},
                # invalid: beyond video duration -> must be discarded
                {"start_time_seconds": d + 30, "end_time_seconds": d + 35, "category": "positioning",
                 "reason_observed": "[모의] 범위 밖 후보", "priority": 2},
            ],
        }

    @staticmethod
    def _detail(clip_d: float) -> dict:
        mid = round(clip_d / 2, 1)
        return {
            "observations": [
                {"time_seconds": round(mid - 1, 1), "end_time_seconds": mid, "fact": "[모의] 적이 화면 중앙에서 접근", "visibility": "clear"},
                {"time_seconds": mid, "end_time_seconds": None, "fact": "[모의] 엄폐물 뒤로 이동", "visibility": "partial"},
            ],
            "hud_readings": [
                {"time_seconds": mid, "item": "health", "value": "[모의] 약 80", "confidence": "low"},
            ],
            # sleep dart claim (checked against HUD pixels) + an unknown ability id
            "ability_events": [
                {"time_seconds": round(mid - 0.5, 1), "ability_id": "sleep_dart", "target": "", "evidence": "[모의] 쿨다운 숫자 표시"},
                {"time_seconds": mid, "ability_id": "does_not_exist", "target": "", "evidence": "[모의] 없는 스킬"},
            ],
            "not_determinable": ["[모의] 화면 밖 적 위치"],
            "outcome": "[모의] 생존",
            "key_moment_times": [round(mid - 1, 1), mid],
        }

    @staticmethod
    def _coaching(scenes: list[dict]) -> dict:
        items = []
        for i, s in enumerate(scenes):
            a, b = s["window_start"], s["window_end"]
            mid = round((a + b) / 2, 1)
            items.append({
                "scene_id": s["scene_id"], "start_time_seconds": round(mid - 1, 1), "end_time_seconds": mid,
                "category": "skill_usage" if i == 0 else s["categories"][0],
                "observation": f"[모의] {s['scene_id']} 관찰 사실",
                "assessment": "[모의] 기준 적용 판단", "suggestion": "[모의] 다음 행동 제안",
                "criterion_ids": ["ana.survival.disengage_on_pressure"] if i % 2 else ["ana.skill.sleep_dart_usage"],
                "judgement": "good" if i == 0 else "needs_improvement",
                "confidence": 0.8, "uncertainty": "", "uncertainty_level": "low",
                "relies_on_outcome_only": False,
                "evidence_frame_timestamps": [round(mid - 1, 1), mid],
            })
        if scenes:
            s = scenes[0]
            mid = round((s["window_start"] + s["window_end"]) / 2, 1)
            base = dict(items[0])
            items.append({**base, "observation": "[모의] 중복 피드백", "confidence": 0.5})  # duplicate
            items.append({**base, "criterion_ids": ["ana.does_not_exist"], "category": "positioning",
                          "observation": "[모의] 없는 기준 참조"})  # unknown criterion
            items.append({**base, "judgement": "needs_improvement", "relies_on_outcome_only": True,
                          "category": "survival", "observation": "[모의] 사망 결과만 근거",
                          "criterion_ids": ["ana.survival.disengage_on_pressure"]})  # outcome only
            items.append({**base, "start_time_seconds": mid + 500, "end_time_seconds": mid + 505,
                          "observation": "[모의] 범위 밖 시간"})  # out of range
        return {
            "items": items,
            "action_items": [
                {"task": "[모의] 교전 중 엄폐물 1~2걸음 거리 유지", "why": "[모의] 근거",
                 "related_scene_ids": [s["scene_id"] for s in scenes[:1]]},
                {"task": "[모의] 근거 없는 과제", "why": "[모의]", "related_scene_ids": ["scene-999"]},
            ],
        }

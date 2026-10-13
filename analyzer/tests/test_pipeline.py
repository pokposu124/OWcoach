import json
import shutil
import subprocess
from pathlib import Path

import pytest

from owcoach.config import load_config, load_pricing
from owcoach.heroes import available_heroes, load_hero
from owcoach.media import probe, validate_input
from owcoach.pipeline import Pipeline, RunStore, StageFailed, compute_cost, inline_refs
from owcoach.postprocess import Scene, detail_to_original, plan_scenes, validate_result
from owcoach.prompts import load_prompt
from owcoach.providers import ProviderError, create_provider
from owcoach.providers.mock import MockProvider
from owcoach.schemas import AnalysisResult, CoachingResponse, DetailResponse, OverviewResponse, StageUsage


@pytest.fixture(scope="session")
def video(tmp_path_factory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("v") / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-t", "20", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


@pytest.fixture
def cfg():
    return load_config()


def make_pipeline(cfg, tmp_path, provider=None):
    return Pipeline(cfg, {"models": {}}, provider or MockProvider(), load_hero("ana"), runs_dir=tmp_path, log=lambda *_: None)


# ---------------------------------------------------------------- config / hero / prompts

def test_hero_profile_loads_and_is_draft():
    assert "ana" in available_heroes()
    hero = load_hero("ana")
    assert hero.review_status == "draft"
    assert all(c.status == "draft" for c in hero.criteria)
    assert {c.category for c in hero.criteria} == {"positioning", "survival", "skill_usage"}
    # numeric facts must carry source + patch (enforced by the model)
    assert all(f.source and f.patch_version for f in hero.numeric_facts)


def test_unknown_hero_rejected():
    with pytest.raises(ValueError):
        load_hero("nonexistent")


def test_prompts_render_all_placeholders():
    p = load_prompt("overview")
    text = p.render(hero_name_ko="아나", hero_name_en="Ana", duration_seconds=10, max_candidates=3, observable_signals="-")
    assert "$" not in text and p.version
    with pytest.raises(KeyError):
        p.render(hero_name_ko="아나")


def test_inline_refs_has_no_refs():
    s = json.dumps(inline_refs(CoachingResponse.model_json_schema()))
    assert "$ref" not in s and "$defs" not in s


def test_gemini_requires_model_env(monkeypatch, cfg):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.delenv("OWCOACH_GEMINI_MODEL", raising=False)
    with pytest.raises(ProviderError, match="OWCOACH_GEMINI_MODEL"):
        create_provider("gemini", cfg["provider"])


def test_gemini_requires_api_key(monkeypatch, cfg):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        create_provider("gemini", cfg["provider"])


# ---------------------------------------------------------------- input validation

def test_validate_input(video, cfg):
    info = probe(video)
    assert info.duration_seconds == pytest.approx(20, abs=0.1)
    assert validate_input(video, info, cfg["input"]) == []
    long_rules = {**cfg["input"], "max_duration_seconds": 10}
    assert any("최대" in r for r in validate_input(video, info, long_rules))
    hi_rules = {**cfg["input"], "min_width": 1920, "min_height": 1080}
    assert any("해상도" in r for r in validate_input(video, info, hi_rules))


# ---------------------------------------------------------------- scene planning / time conversion

def _overview(cands):
    return OverviewResponse(hero_confirmed=True, hero_evidence="", hud_readable="yes", video_quality_notes="",
                            timeline=[], candidates=cands)


def test_plan_scenes_expands_clamps_merges_and_discards(cfg):
    dcfg = {**cfg["detail"], "pre_seconds": 5, "post_seconds": 2, "merge_gap_seconds": 1, "max_window_seconds": 30}
    cands = [
        {"start_time_seconds": 2, "end_time_seconds": 4, "category": "survival", "reason_observed": "a", "priority": 3},
        {"start_time_seconds": 8, "end_time_seconds": 9, "category": "positioning", "reason_observed": "b", "priority": 2},
        {"start_time_seconds": 50, "end_time_seconds": 52, "category": "skill_usage", "reason_observed": "c", "priority": 5},
        {"start_time_seconds": 70, "end_time_seconds": 75, "category": "skill_usage", "reason_observed": "out", "priority": 5},
    ]
    discarded = []
    scenes = plan_scenes(_overview(cands), 60.0, dcfg, 8, discarded)
    assert [(s.window_start, s.window_end) for s in scenes] == [(0.0, 11.0), (45.0, 54.0)]
    assert scenes[0].categories == ["positioning", "survival"]
    assert len(discarded) == 1 and "범위" in discarded[0].reason


def test_detail_times_converted_to_original():
    scene = Scene("scene-01", 100.0, 120.0)
    detail = DetailResponse(
        observations=[{"time_seconds": 5, "end_time_seconds": 7, "fact": "x", "visibility": "clear"},
                      {"time_seconds": 45, "fact": "out of clip", "visibility": "clear"}],
        hud_readings=[], not_determinable=[], outcome="", key_moment_times=[3, 99])
    warnings = []
    rec = detail_to_original(scene, detail, 1.0, warnings)
    assert [(o["time_seconds"], o["end_time_seconds"]) for o in rec["observations"]] == [(105.0, 107.0)]
    assert rec["key_moment_times"] == [103.0]
    assert warnings


# ---------------------------------------------------------------- end to end (mock provider)

def test_mock_end_to_end(video, cfg, tmp_path):
    prov = MockProvider()
    result, run_dir = make_pipeline(cfg, tmp_path, prov).run(video)
    assert result.analysis_mode == "mock" and result.notice
    assert validate_result(result, run_dir) == []
    assert 0 < len(result.feedback) <= cfg["output"]["max_feedback_items"]
    reasons = {d.reason for d in result.discarded}
    assert {"중복 피드백", "시간 범위가 영상 길이를 벗어남", "후보 장면 시간이 영상 범위를 벗어남"} <= reasons
    for f in result.feedback:
        assert 0 <= f.start_time_seconds <= f.end_time_seconds <= result.video.duration_seconds
        assert all((run_dir / e.path).is_file() for e in f.evidence_frames)
        if f.display_mode == "assertive":
            assert f.criterion_ids and f.evidence_frames and f.judgement != "withheld"
    unknown = [f for f in result.feedback if "적용할 코칭 기준을 찾을 수 없음" in f.display_reasons]
    assert unknown and all(f.display_mode == "tentative" for f in unknown)
    outcome = [f for f in result.feedback if "결과(사망 등)만을 근거로 한 판단" in f.display_reasons]
    assert outcome and all(f.judgement == "withheld" for f in outcome)
    assert all(a.related_feedback_ids for a in result.action_items)
    assert result.cost.total is None
    AnalysisResult.model_validate_json((run_dir / "result.json").read_text())
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "done"


def test_rerun_reuses_cached_stages(video, cfg, tmp_path):
    make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="job1")
    prov2 = MockProvider()
    result, _ = make_pipeline(cfg, tmp_path, prov2).run(video, run_id="job1")
    assert prov2.calls == []  # no duplicate provider calls
    assert all(u.from_cache for u in result.usage)

    prov3 = MockProvider()
    make_pipeline(cfg, tmp_path, prov3).run(video, run_id="job1", force_stages={"coaching"})
    assert prov3.calls == ["coaching"]


def test_config_change_invalidates_only_affected_stages(video, cfg, tmp_path):
    make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="job2")
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["detail"]["fps"] = 2.0
    prov = MockProvider()
    make_pipeline(cfg2, tmp_path, prov).run(video, run_id="job2")
    assert "overview" not in prov.calls and "detail" in prov.calls and "coaching" in prov.calls


class FailingCoachProvider(MockProvider):
    def generate_json(self, req):
        self.calls.append("coaching")
        raise ProviderError("simulated outage")


def test_failed_stage_is_recorded_and_retry_resumes(video, cfg, tmp_path):
    with pytest.raises(StageFailed) as ei:
        make_pipeline(cfg, tmp_path, FailingCoachProvider()).run(video, run_id="job3")
    assert ei.value.stage == "coaching"
    meta = json.loads((tmp_path / "job3" / "run.json").read_text())
    assert meta["status"] == "failed" and meta["stages"]["coaching"]["status"] == "failed"
    assert meta["stages"]["overview"]["status"] == "done"
    prov = MockProvider()
    make_pipeline(cfg, tmp_path, prov).run(video, run_id="job3")
    assert prov.calls == ["coaching"]


class BadSchemaProvider(MockProvider):
    def analyze_video(self, req):
        r = super().analyze_video(req)
        r.data = {"unexpected": True}
        return r


def test_schema_mismatch_fails_after_repair(video, cfg, tmp_path):
    prov = BadSchemaProvider()
    with pytest.raises(StageFailed, match="스키마"):
        make_pipeline(cfg, tmp_path, prov).run(video, run_id="job4")
    assert prov.calls == ["overview"] * (cfg["provider"]["json_repair_retries"] + 1)


class WrongHeroProvider(MockProvider):
    def _overview(self, d):
        o = MockProvider._overview(d)
        return {**o, "hero_confirmed": False, "hero_evidence": "다른 영웅 무기"}


def test_hero_mismatch_fails(video, cfg, tmp_path):
    with pytest.raises(StageFailed, match="영웅"):
        make_pipeline(cfg, tmp_path, WrongHeroProvider()).run(video, run_id="job5")


def test_concurrent_run_blocked(tmp_path):
    store = RunStore(tmp_path / "job6")
    with store.lock():
        with pytest.raises(StageFailed, match="이미 실행 중"):
            with RunStore(tmp_path / "job6").lock():
                pass


def test_report_written(video, cfg, tmp_path):
    from owcoach.report import write_report

    _, run_dir = make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="job7")
    html = write_report(run_dir).read_text()
    assert "실제 AI 분석 결과가 아닙니다" in html and "coach_review.json" in html


# ---------------------------------------------------------------- cost

def _u(model, i, o, t=0, cached=False):
    return StageUsage(stage="s", model=model, prompt_version="1", input_tokens=i, output_tokens=o,
                      thinking_tokens=t, latency_seconds=0, from_cache=cached)


def test_cost_only_with_verified_price():
    usage = [_u("m1", 1_000_000, 100_000, 100_000)]
    assert compute_cost(usage, {"models": {}}, False).total is None
    pricing = {"models": {"m1": {"input_per_million_tokens": 1.0, "output_per_million_tokens": 10.0,
                                 "currency": "USD", "source_url": "x", "verified_on": "2026-01-01"}}}
    c = compute_cost(usage, pricing, False)
    assert c.estimated and c.total == pytest.approx(3.0) and "추정" in c.note
    assert compute_cost([_u("m1", 1, 1, cached=True)], pricing, False).total is None
    assert load_pricing()["models"] == {}


# ---------------------------------------------------------------- gemini request wiring (offline, fake SDK client)

def test_gemini_request_wiring_offline(monkeypatch, cfg, tmp_path):
    from types import SimpleNamespace as NS

    from google.genai import types

    from owcoach.providers.base import TextRequest, VideoRequest
    from owcoach.providers.gemini import GeminiProvider

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("OWCOACH_GEMINI_MODEL", "configured-model")
    p = GeminiProvider({**cfg["provider"], "file_poll_interval_seconds": 0})
    seen = {}

    def file(state):
        return NS(name="files/x", state=state, uri="u", mime_type="video/mp4", error=None)

    class Files:
        def upload(self, file, config):  # noqa: A002
            return globals_file(types.FileState.PROCESSING)

        def get(self, name):
            return globals_file(types.FileState.ACTIVE)

        def delete(self, name):
            seen["deleted"] = name

    globals_file = file

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, contents=contents, config=config)
            um = types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=20,
                                                            thoughts_token_count=5, total_token_count=125)
            return types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text='{"ok": 1}')]))],
                usage_metadata=um)

    p._client = NS(files=Files(), models=Models())
    r = p.analyze_video(VideoRequest(tmp_path / "v.mp4", "prompt", {"type": "object"}, fps=2.0))
    assert r.data == {"ok": 1} and r.usage.input_tokens == 100 and r.usage.thinking_tokens == 5
    assert seen["model"] == "configured-model" and seen["deleted"] == "files/x"
    assert seen["contents"][0].parts[0].video_metadata.fps == 2.0
    assert seen["config"].response_mime_type == "application/json"
    assert p.generate_json(TextRequest("t", {"type": "object"})).data == {"ok": 1}

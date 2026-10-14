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
    assert {c.category for c in hero.criteria} == {"skill_usage", "positioning", "ultimate_usage"}
    # numeric facts must carry source + patch (enforced by the model)
    assert all(f.source and f.patch_version for f in hero.numeric_facts)


def test_unknown_hero_rejected():
    with pytest.raises(ValueError):
        load_hero("nonexistent")


def test_prompts_render_all_placeholders():
    p = load_prompt("overview")
    text = p.render(hero_name_ko="아나", hero_name_en="Ana", duration_seconds=10, max_candidates=3, observable_signals="-",
                    team_context="-")
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
        {"start_time_seconds": 2, "end_time_seconds": 4, "category": "ultimate_usage", "reason_observed": "a", "priority": 3},
        {"start_time_seconds": 8, "end_time_seconds": 9, "category": "positioning", "reason_observed": "b", "priority": 2},
        {"start_time_seconds": 50, "end_time_seconds": 52, "category": "skill_usage", "reason_observed": "c", "priority": 5},
        {"start_time_seconds": 70, "end_time_seconds": 75, "category": "skill_usage", "reason_observed": "out", "priority": 5},
    ]
    discarded = []
    scenes = plan_scenes(_overview(cands), 60.0, dcfg, 8, discarded)
    assert [(s.window_start, s.window_end) for s in scenes] == [(0.0, 11.0), (45.0, 54.0)]
    assert scenes[0].categories == ["positioning", "ultimate_usage"]
    assert len(discarded) == 1 and "범위" in discarded[0].reason


def test_detail_times_converted_to_original():
    scene = Scene("scene-01", 100.0, 120.0)
    detail = DetailResponse(
        observations=[{"time_seconds": 5, "end_time_seconds": 7, "fact": "x", "visibility": "clear"},
                      {"time_seconds": 45, "fact": "out of clip", "visibility": "clear"}],
        situation_track=[{"time_seconds": 6, "tank": "앞 멀리", "tank_name": "시그마", "player_moving": "no",
                          "facing_allies": "yes", "payload": "멈춤", "enemy_dva": "메카", "note": ""}],
        hud_readings=[], not_determinable=[], outcome="", key_moment_times=[3, 99],
        ability_events=[{"time_seconds": 4, "ability_id": "nano_boost", "target": "", "evidence": ""},
                        {"time_seconds": 60, "ability_id": "nano_boost", "target": "", "evidence": "out"}])
    warnings = []
    rec = detail_to_original(scene, detail, 1.0, warnings)
    assert [(o["time_seconds"], o["end_time_seconds"]) for o in rec["observations"]] == [(105.0, 107.0)]
    assert rec["key_moment_times"] == [103.0]
    assert [e["time_seconds"] for e in rec["ability_events"]] == [104.0]
    assert [(x["time_seconds"], x["tank"]) for x in rec["situation_track"]] == [(106.0, "앞 멀리")]
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
            assert f.criterion_ids and f.evidence_frames and f.grade != "withheld"
    unknown = [f for f in result.feedback if "적용할 코칭 기준을 찾을 수 없음" in f.display_reasons]
    assert unknown and all(f.display_mode == "tentative" for f in unknown)
    outcome = [f for f in result.feedback if "결과(사망 등)만을 근거로 한 판단" in f.display_reasons]
    assert outcome and all(f.grade == "withheld" for f in outcome)
    assert all(a.related_feedback_ids for a in result.action_items)
    # draft criteria + no HUD-verified ability use (synthetic video has no HUD) -> nothing assertive
    assert all(f.display_mode == "tentative" for f in result.feedback)
    assert any(f.hud_checks for f in result.feedback)
    # HUD checks only on skill items, and only for the ability of the item's criterion (sleep dart here);
    # the mock's unknown ability id is therefore not attached to any item
    assert all(f.category == "skill_usage" for f in result.feedback if f.hud_checks)
    assert {c.ability_id for f in result.feedback for c in f.hud_checks} == {"sleep_dart"}
    assert result.team["team_identification"]["ally_side"] == "left" and len(result.team["frame_times_seconds"]) == 2
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


def test_team_stage_sends_cropped_stills(video, cfg, tmp_path):
    class Spy(MockProvider):
        def analyze_images(self, req):
            self.images = req.images
            return super().analyze_images(req)

    prov = Spy()
    make_pipeline(cfg, tmp_path, prov).run(video, run_id="team1")
    tcfg = cfg["team"]
    assert prov.calls[:2] == ["team", "overview"]
    assert len(prov.images) == len(tcfg["frame_fractions"]) * len(tcfg["regions"])
    assert all(d[:8] == b"\x89PNG\r\n\x1a\n" and m == "image/png" for d, m in prov.images)


def test_portrait_matching_finds_planted_hero(tmp_path, cfg):
    """Plant a downscaled crop of a reference portrait into a team-list slot of a synthetic frame;
    the matcher must name that hero for the slot and leave the empty slots as 불명."""
    from PIL import Image

    from owcoach.portraits import identify, load_portraits

    tcfg = cfg["team"]
    refs = load_portraits(tcfg["portraits"])["heroes"]
    names = [n for n, _ in refs]
    assert "독트린" in names and "아나" in names and len(set(names)) == len(names) == 54
    slots = tcfg["portrait_slots"]
    name, img = next((n, i) for n, i in refs if n == "독트린")
    small = img.resize((int(img.width * 0.35), int(img.height * 0.35)), Image.BILINEAR)
    patch = small.crop((10, 12, 10 + slots["w"] + 6, 12 + slots["h"] + 6))
    frame = Image.new("RGB", (1280, 720), (90, 90, 120))
    x = slots["right_x"][1]
    frame.paste(patch, (x - 3, slots["y"] - 3))
    png = tmp_path / "f.png"
    frame.save(png)
    video = tmp_path / "f.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(png), "-t", "1", "-pix_fmt", "yuv444p",
                    "-c:v", "libx264", "-crf", "0", str(video)], check=True)
    res = identify(video, [0.5], tcfg, refs)
    assert res[6]["slot"] == 7 and res[6]["hero"] == "독트린" and res[6]["confidence"] == "high"
    assert res[0]["hero"] == "불명"


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
    assert prov.calls == ["team"] + ["overview"] * (cfg["provider"]["json_repair_retries"] + 1)


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
    # two-pane viewer: numbered buttons seek the video to each feedback's start time
    result = AnalysisResult.model_validate_json((run_dir / "result.json").read_text())
    first = sorted(result.feedback, key=lambda f: f.start_time_seconds)[0]
    assert 'class=num type=button data-seek="%s"' % first.start_time_seconds in html and ">1</button>" in html
    assert "grid-template-columns:minmax(0,3fr) minmax(0,2fr)" in html and "아군" in html


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


# ---------------------------------------------------------------- HUD check (no AI)

@pytest.fixture(scope="session")
def hud_video(tmp_path_factory, cfg_static=None) -> Path:
    """Black 1280x720 video: ability_1 tile white (ready) for 0-5s then cooldown digit only (used at 5s);
    ability_2 tile always white so the HUD is not treated as hidden; ultimate disc white 0-8s."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("hud") / "hud.mp4"
    # cooldown tile keeps a small white digit (~10% of the tile), as on the real HUD
    vf = ("drawbox=x=977:y=614:w=34:h=34:color=white:t=fill:enable='lt(t,5)',"
          "drawbox=x=990:y=622:w=8:h=16:color=white:t=fill:enable='gte(t,5)',"
          "drawbox=x=1030:y=614:w=34:h=34:color=white:t=fill,"
          "drawbox=x=618:y=598:w=44:h=44:color=white:t=fill:enable='lt(t,8)'")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=1280x720:rate=30",
                    "-t", "12", "-vf", vf, "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def test_hud_detects_use_and_checks_claims(hud_video, cfg):
    from owcoach.hud import check_use_claim, min_run_for, sample_hud, use_times

    hcfg = cfg["hud_check"]
    samples = sample_hud(hud_video, 0, 12, hcfg)
    assert samples and all(s.states["ability_2"] == "ready" for s in samples)
    assert use_times(samples, "ability_1", min_run_for(hcfg, "ability_1")) == [pytest.approx(5.0, abs=0.5)]
    assert use_times(samples, "ultimate", min_run_for(hcfg, "ultimate")) == [pytest.approx(8.0, abs=0.5)]
    assert check_use_claim(5.5, "ability_1", samples, hcfg)["status"] == "verified"
    bad = check_use_claim(1.0, "ability_1", samples, hcfg)  # still ready around 1s
    assert bad["status"] == "contradicted" and "준비 상태" in bad["note"]
    assert check_use_claim(1.0, "ability_2", samples, hcfg)["status"] == "contradicted"
    # claim far from the only use: contradicted and the actual use time is reported
    far = check_use_claim(11.6, "ultimate", samples, hcfg)
    assert far["status"] == "contradicted" and "8초" in far["note"]


def test_hud_hidden_frames_are_unverifiable(cfg):
    from owcoach.hud import HudSample, check_use_claim

    hcfg = cfg["hud_check"]
    samples = [HudSample(t / 4, {"ultimate": None}, {}) for t in range(40)]
    assert check_use_claim(5.0, "ultimate", samples, hcfg)["status"] == "unverifiable"


def _scene_rec(status):
    return {"scene_id": "scene-01", "window_start": 0.0, "window_end": 20.0, "observations": [], "hud_readings": [],
            "ability_events": [{"time_seconds": 10.0, "ability_id": "nano_boost", "target": "A", "evidence": "e",
                                "hud_check": {"status": status, "hud_slot": "ultimate",
                                              "hud_use_time_seconds": 10.0 if status == "verified" else None,
                                              "note": "n"}}],
            "not_determinable": [], "outcome": "", "key_moment_times": []}


def _coaching_item(**kw):
    base = {"scene_id": "scene-01", "start_time_seconds": 9.0, "end_time_seconds": 12.0, "category": "ultimate_usage",
            "observation": "o", "assessment": "a", "suggestion": "s", "criterion_ids": ["ana.ultimate.nano_boost_usage"],
            "grade": "best", "confidence": 1.0, "uncertainty": "", "uncertainty_level": "low",
            "relies_on_outcome_only": False, "evidence_frame_timestamps": [10.0]}
    item = {**base, **kw}
    if "matched_sign" not in item:
        # quote a real sign of the first cited criterion, as the coaching prompt asks
        c = load_hero("ana").criterion(item["criterion_ids"][0]) if item["criterion_ids"] else None
        signs = (c.bad_signs if item["grade"] == "blunder" else c.good_signs) if c else []
        item["matched_sign"] = signs[0] if signs and item["grade"] != "withheld" else ""
    return item


def test_grade_needs_a_matched_sign(cfg):
    """'best' with no good sign of the cited criterion quoted (a grenade that merely hit something) is withheld."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    ok = _coaching_item()
    vague = _coaching_item(matched_sign="적에게 수류탄이 맞음")
    fb, _ = finalize_feedback(CoachingResponse(items=[ok], action_items=[]), {"scene-01": _scene_rec("verified")},
                              hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "best"
    fb, _ = finalize_feedback(CoachingResponse(items=[vague], action_items=[]), {"scene-01": _scene_rec("verified")},
                              hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "withheld" and any("신호를 특정하지 못해" in r for r in fb[0].display_reasons)


@pytest.mark.parametrize("status,approved,expect", [
    ("verified", True, "assertive"),
    ("verified", False, "tentative"),       # draft criterion
    ("contradicted", True, "tentative"),    # HUD disagrees with the model
    ("unverifiable", True, "tentative"),    # nothing HUD-verified
])
def test_assertive_requires_hud_verification_and_approved_criteria(cfg, status, approved, expect):
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    if approved:
        hero = hero.model_copy(deep=True)
        for c in hero.criteria:
            c.status = "approved"
    coaching = CoachingResponse(items=[_coaching_item()], action_items=[])
    fb, _ = finalize_feedback(coaching, {"scene-01": _scene_rec(status)}, hero, 30.0, cfg["output"],
                              lambda t: f"evidence/{t}.jpg", [])
    assert len(fb) == 1 and fb[0].display_mode == expect
    assert fb[0].hud_checks[0].status == status
    if status == "contradicted":
        assert any("HUD 대조 불일치" in r for r in fb[0].display_reasons)


def test_buff_target_from_team_list_arrow(tmp_path, cfg):
    """A white arrow appears over slot 8's portrait from t=2s: that slot is the nano target; a screen-wide
    flash (all slots white at t=4s) must not be counted for any slot."""
    from owcoach.hud import detect_buff_target

    bcfg = cfg["hud_check"]["buff_arrow"]
    xs = cfg["team"]["portrait_slots"]["right_x"]
    vf = (f"drawbox=x={xs[2] + 8}:y={bcfg['y'] + 2}:w=12:h=12:color=white:t=fill:enable='between(t,2,4.9)',"
          f"drawbox=x={xs[0] - 5}:y={bcfg['y']}:w={xs[-1] - xs[0] + 50}:h={bcfg['h']}:color=white:t=fill:enable='between(t,4,4.2)'")
    video = tmp_path / "arrow.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x502030:size=1280x720:rate=30",
                    "-t", "6", "-vf", vf, "-pix_fmt", "yuv420p", str(video)], check=True)
    allies = [{"slot": 6 + i, "x": x, "player_name": f"P{i}", "hero": None} for i, x in enumerate(xs) if i != 0]
    hit = detect_buff_target(video, 2.0, allies, bcfg)
    assert hit["status"] == "verified" and hit["slot"] == 8 and hit["player_name"] == "P2"
    assert detect_buff_target(video, 0.8, allies, {**bcfg, "window_seconds": 1.0})["status"] == "unverifiable"


def test_time_format_minutes_seconds():
    from owcoach.timefmt import fmt_range, fmt_time, normalize_times

    assert fmt_time(136.0) == "2분 16초" and fmt_time(39.5) == "40초" and fmt_time(120) == "2분 0초"
    assert fmt_range(14.5, 44.0) == "14초 ~ 44초"
    text = "132.75초에 궁극기 100%, 136.00초에 나노. 쿨다운 12초, 2분 16초 그대로"
    assert normalize_times(text) == "2분 13초에 궁극기 100%, 2분 16초에 나노. 쿨다운 12초, 2분 16초 그대로"
    assert normalize_times("134초(2분 14초)에 수면총, 2분 14초(2분 14초)") == "2분 14초에 수면총, 2분 14초"


def test_hud_evidence_applies_to_skill_usage_only(cfg):
    """A positioning item is neither helped by a HUD-verified grenade in its window nor hurt by a
    HUD-contradicted one: HUD checks belong to skill-usage feedback only."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana").model_copy(deep=True)
    for c in hero.criteria:
        c.status = "approved"
    for status in ("verified", "contradicted"):
        item = _coaching_item(category="positioning", criterion_ids=["ana.positioning.cover_access"],
                              observation="132.75초에 모서리 엄폐")
        fb, _ = finalize_feedback(CoachingResponse(items=[item], action_items=[]), {"scene-01": _scene_rec(status)},
                                  hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
        f = fb[0]
        assert f.hud_checks == [] and f.verification == "model_observation"
        # positioning is always held (hold_categories), but never because of HUD results
        assert not any("HUD" in r for r in f.display_reasons) and f.display_mode == "tentative"
        assert f.display_reasons == ["포지셔닝은(는) 코드로 확인할 수 없어 보류"]
        assert f.observation == "2분 13초에 모서리 엄폐"
    skill, _ = finalize_feedback(CoachingResponse(items=[_coaching_item()], action_items=[]),
                                 {"scene-01": _scene_rec("verified")}, hero, 30.0, cfg["output"],
                                 lambda t: f"evidence/{t}.jpg", [])
    assert skill[0].verification == "hud" and skill[0].hud_checks


def test_skill_item_checks_only_its_own_ability(cfg):
    """A nano feedback item is judged by nano HUD checks only, not by an unrelated grenade claim."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana").model_copy(deep=True)
    for c in hero.criteria:
        c.status = "approved"
    rec = _scene_rec("verified")
    rec["ability_events"].append({"time_seconds": 11.0, "ability_id": "biotic_grenade", "target": "", "evidence": "",
                                  "hud_check": {"status": "contradicted", "hud_slot": "ability_2",
                                                "hud_use_time_seconds": None, "note": "n"}})
    fb, _ = finalize_feedback(CoachingResponse(items=[_coaching_item()], action_items=[]), {"scene-01": rec},
                              hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert [c.ability_id for c in fb[0].hud_checks] == ["nano_boost"] and fb[0].display_mode == "assertive"


def test_report_fragment_has_no_document_wrapper(video, cfg, tmp_path):
    from owcoach.report import build_report_html

    _, run_dir = make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="frag1")
    result = AnalysisResult.model_validate_json((run_dir / "result.json").read_text())
    frag = build_report_html(result, "media/web.mp4", fragment=True)
    assert "<html" not in frag and "<body" not in frag and 'src="media/web.mp4"' in frag and "id=save" not in frag


def test_buff_interrupted_when_target_goes_down(tmp_path, cfg):
    """No arrow, but slot 9's portrait turns dark from t=2.5s and stays dark: the buff was cut off."""
    from owcoach.hud import detect_buff_target

    bcfg = cfg["hud_check"]["buff_arrow"]
    xs = cfg["team"]["portrait_slots"]["right_x"]
    boxes = ",".join(f"drawbox=x={x}:y={bcfg['death_y']}:w=34:h={bcfg['death_h']}:color=0xb0b0b0:t=fill" for x in xs)
    vf = boxes + f",drawbox=x={xs[3]}:y={bcfg['death_y']}:w=34:h={bcfg['death_h']}:color=0x505050:t=fill:enable='gte(t,2.5)'"
    video = tmp_path / "down.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x502030:size=1280x720:rate=30",
                    "-t", "6", "-vf", vf, "-pix_fmt", "yuv420p", str(video)], check=True)
    allies = [{"slot": 6 + i, "x": x, "player_name": f"P{i}", "hero": None} for i, x in enumerate(xs) if i != 0]
    res = detect_buff_target(video, 2.0, allies, bcfg)
    assert res["status"] == "interrupted" and res["slot"] == 9 and res["player_name"] == "P3"


def test_hud_ultimate_uses_become_scenes(video, cfg, tmp_path, monkeypatch):
    """An ultimate the HUD shows becomes a scene candidate (category ultimate_usage) and, inside that
    scene, a code-added ability event with the target check, even if the model never mentioned it."""
    import owcoach.pipeline as pl

    monkeypatch.setattr(pl, "scan_uses", lambda video, dur, slot, cfg: [18.5] if slot == "ultimate" else [])
    monkeypatch.setattr(pl, "detect_buff_target", lambda *a, **k: {"status": "interrupted", "slot": 9,
                                                                     "player_name": "S", "hero": "솔저: 76", "note": "n"})
    seen = {}
    real_plan, real_check = pl.plan_scenes, pl.Pipeline._hud_check_scene

    def spy_plan(overview, *a, **k):
        seen["cands"] = [(c.category, c.reason_observed) for c in k.get("forced", [])]
        return real_plan(overview, *a, **k)

    def spy_check(self, video, rec, hcfg):
        real_check(self, video, rec, hcfg)
        seen.setdefault("events", []).extend(e for e in rec["ability_events"] if e.get("source") == "hud")

    monkeypatch.setattr(pl, "plan_scenes", spy_plan)
    monkeypatch.setattr(pl.Pipeline, "_hud_check_scene", spy_check)
    result, _ = make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="hudult")
    assert result.hud_ultimate_uses_seconds == [18.5]
    assert ("ultimate_usage", "HUD: 18초에 궁극기 사용 감지") in seen["cands"]
    ev = seen["events"]
    assert len(ev) == 1 and ev[0]["time_seconds"] == 18.5 and ev[0]["ability_id"] == "nano_boost"
    assert ev[0]["hud_check"]["target_check"]["status"] == "interrupted"


def test_force_scene_skills_become_scenes(video, cfg, tmp_path, monkeypatch):
    """Ana's sleep dart and grenade are force_scene: every HUD-detected use is a forced skill_usage candidate."""
    import owcoach.pipeline as pl

    uses = {"ability_1": [5.0], "ability_2": [12.0, 25.0], "ultimate": []}
    monkeypatch.setattr(pl, "scan_uses", lambda video, dur, slot, cfg: uses.get(slot, []))
    seen = {}
    real_plan = pl.plan_scenes

    def spy_plan(overview, *a, **k):
        seen["cands"] = [(c.category, c.reason_observed) for c in k.get("forced", [])]
        return real_plan(overview, *a, **k)

    monkeypatch.setattr(pl, "plan_scenes", spy_plan)
    make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="hudskill")
    assert ("skill_usage", "HUD: 5초에 수면총 사용 감지") in seen["cands"]
    assert [r for c, r in seen["cands"] if "생체 수류탄" in r] == ["HUD: 12초에 생체 수류탄 사용 감지", "HUD: 25초에 생체 수류탄 사용 감지"]


def test_player_death_from_empty_health_bar(tmp_path, cfg):
    """Health blocks white except 4.0-8.0s (health 0 while dead) and one 0.1s gap at 2s:
    one death at ~4s; the short gap is not a death."""
    from owcoach.hud import scan_player_deaths

    dcfg = cfg["death_check"]
    hb = dcfg["health_bar"]
    vf = (f"drawbox=x={hb['x0']}:y={hb['y0']}:w={hb['x1'] - hb['x0']}:h={hb['y1'] - hb['y0']}:color=white:t=fill"
          ":enable='not(between(t,4,8))*not(between(t,2,2.1))'")
    video = tmp_path / "death.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=1280x720:rate=30",
                    "-t", "12", "-vf", vf, "-pix_fmt", "yuv420p", str(video)], check=True)
    assert scan_player_deaths(video, 12.0, dcfg) == [pytest.approx(4.0, abs=0.5)]


def test_deaths_force_scenes_and_coverage_warning(video, cfg, tmp_path, monkeypatch):
    """A death the model did not pick becomes a scene covering the 10 s before it, the scene record
    carries it, and a missing feedback for that window is reported as a warning."""
    import owcoach.pipeline as pl

    monkeypatch.setattr(pl, "scan_dead_periods", lambda *a, **k: [(17.0, 19.5)])
    seen = {}
    real_plan = pl.plan_scenes

    def spy_plan(overview, *a, **k):
        seen["cands"] = [(c.start_time_seconds, c.end_time_seconds, c.reason_observed) for c in k.get("forced", [])]
        scenes = real_plan(overview, *a, **k)
        seen["windows"] = [(s.window_start, s.window_end) for s in scenes]
        return scenes

    monkeypatch.setattr(pl, "plan_scenes", spy_plan)
    result, _ = make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="death1")
    assert result.player_deaths == [{"time_seconds": 17.0, "source": "체력 0", "respawn_seconds": 19.5}]
    assert any(c[0] == 7.0 and c[1] == 17.0 and "사망" in c[2] for c in seen["cands"])
    assert any(a <= 7.0 and b >= 17.0 for a, b in seen["windows"])
    covered = any(f.start_time_seconds <= 18 and f.end_time_seconds >= 7 for f in result.feedback)
    assert covered or any("사망 17초" in w for w in result.warnings)


def test_model_death_inside_dead_period_is_same_death(cfg, monkeypatch):
    """The overview logs a death 9 s after health hit 0 (still waiting to respawn): one death, not two.
    A model-only death far from any health-0 stretch is kept on its own."""
    import owcoach.pipeline as pl
    from owcoach.schemas import OverviewResponse

    monkeypatch.setattr(pl, "scan_dead_periods", lambda *a, **k: [(138.5, 152.0)])
    ov = OverviewResponse(hero_confirmed=True, hero_evidence="", hud_readable="yes", video_quality_notes="",
                          candidates=[], timeline=[
                              {"time_seconds": 147.0, "event": "플레이어 사망", "visible_evidence": ""},
                              {"time_seconds": 250.0, "event": "처치당함", "visible_evidence": ""}])
    pipe = make_pipeline(cfg, Path("/tmp"))
    deaths = pipe._find_deaths(Path("x.mp4"), 290.0, ov, cfg["hud_check"])
    assert [(d["time_seconds"], d["source"]) for d in deaths] == [(138.5, "체력 0 + 모델 기록"), (250.0, "모델 기록")]


def test_forced_scenes_do_not_count_toward_the_limit(cfg):
    """With max_candidates=1 the overview keeps only its best pick, but every forced (death / ultimate)
    candidate is still planned."""
    from owcoach.schemas import SceneCandidate

    dcfg = {**cfg["detail"], "pre_seconds": 1, "post_seconds": 1, "merge_gap_seconds": 0, "max_window_seconds": 30}
    cands = [{"start_time_seconds": 10, "end_time_seconds": 12, "category": "positioning", "reason_observed": "a", "priority": 4},
             {"start_time_seconds": 30, "end_time_seconds": 32, "category": "positioning", "reason_observed": "b", "priority": 2}]
    forced = [SceneCandidate(start_time_seconds=t - 10, end_time_seconds=t, category="positioning",
                             reason_observed=f"death {t}", priority=5) for t in (60, 90, 120)]
    discarded = []
    scenes = plan_scenes(_overview(cands), 200.0, dcfg, 1, discarded, forced=forced)
    assert [(s.window_start, s.window_end) for s in scenes] == [(9, 13), (49, 61), (79, 91), (109, 121)]
    assert [d.reason for d in discarded] == ["후보 장면 최대 개수 초과"]


def test_death_and_ultimate_feedback_exceed_the_limit(cfg):
    """max_feedback_items=1: one ordinary item survives, plus the death and the HUD-verified ultimate items."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["window_end"] = 40.0
    items = [
        _coaching_item(),  # HUD-verified nano (ultimate_usage)
        _coaching_item(category="positioning", criterion_ids=["ana.positioning.cover_access"],
                       start_time_seconds=25.0, end_time_seconds=35.0, evidence_frame_timestamps=[30.0]),  # ends at death 35
        _coaching_item(category="skill_usage", criterion_ids=["ana.skill.sleep_dart_usage"],
                       start_time_seconds=1.0, end_time_seconds=3.0, evidence_frame_timestamps=[2.0]),
        _coaching_item(category="skill_usage", criterion_ids=["ana.skill.biotic_grenade_usage"],
                       start_time_seconds=15.0, end_time_seconds=17.0, evidence_frame_timestamps=[16.0]),
    ]
    discarded = []
    fb, _ = finalize_feedback(CoachingResponse(items=items, action_items=[]), {"scene-01": rec}, hero, 60.0,
                              {**cfg["output"], "max_feedback_items": 1}, lambda t: f"evidence/{t}.jpg", discarded,
                              death_times=[35.0])
    cats = sorted(f.category for f in fb)
    assert cats == ["positioning", "skill_usage", "ultimate_usage"]
    assert [d.reason for d in discarded] == ["피드백 최대 개수 초과"]


def test_forced_skill_feedback_exceeds_the_limit(cfg):
    """max_feedback_items=0: a HUD-verified sleep dart (force_scene) item is still kept; an unverified one is not."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["ability_events"].append({"time_seconds": 2.0, "ability_id": "sleep_dart", "target": "", "evidence": "e",
                                  "hud_check": {"status": "verified", "hud_slot": "ability_1",
                                                "hud_use_time_seconds": 2.0, "note": "n"}})
    items = [
        _coaching_item(category="skill_usage", criterion_ids=["ana.skill.sleep_dart_usage"],
                       start_time_seconds=1.0, end_time_seconds=3.0, evidence_frame_timestamps=[2.0]),
        _coaching_item(category="skill_usage", criterion_ids=["ana.skill.biotic_grenade_usage"],
                       start_time_seconds=15.0, end_time_seconds=17.0, evidence_frame_timestamps=[16.0]),
    ]
    discarded = []
    fb, _ = finalize_feedback(CoachingResponse(items=items, action_items=[]), {"scene-01": rec}, hero, 60.0,
                              {**cfg["output"], "max_feedback_items": 0}, lambda t: f"evidence/{t}.jpg", discarded)
    assert [f.start_time_seconds for f in fb] == [1.0]


def test_common_knowledge_applies_to_every_hero():
    """heroes/common.yaml (e.g. no skills into an enemy Genji's deflect) is merged into each hero and is
    not itself offered as a hero."""
    assert "common" not in available_heroes()
    hero = load_hero("ana")
    c = hero.criterion("common.skill.no_skills_into_deflect")
    assert c and c.category == "skill_usage" and c.source.type == "user_coaching"
    assert any("튕겨내기" in s for s in hero.observable_signals)
    assert "+common-" in hero.profile_version
    assert any(x["id"] == c.id for x in hero.criteria_for_prompt() if "id" in x)


def test_moment_check_runs_for_each_hud_verified_skill(video, cfg, tmp_path, monkeypatch):
    """A HUD-detected grenade gets one short high-fps look (stage 'moment'); its answer is attached to the
    event's hud_check, cached on re-run, and the counter list comes from heroes/common.yaml."""
    import owcoach.pipeline as pl

    monkeypatch.setattr(pl.Pipeline, "_hud_uses_in",
                        lambda self, video, s, e, h: [{"ability_id": "biotic_grenade", "time_seconds": 10.0}] if s <= 10.0 <= e else [])
    prompts = []

    class Spy(MockProvider):
        def analyze_video(self, req):
            if req.context["stage"] == "moment":
                prompts.append((req.prompt, req.fps))
            return super().analyze_video(req)

    seen = {}
    real_check = pl.Pipeline._hud_check_scene

    def spy_check(self, video, rec, hcfg):
        real_check(self, video, rec, hcfg)
        seen.setdefault("recs", []).append(rec)

    monkeypatch.setattr(pl.Pipeline, "_hud_check_scene", spy_check)
    make_pipeline(cfg, tmp_path, Spy()).run(video, run_id="moment1")
    assert len(prompts) == 1 and prompts[0][1] == cfg["moment_check"]["fps"]
    assert "생체 수류탄" in prompts[0][0] and "겐지 튕겨내기" in prompts[0][0]
    ev = [e for r in seen["recs"] for e in r["ability_events"] if e.get("source") == "hud"]
    assert ev and ev[0]["hud_check"]["moment_check"]["counter_active"] == "no"
    prompts.clear()
    make_pipeline(cfg, tmp_path, Spy()).run(video, run_id="moment1")
    assert prompts == []  # cached


def test_evaluate_against_user_labels(video, cfg, tmp_path):
    from owcoach.evaluate import evaluate

    result, run_dir = make_pipeline(cfg, tmp_path, MockProvider()).run(video, run_id="eval1")
    labels = tmp_path / "labels.yaml"
    labels.write_text(
        f'video_sha12: "{result.video.sha256[:12]}"\nhero: ana\n'
        "facts:\n  deaths: [{time_seconds: 5.0}]\n  roster: [{slot: 1, hero: \"아나\"}]\n"
        "judgements: [{time_seconds: 999.0, category: skill_usage, grade: blunder}]\n", "utf-8")
    rep = evaluate(run_dir, labels)
    kinds = {(c["kind"], c["ok"]) for c in rep["checks"]}
    assert ("사망 감지", False) in kinds and ("사용자 판정 일치", False) in kinds
    bad = tmp_path / "other.yaml"
    bad.write_text('video_sha12: "000000000000"\nhero: ana\n', "utf-8")
    with pytest.raises(ValueError, match="영상이 다릅니다"):
        evaluate(run_dir, bad)


def test_disable_then_death_withholds_good_grade(cfg):
    """A sleep dart 1.5 s before the player's death cannot back a 'best' grade (a hit would have stopped
    the attacker); a blunder on the same evidence is kept."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["ability_events"][0].update(ability_id="sleep_dart")
    rec["ability_events"][0]["hud_check"].update(hud_slot="ability_1", death_after_seconds=1.5)
    for grade, want in (("best", "withheld"), ("blunder", "blunder")):
        item = _coaching_item(category="skill_usage", grade=grade, criterion_ids=["ana.skill.sleep_dart_usage"])
        fb, _ = finalize_feedback(CoachingResponse(items=[item], action_items=[]), {"scene-01": rec}, hero, 30.0,
                                  cfg["output"], lambda t: f"evidence/{t}.jpg", [])
        assert fb[0].grade == want
    assert any("명중 여부 미확인" in r for r in fb[0].display_reasons) is False


def test_unverifiable_criterion_cannot_grade(cfg):
    """A blunder citing the deflect criterion (enemy counter seen by the model, not by code) is withheld,
    alone or alongside another criterion (video2 2:17: an invented deflect next to the sleep dart criterion)."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    only = _coaching_item(category="skill_usage", grade="blunder", criterion_ids=["common.skill.no_skills_into_deflect"])
    fb, _ = finalize_feedback(CoachingResponse(items=[only], action_items=[]), {"scene-01": _scene_rec("verified")},
                              hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "withheld" and any("판단 보류" in r for r in fb[0].display_reasons)
    mixed = _coaching_item(category="skill_usage", grade="blunder",
                           criterion_ids=["common.skill.no_skills_into_deflect", "ana.skill.sleep_dart_usage"])
    fb, _ = finalize_feedback(CoachingResponse(items=[mixed], action_items=[]), {"scene-01": _scene_rec("verified")},
                              hero, 30.0, cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "withheld"


def test_sleep_hit_from_enemy_status_icon(tmp_path, cfg):
    """A white "zz" over enemy slot 5's portrait from 2.5 s: the sleep dart at 2 s hit that enemy;
    with no icon anywhere it missed."""
    from owcoach.hud import detect_status_icon

    icfg = cfg["hud_check"]["sleep_icon"]
    xs = cfg["team"]["portrait_slots"]["left_x"]
    vf = f"drawbox=x={xs[4] + icfg['dx0'] + 2}:y={icfg['y'] + 1}:w=10:h=7:color=white:t=fill:enable='between(t,2.5,4.5)'"
    video = tmp_path / "zz.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x2a6fb8:size=1280x720:rate=30",
                    "-t", "6", "-vf", vf, "-pix_fmt", "yuv420p", str(video)], check=True)
    enemies = [{"slot": i + 1, "x": x, "player_name": f"E{i}", "hero": None} for i, x in enumerate(xs)]
    hit = detect_status_icon(video, 2.0, enemies, icfg)
    assert hit["status"] == "verified" and hit["slot"] == 5
    assert detect_status_icon(video, 0.8, enemies, {**icfg, "window_seconds": 1.0})["status"] == "missed"


def test_missed_sleep_withholds_good_grade(cfg):
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["ability_events"][0].update(ability_id="sleep_dart")
    rec["ability_events"][0]["hud_check"].update(hud_slot="ability_1", target_check={"status": "missed", "note": "zz 없음"})
    item = _coaching_item(category="skill_usage", grade="excellent", criterion_ids=["ana.skill.sleep_dart_usage"])
    fb, _ = finalize_feedback(CoachingResponse(items=[item], action_items=[]), {"scene-01": rec}, hero, 30.0,
                              cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "withheld" and any("빗나감" in r for r in fb[0].display_reasons)


def test_reflected_grenade_from_self_antiheal_icon(tmp_path, cfg):
    """Purple anti-heal icon left of the health number from 2.3 s to 4.5 s: the grenade at 2 s came back.
    A 0.25 s purple flash (enemy ultimate effect) at 5.5 s is not a reflection."""
    from owcoach.hud import detect_reflect

    rcfg = cfg["hud_check"]["self_antiheal"]
    box = f"x={rcfg['x0']}:y={rcfg['y0']}:w={rcfg['x1'] - rcfg['x0']}:h={rcfg['y1'] - rcfg['y0']}:color=0xc040f0:t=fill"
    vf = f"drawbox={box}:enable='between(t,2.3,4.5)',drawbox={box}:enable='between(t,5.5,5.75)'"
    video = tmp_path / "antiheal.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:size=1280x720:rate=30",
                    "-t", "8", "-vf", vf, "-pix_fmt", "yuv444p", str(video)], check=True)
    hit = detect_reflect(video, 2.0, rcfg)
    assert hit["status"] == "reflected" and abs(hit["onset_seconds"] - 2.3) <= 0.2
    assert detect_reflect(video, 5.3, rcfg)["status"] == "not_reflected"


def test_reflected_grenade_is_a_blunder(cfg):
    """A grenade the code saw come back is a blunder even if the model called it best, and cites the
    deflect criterion."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["ability_events"][0].update(ability_id="biotic_grenade")
    rec["ability_events"][0]["hud_check"].update(hud_slot="ability_2", reflect_check={"status": "reflected", "note": "반사됨"})
    item = _coaching_item(category="skill_usage", grade="best", criterion_ids=["ana.skill.biotic_grenade_usage"])
    fb, _ = finalize_feedback(CoachingResponse(items=[item], action_items=[]), {"scene-01": rec}, hero, 30.0,
                              cfg["output"], lambda t: f"evidence/{t}.jpg", [])
    assert fb[0].grade == "blunder"
    assert "common.skill.no_skills_into_deflect" in fb[0].criterion_ids
    assert any("반사됨" in r for r in fb[0].display_reasons)


def test_ally_color_from_player_portrait_border(tmp_path, cfg):
    """A red border around the bottom-left portrait means allies are red; the team context says so and
    overrides the team stage's guess."""
    from owcoach.hud import player_team_color
    from owcoach.pipeline import team_context_text

    pcfg = cfg["team"]["player_portrait"]
    vf = f"drawbox=x={pcfg['x0'] + 10}:y={pcfg['y0'] + 10}:w=40:h=70:color=0xe02050:t=4"
    video = tmp_path / "portrait.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x303030:size=1280x720:rate=30",
                    "-t", "4", "-vf", vf, "-pix_fmt", "yuv444p", str(video)], check=True)
    color = player_team_color(video, 4.0, {**pcfg, "frames": 3})
    assert color["ally_color"] == "빨강"
    team = {"team_identification": {"player_name": "P", "ally_side": "right", "ally_color": "파랑",
                                    "enemy_color": "빨강", "evidence": "e"}, "roster": []}
    text = team_context_text(team, color)
    assert "아군 색 빨강" in text and "\"빨강=적\" 관례" in text


def test_comp_notes_and_hero_swaps_in_team_context():
    """A coach comp note applies once our team (and the enemy, for a versus note) matches 4 of 5 heroes,
    using the heroes in play after swaps; swaps are listed with their time."""
    from owcoach.pipeline import team_context_text

    def r(slot, team, name, hist):
        return {"slot": slot, "team": team, "player_name": name, "hero": hist[-1][0], "hero_confidence": "high",
                "hero_history": [{"hero": h, "start_seconds": a, "end_seconds": b} for h, a, b in hist]}

    allies = [r(6, "ally", "A", [("아나", 0, 300)]), r(7, "ally", "B", [("시그마", 0, 300)]),
              r(8, "ally", "C", [("위도우메이커", 0, 80), ("파라", 81, 300)]), r(9, "ally", "D", [("솔저: 76", 0, 300)]),
              r(10, "ally", "E", [("독트린", 0, 300)])]
    enemies = [r(1, "enemy", "F", [("D.Va", 0, 300)]), r(2, "enemy", "G", [("주노", 0, 300)]),
               r(3, "enemy", "H", [("라이프위버", 0, 300)]), r(4, "enemy", "I", [("소전", 0, 300)]),
               r(5, "enemy", "J", [("시에라", 0, 300)])]
    team = {"team_identification": {"player_name": "A", "ally_side": "right", "ally_color": "빨강",
                                    "enemy_color": "파랑", "evidence": "e"},
            "roster": enemies + allies,
            "hero_swaps": [{"slot": 8, "from_hero": "위도우메이커", "to_hero": "파라", "time_seconds": 81.0, "player_name": "C"}]}
    notes = [{"team": ["시그마", "위도우메이커", "솔저: 76", "독트린", "아나"], "note": "대치 조합"},
             {"team": ["시그마", "위도우메이커", "솔저: 76", "독트린", "아나"], "versus": ["겐지", "트레이서", "a", "b", "c"],
              "note": "다른 상대"}]
    text = team_context_text(team, None, notes)
    assert "영웅 교체 (코드 확인): 1분 21초경 아군 C 위도우메이커 → 파라" in text
    assert "코치 조합 메모 (0초부터 해당하는 조합): 대치 조합" in text and "다른 상대" not in text
    assert "위도우메이커(0초~) → 파라(1분 21초~)" in text


def test_roster_timeline_finds_a_swap(tmp_path, cfg):
    """Slot 1 shows one reference portrait for 0-20 s and another from 20 s: one swap near 20 s;
    a single odd frame does not count."""
    from PIL import Image
    from owcoach.portraits import load_portraits, roster_timeline

    refs = load_portraits(cfg["team"]["portraits"])["heroes"][:3]
    ps = cfg["team"]["portrait_slots"]
    x, y, w, h = ps["left_x"][0], ps["y"], ps["w"], ps["h"]
    s = 0.35
    crops = []
    for name, img in refs[:2]:
        t = img.resize((int(img.width * s), int(img.height * s)))
        crops.append(t.crop((0, 0, w, h)))
    for i, c in enumerate(crops):
        c.save(tmp_path / f"p{i}.png")
    video = tmp_path / "swap.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x202020:size=1280x720:rate=10",
                    "-i", str(tmp_path / "p0.png"), "-i", str(tmp_path / "p1.png"), "-filter_complex",
                    f"[0][1]overlay={x}:{y}:enable='lt(t,20)'[a];[a][2]overlay={x}:{y}:enable='gte(t,20)'",
                    "-t", "40", "-pix_fmt", "yuv444p", str(video)], check=True)
    tcfg = {**cfg["team"], "portrait_slots": {**ps, "left_x": [x], "right_x": [],
                                              "timeline": {"step_seconds": 4.0, "min_run": 2, "refine_seconds": 1.0}}}
    tl = roster_timeline(video, 40.0, tcfg, refs)
    assert [(w["from_hero"], w["to_hero"]) for w in tl["swaps"]] == [(refs[0][0], refs[1][0])]
    assert abs(tl["swaps"][0]["time_seconds"] - 20.0) <= 1.5


def test_positioning_good_grade_needs_a_specific_clean_moment(cfg):
    """Best positioning over a whole 30 s scene is withheld; so is a short one during which the situation
    track shows the player moving with the back to the team twice in a row."""
    from owcoach.postprocess import finalize_feedback

    hero = load_hero("ana")
    rec = _scene_rec("verified")
    rec["window_end"] = 40.0
    rec["situation_track"] = [{"time_seconds": t, "tank": "앞 가까이", "player_moving": "yes",
                               "facing_allies": "no" if 20 <= t <= 26 else "yes"} for t in range(0, 40, 3)]
    pos = dict(category="positioning", criterion_ids=["ana.positioning.cover_access"])
    items = [_coaching_item(**pos, start_time_seconds=2.0, end_time_seconds=34.0, evidence_frame_timestamps=[10.0]),
             _coaching_item(**pos, start_time_seconds=19.0, end_time_seconds=27.0, evidence_frame_timestamps=[22.0]),
             _coaching_item(**pos, start_time_seconds=3.0, end_time_seconds=9.0, evidence_frame_timestamps=[5.0])]
    by_start = {}
    for it in items:  # one at a time: overlapping items of one category would be merged as duplicates
        fb, _ = finalize_feedback(CoachingResponse(items=[it], action_items=[]), {"scene-01": rec}, hero, 60.0,
                                  cfg["output"], lambda t: f"evidence/{t}.jpg", [])
        by_start[fb[0].start_time_seconds] = fb[0]
    assert by_start[2.0].grade == "withheld" and any("특정 순간" in r for r in by_start[2.0].display_reasons)
    assert by_start[19.0].grade == "withheld" and any("본대를 등짐" in r for r in by_start[19.0].display_reasons)
    assert by_start[3.0].grade == "best"

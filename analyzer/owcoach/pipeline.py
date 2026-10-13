"""Local analysis pipeline.

Stages (each persisted under runs/<run_id>/ so a re-run with the same run id reuses finished
stages and does not pay for the same API call twice):

  1. probe     - duration / resolution / input conditions (ffprobe)
  2. proxy     - upload copy with identical timeline (ffmpeg)
     overview  - whole-video flow + scene candidates (AI, video)
  3. detail    - re-analysis of each candidate window incl. pre/post range (AI, video clip)
  4. coaching  - hero criteria applied to observed facts (AI, text only)
  5. finalize  - deterministic validation / dedup / evidence frames (no AI)
"""

from __future__ import annotations

import copy
import fcntl
import json
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import __version__
from .config import ANALYZER_ROOT, fingerprint
from .hud import check_use_claim, sample_hud
from .heroes import HeroProfile
from .media import (
    InputRejected,
    cut_clip,
    extract_frame,
    make_proxy,
    probe,
    sha256_file,
    validate_input,
)
from .postprocess import Scene, detail_to_original, finalize_feedback, plan_scenes, validate_result
from .prompts import load_prompt
from .providers import AnalysisProvider, ProviderError, TextRequest, VideoRequest
from .schemas import (
    AnalysisResult,
    CoachingResponse,
    CostSummary,
    DetailResponse,
    DiscardedItem,
    OverviewResponse,
    StageUsage,
    Versions,
    VideoInfo,
)

DEFAULT_RUNS_DIR = ANALYZER_ROOT / "runs"
MOCK_NOTICE = "모의(mock) 응답으로 생성된 결과입니다. 실제 AI 분석 결과가 아닙니다."


class StageFailed(Exception):
    def __init__(self, stage: str, reason: str):
        self.stage, self.reason = stage, reason
        super().__init__(f"[{stage}] {reason}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def inline_refs(schema: dict) -> dict:
    """Inline $defs/$ref so the schema is self-contained (safer for structured-output APIs)."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def compute_cost(usage: list[StageUsage], pricing: dict, is_mock: bool) -> CostSummary:
    if is_mock:
        return CostSummary(note="모의 실행: API를 호출하지 않아 비용이 발생하지 않았습니다.")
    billable = [u for u in usage if not u.from_cache]
    if not billable:
        return CostSummary(note="모든 단계가 캐시에서 재사용되어 이번 실행에서 새 API 호출이 없었습니다.")
    models = pricing.get("models") or {}
    total, currency, sources = 0.0, None, set()
    for u in billable:
        p = models.get(u.model)
        if not p:
            return CostSummary(note=f"모델 '{u.model}'의 확인된 단가가 config/pricing.yaml에 없어 비용을 계산하지 않았습니다. 토큰 사용량만 기록합니다.")
        if u.input_tokens is None or u.output_tokens is None:
            return CostSummary(note="일부 호출의 토큰 사용량이 응답에 없어 비용을 계산하지 않았습니다.")
        out_tokens = u.output_tokens + (u.thinking_tokens or 0)
        total += u.input_tokens / 1e6 * p["input_per_million_tokens"] + out_tokens / 1e6 * p["output_per_million_tokens"]
        currency = p.get("currency", "USD")
        sources.add(f"{p.get('source_url')} (확인일 {p.get('verified_on')})")
    return CostSummary(
        total=round(total, 6), currency=currency,
        note="추정치: 토큰 수 × pricing.yaml 단가. thinking 토큰은 출력 단가로 계산. 단가 출처: " + ", ".join(sorted(sources)),
    )


def team_context_text(overview: OverviewResponse) -> str:
    """Team / roster facts from the overview, handed to the detail stage as plain text."""
    ti = overview.team_identification
    side = {"left": "왼쪽", "right": "오른쪽", "unknown": "판별 못 함"}[ti.ally_side]
    lines = [f"- 플레이어 이름: {ti.player_name or '판독 못 함'}",
             f"- 아군 팀: 상단 팀 목록 {side}, 아군 색 {ti.ally_color}, 적 색 {ti.enemy_color}",
             f"- 판단 근거: {ti.evidence}"]
    team_ko = {"ally": "아군", "enemy": "적", "unknown": "소속 불명"}
    for r in overview.roster:
        lines.append(f"- {r.player_name}: {team_ko[r.team]}, 영웅 {r.hero} (초상화 판독 확신도 {r.hero_confidence})")
    return "\n".join(lines)


class RunStore:
    """Files of one analysis job. run.json keeps per-stage status for display and retry."""

    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.stages_dir = run_dir / "stages"
        self.stages_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = run_dir / "run.json"
        self.meta = json.loads(self.meta_path.read_text("utf-8")) if self.meta_path.exists() else {"stages": {}}

    def save_meta(self) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.meta_path)

    def set_stage(self, name: str, **fields) -> None:
        st = self.meta["stages"].setdefault(name, {})
        st.update(fields)
        self.save_meta()

    def stage_path(self, name: str) -> Path:
        return self.stages_dir / f"{name}.json"

    def load_stage(self, name: str, fp: str) -> dict | None:
        p = self.stage_path(name)
        if not p.exists():
            return None
        data = json.loads(p.read_text("utf-8"))
        return data if data.get("fingerprint") == fp else None

    def save_stage(self, name: str, data: dict) -> None:
        p = self.stage_path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(p)

    @contextmanager
    def lock(self):
        f = open(self.dir / ".lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise StageFailed("lock", f"작업 {self.dir.name} 이(가) 이미 실행 중입니다. 중복 실행을 막았습니다.")
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


class Pipeline:
    def __init__(self, cfg: dict, pricing: dict, provider: AnalysisProvider, hero: HeroProfile,
                 runs_dir: Path = DEFAULT_RUNS_DIR, log: Callable[[str], None] = print):
        self.cfg, self.pricing, self.provider, self.hero = cfg, pricing, provider, hero
        self.runs_dir = Path(runs_dir)
        self.log = log
        self.prompts = {k: load_prompt(k) for k in ("overview", "detail", "coaching")}
        self.usage: list[StageUsage] = []
        self.timings: dict[str, float] = {}
        self.warnings: list[str] = []
        self.discarded: list[DiscardedItem] = []

    # ---------------------------------------------------------------- AI call helper

    def _call(self, stage: str, prompt_id: str, model_cls: type[BaseModel], make_request) -> tuple[BaseModel, list[dict]]:
        """Call provider, validate against the stage schema; re-ask on schema mismatch."""
        schema = inline_refs(model_cls.model_json_schema())
        repairs = int(self.cfg["provider"].get("json_repair_retries", 1))
        usage_records, note = [], ""
        for attempt in range(repairs + 1):
            req = make_request(schema, note)
            try:
                resp = (self.provider.analyze_video(req) if isinstance(req, VideoRequest)
                        else self.provider.generate_json(req))
            except ProviderError as e:
                raise StageFailed(stage, str(e)) from e
            usage_records.append(StageUsage(
                stage=stage, model=resp.model, prompt_version=self.prompts[prompt_id].version,
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                thinking_tokens=resp.usage.thinking_tokens, total_tokens=resp.usage.total_tokens,
                input_tokens_by_modality=resp.usage.input_tokens_by_modality,
                latency_seconds=resp.latency_seconds, attempts=resp.attempts, retry_errors=resp.retry_errors,
            ).model_dump())
            try:
                return model_cls.model_validate(resp.data), usage_records
            except ValidationError as e:
                note = f"\n\n이전 응답이 스키마 검증에 실패했습니다. 오류: {str(e)[:800]}\n스키마에 맞게 다시 응답하세요."
                self.log(f"  {stage}: 응답 스키마 불일치, 재요청 ({attempt + 1}/{repairs})")
        # usage is still recorded so failed attempts are accounted for
        self._record_usage(usage_records)
        raise StageFailed(stage, "모델 응답이 스키마 검증을 통과하지 못했습니다.")

    def _record_usage(self, records: list[dict], from_cache: bool = False) -> None:
        for r in records:
            self.usage.append(StageUsage(**{**r, "from_cache": from_cache}))

    def _cached_or_run(self, store: RunStore, name: str, fp: str, fn: Callable[[], dict], force: bool) -> dict:
        cached = None if force else store.load_stage(name, fp)
        if cached is not None:
            self.log(f"  {name}: 캐시 재사용 (API 호출 없음)")
            self._record_usage(cached.get("usage", []), from_cache=True)
            store.set_stage(name, status="done", reused=True)
            return cached["output"]
        store.set_stage(name, status="running", started_at=now_iso(), error=None, reused=False)
        t0 = time.monotonic()
        try:
            output, usage = fn()
        except StageFailed as e:
            store.set_stage(name, status="failed", error=e.reason, finished_at=now_iso())
            raise
        except Exception as e:
            store.set_stage(name, status="failed", error=f"{type(e).__name__}: {e}", finished_at=now_iso())
            raise StageFailed(name, f"{type(e).__name__}: {e}") from e
        elapsed = round(time.monotonic() - t0, 3)
        self._record_usage(usage)
        store.save_stage(name, {"fingerprint": fp, "output": output, "usage": usage, "elapsed_seconds": elapsed,
                                "created_at": now_iso()})
        store.set_stage(name, status="done", finished_at=now_iso(), elapsed_seconds=elapsed)
        return output

    def _hud_check_scene(self, video: Path, rec: dict, hcfg: dict) -> None:
        slots = {a.id: a.hud_slot for a in self.hero.abilities}
        events = rec["ability_events"]
        if not events:
            return
        if not hcfg.get("enabled"):
            for e in events:
                e["hud_check"] = {"status": "not_checked", "hud_slot": None, "hud_use_time_seconds": None,
                                  "note": "HUD 대조 비활성화"}
            return
        pad = float(hcfg["search_seconds"]) + 1.0
        samples = sample_hud(video, rec["window_start"] - pad, rec["window_end"] + pad, hcfg)
        for e in events:
            slot = slots.get(e["ability_id"])
            if e["ability_id"] not in slots:
                e["hud_check"] = {"status": "not_checked", "hud_slot": None, "hud_use_time_seconds": None,
                                  "note": f"알 수 없는 스킬 id: {e['ability_id']}"}
            elif not slot or slot not in hcfg["regions"]:
                e["hud_check"] = {"status": "not_checked", "hud_slot": slot, "hud_use_time_seconds": None,
                                  "note": "HUD 대조 영역이 없는 스킬"}
            else:
                e["hud_check"] = {"hud_slot": slot, **check_use_claim(e["time_seconds"], slot, samples, hcfg)}

    # ---------------------------------------------------------------- run

    def run(self, video: Path, run_id: str | None = None, game_patch: str | None = None,
            force_stages: set[str] = frozenset()) -> tuple[AnalysisResult, Path]:
        video = Path(video).resolve()
        t_total = time.monotonic()
        t0 = time.monotonic()
        sha = sha256_file(video)
        run_id = run_id or f"{sha[:12]}-{self.hero.hero_id}"
        store = RunStore(self.runs_dir / run_id)
        store.meta.update({"run_id": run_id, "video": str(video), "hero": self.hero.hero_id,
                           "provider": self.provider.name, "updated_at": now_iso()})
        store.meta.setdefault("created_at", now_iso())
        store.save_meta()
        with store.lock():
            store.meta["status"] = "running"
            store.save_meta()
            try:
                result = self._run_locked(store, video, sha, run_id, game_patch, force_stages, t0)
            except StageFailed as e:
                store.meta.update(status="failed", failed_stage=e.stage, error=e.reason, updated_at=now_iso())
                store.save_meta()
                raise
            except InputRejected as e:
                store.meta.update(status="failed", failed_stage="probe", error="; ".join(e.reasons), updated_at=now_iso())
                store.save_meta()
                raise
            except Exception as e:
                store.meta.update(status="failed", error=f"{type(e).__name__}: {e}", updated_at=now_iso())
                store.save_meta()
                raise
            self.timings["total"] = round(time.monotonic() - t_total, 3)
            result.timings_seconds = self.timings
            (store.dir / "result.json").write_text(result.model_dump_json(indent=2), "utf-8")
            store.meta.update(status="done", failed_stage=None, error=None, updated_at=now_iso(),
                              analysis_mode=result.analysis_mode)
            store.save_meta()
        return result, store.dir

    def _run_locked(self, store: RunStore, video: Path, sha: str, run_id: str, game_patch: str | None,
                    force: set[str], t0: float) -> AnalysisResult:
        cfg, hero, prov = self.cfg, self.hero, self.provider
        ai_fp_base = {"provider": prov.name, "model": prov.model, "hero": hero.hero_id,
                      "hero_profile": hero.profile_version}

        # 1. probe / input conditions -------------------------------------------------
        self.log("[1/5] 입력 확인")
        store.set_stage("probe", status="running", started_at=now_iso())
        info = probe(video)
        reasons = validate_input(video, info, cfg["input"])
        if reasons:
            store.set_stage("probe", status="failed", error="; ".join(reasons))
            raise StageFailed("probe", "입력 조건 불충족: " + " / ".join(reasons))
        store.save_stage("probe", {"fingerprint": sha, "output": info.to_dict()})
        store.set_stage("probe", status="done", finished_at=now_iso())
        duration = info.duration_seconds
        self.timings["probe"] = round(time.monotonic() - t0, 3)

        # proxy (upload copy with same timeline)
        t = time.monotonic()
        pcfg = cfg["preprocess"]["proxy"]
        proxy_path = store.dir / "media" / "proxy.mp4"
        proxy_fp = fingerprint(sha, pcfg)
        if pcfg["enabled"]:
            if force & {"proxy"} or store.load_stage("proxy", proxy_fp) is None or not proxy_path.exists():
                self.log("  프록시 영상 생성")
                make_proxy(video, proxy_path, pcfg["max_height"], pcfg["crf"], pcfg["fps"])
                store.save_stage("proxy", {"fingerprint": proxy_fp, "output": {"path": "media/proxy.mp4"}})
            upload_src = proxy_path
        else:
            upload_src = video
        store.set_stage("proxy", status="done")
        self.timings["preprocess"] = round(time.monotonic() - t, 3)

        # 2. overview -------------------------------------------------------------------
        self.log("[2/5] 전체 흐름 분석")
        t = time.monotonic()
        ocfg = cfg["overview"]
        op = self.prompts["overview"]
        overview_fp = fingerprint(ai_fp_base, proxy_fp, ocfg, op.version, hero.observable_signals)
        prompt = op.render(
            hero_name_ko=hero.display_name_ko, hero_name_en=hero.display_name_en,
            duration_seconds=duration, max_candidates=ocfg["max_candidates"],
            observable_signals="\n".join(f"- {s}" for s in hero.observable_signals),
        )

        def run_overview():
            parsed, usage = self._call("overview", "overview", OverviewResponse, lambda schema, note: VideoRequest(
                video_path=upload_src, prompt=prompt + note, response_schema=schema, fps=ocfg["fps"],
                media_resolution=ocfg.get("media_resolution"), temperature=cfg["provider"].get("temperature"),
                context={"stage": "overview", "duration": duration},
            ))
            return parsed.model_dump(), usage

        overview = OverviewResponse.model_validate(
            self._cached_or_run(store, "overview", overview_fp, run_overview, "overview" in force))
        self.timings["overview"] = round(time.monotonic() - t, 3)
        if not overview.hero_confirmed:
            raise StageFailed("overview", f"영상의 영웅을 {hero.display_name_ko}(으)로 확인할 수 없습니다: {overview.hero_evidence}")
        if overview.hud_readable == "no":
            raise StageFailed("overview", f"HUD를 판독할 수 없습니다: {overview.video_quality_notes}")
        if overview.hud_readable == "partial":
            self.warnings.append(f"HUD 일부만 판독 가능: {overview.video_quality_notes}")
        if overview.team_identification.ally_side == "unknown":
            self.warnings.append(f"아군 팀을 판별하지 못함: {overview.team_identification.evidence}")
        team_context = team_context_text(overview)

        scenes = plan_scenes(overview, duration, cfg["detail"], ocfg["max_candidates"], self.discarded)
        self.log(f"  후보 장면 {len(scenes)}개: " + ", ".join(f"{s.window_start:.1f}-{s.window_end:.1f}s" for s in scenes))

        # 3. detail ---------------------------------------------------------------------
        self.log("[3/5] 후보 장면 상세 분석")
        t = time.monotonic()
        dcfg = cfg["detail"]
        dp = self.prompts["detail"]
        scene_records: dict[str, dict] = {}
        detail_fps = []
        abilities = "\n".join(f"- id={a.id}: {a.name_ko} ({a.name_en}) — {a.hud_hint or ''}" for a in hero.abilities)
        hcfg = cfg.get("hud_check") or {"enabled": False}
        for scene in scenes:
            fp = fingerprint(ai_fp_base, sha, scene.to_dict(), dcfg, pcfg, dp.version, team_context)
            detail_fps.append(fp)

            def run_detail(scene: Scene = scene):
                clip = store.dir / "media" / "clips" / f"{scene.scene_id}.mp4"
                cut_clip(video, clip, scene.window_start, scene.window_end, pcfg["max_height"], pcfg["crf"])
                prompt = dp.render(
                    clip_duration_seconds=scene.duration, hero_name_ko=hero.display_name_ko,
                    hero_name_en=hero.display_name_en, candidate_reasons=" / ".join(scene.reasons),
                    observable_signals="\n".join(f"- {s}" for s in hero.observable_signals), abilities=abilities,
                    team_context=team_context,
                )
                parsed, usage = self._call(f"detail:{scene.scene_id}", "detail", DetailResponse,
                                           lambda schema, note: VideoRequest(
                    video_path=clip, prompt=prompt + note, response_schema=schema, fps=dcfg["fps"],
                    media_resolution=dcfg.get("media_resolution"), temperature=cfg["provider"].get("temperature"),
                    context={"stage": "detail", "clip_duration": scene.duration},
                ))
                # raw (clip-relative) output is kept; conversion happens below so it is re-checked every run
                return parsed.model_dump(), usage

            raw = self._cached_or_run(store, f"detail-{scene.scene_id}", fp, run_detail, "detail" in force)
            scene_records[scene.scene_id] = detail_to_original(
                scene, DetailResponse.model_validate(raw), cfg["output"]["window_tolerance_seconds"], self.warnings)
        self.timings["detail"] = round(time.monotonic() - t, 3)

        # 3b. HUD check of claimed ability/ultimate use (no AI) ---------------------------
        t = time.monotonic()
        for rec in scene_records.values():
            self._hud_check_scene(video, rec, hcfg)
        n_checked = [e["hud_check"]["status"] for r in scene_records.values() for e in r["ability_events"]]
        if n_checked:
            self.log("  HUD 대조: " + ", ".join(f"{s} {n_checked.count(s)}" for s in sorted(set(n_checked))))
        self.timings["hud_check"] = round(time.monotonic() - t, 3)

        # 4. coaching -------------------------------------------------------------------
        self.log("[4/5] 코칭 기준 적용")
        t = time.monotonic()
        cp = self.prompts["coaching"]
        out = cfg["output"]
        coaching_fp = fingerprint(ai_fp_base, detail_fps, scene_records, cfg["coaching"], out["max_feedback_items"],
                                  out["max_action_items"], cp.version, hero.criteria_for_prompt())
        if scene_records:
            prompt = cp.render(
                hero_name_ko=hero.display_name_ko,
                criteria=json.dumps(hero.criteria_for_prompt(), ensure_ascii=False, indent=1),
                observations=json.dumps(list(scene_records.values()), ensure_ascii=False, indent=1),
                max_feedback_items=out["max_feedback_items"], max_action_items=out["max_action_items"],
            )

            def run_coaching():
                parsed, usage = self._call("coaching", "coaching", CoachingResponse, lambda schema, note: TextRequest(
                    prompt=prompt + note, response_schema=schema, temperature=cfg["coaching"].get("temperature"),
                    context={"stage": "coaching", "scenes": list(scene_records.values())},
                ))
                return parsed.model_dump(), usage

            coaching = CoachingResponse.model_validate(
                self._cached_or_run(store, "coaching", coaching_fp, run_coaching, "coaching" in force))
        else:
            self.warnings.append("근거가 충분한 후보 장면이 없어 코칭 단계를 건너뛰었습니다.")
            coaching = CoachingResponse(items=[], action_items=[])
        self.timings["coaching"] = round(time.monotonic() - t, 3)

        # 5. finalize -------------------------------------------------------------------
        self.log("[5/5] 검증·중복 제거·근거 프레임 추출")
        t = time.monotonic()
        evidence_dir = store.dir / "evidence"
        if evidence_dir.exists():
            shutil.rmtree(evidence_dir)
        max_w = cfg["preprocess"]["evidence_frame_max_width"]

        def extract(ts: float) -> str | None:
            rel = f"evidence/frame_{int(round(ts * 1000)):09d}ms.jpg"
            dst = store.dir / rel
            if dst.exists() or extract_frame(video, ts, dst, max_w):
                return rel
            self.warnings.append(f"근거 프레임 추출 실패: {ts}s")
            return None

        feedback, actions = finalize_feedback(coaching, scene_records, hero, duration, out, extract, self.discarded,
                                              hud_enabled=bool(hcfg.get("enabled")))
        self.timings["finalize"] = round(time.monotonic() - t, 3)

        result = AnalysisResult(
            run_id=run_id,
            analysis_mode="mock" if prov.is_mock else "real",
            notice=MOCK_NOTICE if prov.is_mock else None,
            hero=hero.hero_id, hero_display_name=hero.display_name_ko,
            game_patch_version=game_patch, criteria_patch_version=hero.patch_version,
            video=VideoInfo(file_name=video.name, sha256=sha, duration_seconds=duration, width=info.width,
                            height=info.height, fps=info.fps, codec=info.codec),
            overview=overview.model_dump(),
            feedback=feedback, action_items=actions, discarded=self.discarded,
            versions=Versions(
                pipeline=f"{cfg['pipeline_version']} (owcoach {__version__})",
                result_schema=AnalysisResult.model_fields["schema_version"].default,
                provider=prov.name, model=prov.model, sdk=prov.sdk_version,
                prompts={k: p.version for k, p in self.prompts.items()},
                hero_profile=hero.profile_version, hero_profile_review_status=hero.review_status,
                config_fingerprint=fingerprint(cfg),
            ),
            timings_seconds=self.timings, usage=self.usage,
            cost=compute_cost(self.usage, self.pricing, prov.is_mock),
            warnings=self.warnings, created_at=now_iso(),
        )
        errors = validate_result(result, store.dir)
        if errors:
            raise StageFailed("finalize", "결과 검증 실패: " + "; ".join(errors))
        return result

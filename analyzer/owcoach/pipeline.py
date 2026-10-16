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

import numpy as np
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import __version__
from .config import ANALYZER_ROOT, fingerprint
from .hud import (
    check_use_claim,
    detect_buff_target,
    detect_antiheal_targets,
    detect_heal_boost,
    camera_turn,
    screen_presence,
    team_health_timeline,
    detect_kill_credit,
    detect_reflect,
    player_team_color,
    detect_status_icon,
    min_run_for,
    sample_hud,
    scan_dead_periods,
    scan_uses,
    use_times,
)
from .timefmt import fmt_range, fmt_time
from .gamedata import load_gamedata
from .heroes import HeroProfile, load_comp_notes
from .portraits import roster_timeline, identify, load_portraits
from .media import (
    InputRejected,
    cut_clip,
    extract_crop_png,
    extract_frame,
    make_proxy,
    probe,
    sha256_file,
    validate_input,
)
from .postprocess import Scene, detail_to_original, finalize_feedback, plan_scenes, validate_result
from .prompts import load_prompt
from .providers import AnalysisProvider, ImageRequest, ProviderError, TextRequest, VideoRequest
from .schemas import (
    AnalysisResult,
    CoachingResponse,
    KillerResponse,
    KillFeedResponse,
    CostSummary,
    DetailResponse,
    MomentResponse,
    DiscardedItem,
    OverviewResponse,
    SceneCandidate,
    StageUsage,
    TeamResponse,
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


def _price(u: StageUsage, p: dict) -> float:
    out_tokens = u.output_tokens + (u.thinking_tokens or 0)
    return u.input_tokens / 1e6 * p["input_per_million_tokens"] + out_tokens / 1e6 * p["output_per_million_tokens"]


def compute_cost(usage: list[StageUsage], pricing: dict, is_mock: bool) -> CostSummary:
    """This run's cost (new API calls only) and, as full_run_total, what the whole analysis costs when no
    stage is reused from the cache (cached stages keep the token counts of the call that produced them)."""
    if is_mock:
        return CostSummary(note="모의 실행: API를 호출하지 않아 비용이 발생하지 않았습니다.")
    models = pricing.get("models") or {}
    for u in usage:
        if not models.get(u.model):
            return CostSummary(note=f"모델 '{u.model}'의 확인된 단가가 config/pricing.yaml에 없어 비용을 계산하지 않았습니다. 토큰 사용량만 기록합니다.")
        if u.input_tokens is None or u.output_tokens is None:
            return CostSummary(note="일부 호출의 토큰 사용량이 응답에 없어 비용을 계산하지 않았습니다.")
    total = sum(_price(u, models[u.model]) for u in usage if not u.from_cache)
    full = sum(_price(u, models[u.model]) for u in usage)
    sources = {f"{models[u.model].get('source_url')} (확인일 {models[u.model].get('verified_on')})" for u in usage}
    currency = next((models[u.model].get("currency", "USD") for u in usage), None)
    note = "추정치: 토큰 수 × pricing.yaml 단가. thinking 토큰은 출력 단가로 계산. 단가 출처: " + ", ".join(sorted(sources))
    if not any(not u.from_cache for u in usage):
        note = "이번 실행은 모든 단계를 캐시에서 재사용해 새 API 호출이 없었습니다. " + note
    return CostSummary(total=round(total, 6), full_run_total=round(full, 6) if usage else None,
                       currency=currency, note=note)


OTHER_COLOR = {"빨강": "파랑/청록", "파랑": "빨강/분홍"}


def team_color_text(color: dict | None) -> str:
    c = (color or {}).get("ally_color")
    if not c:
        return ""
    return (f"- 아군/적 색 (코드 측정, 이 표를 따르세요): 좌하단 내 초상화 테두리 색이 {c}입니다. "
            f"화면의 윤곽선·체력바·이름표가 {c}(분홍 포함) 계열이면 아군, {OTHER_COLOR[c]} 계열이면 적입니다. "
            "\"빨강=적\" 관례로 판단하지 마세요.")


def team_context_text(team: dict | None, color: dict | None = None, comp_notes: list[dict] | None = None) -> str:
    """Team / roster facts (team stage + portrait matching + code-measured ally color), handed to the
    overview, detail and moment prompts."""
    color_line = team_color_text(color)
    if team is None:
        return color_line or "- 팀 판별 안 함: 아군/적 색을 가정하지 말고 화면 상단 팀 목록과 플레이어 이름으로 직접 판단하세요."
    ti = team["team_identification"]
    side = {"left": "왼쪽", "right": "오른쪽", "unknown": "판별 못 함"}[ti["ally_side"]]
    ally, enemy = ti["ally_color"], ti["enemy_color"]
    if (color or {}).get("ally_color"):
        ally, enemy = color["ally_color"], OTHER_COLOR[color["ally_color"]]
    lines = [f"- 플레이어 이름: {ti['player_name'] or '판독 못 함'}",
             f"- 아군 팀: 상단 팀 목록 {side}, 아군 색 {ally}, 적 색 {enemy}",
             f"- 판단 근거: {ti['evidence']}"]
    if color_line:
        lines.insert(0, color_line)
    team_ko = {"ally": "아군", "enemy": "적", "unknown": "소속 불명"}
    for r in team["roster"]:
        hero = r.get("hero") or "불명"
        note = f"초상화 대조 {r['hero_confidence']}" if r.get("hero_confidence") else "영웅 판독 안 함"
        hist = r.get("hero_history") or []
        if len(hist) > 1:
            note += "; " + " → ".join(f"{h['hero']}({fmt_time(h['start_seconds'])}~)" for h in hist)
        lines.append(f"- {r['player_name']}: {team_ko[r['team']]}, 영웅 {hero} ({note})")
    for w in team.get("hero_swaps", []):
        side = team_ko[next((r["team"] for r in team["roster"] if r["slot"] == w["slot"]), "unknown")]
        lines.append(f"- 영웅 교체 (코드 확인): {fmt_time(w['time_seconds'])}경 {side} {w['player_name']} {w['from_hero']} → {w['to_hero']}. "
                     "조합이 바뀌면 그 뒤 장면은 바뀐 조합 기준으로 판단하세요.")
    lines += comp_note_lines(team, comp_notes or [])
    return "\n".join(lines)


def coaching_gaps(coaching: CoachingResponse, scene_records: dict) -> list[str]:
    """What the coaching prompt requires but the response lacks: a positioning item per scene and an item
    for every feedback_required skill / ultimate use (matched by its HUD time inside the item)."""
    gaps = []
    for sid, rec in scene_records.items():
        if not any(i.scene_id == sid and i.category == "positioning" for i in coaching.items):
            gaps.append(f"{sid} ({fmt_range(rec['window_start'], rec['window_end'])})의 positioning 피드백")
        for e in rec["ability_events"]:
            x = (e.get("hud_check") or {}).get("hud_use_time_seconds")
            if e.get("feedback_required") and x is not None and not any(
                    i.start_time_seconds - 1.5 <= x <= i.end_time_seconds + 1.5 and i.category != "positioning"
                    for i in coaching.items):
                gaps.append(f"{sid}의 {e['ability_id']} {fmt_time(x)} 사용 피드백")
    return gaps


def _comp_at(team: dict, side: str, t: float) -> list[str]:
    out = []
    for r in team["roster"]:
        if r["team"] != side:
            continue
        seg = next((h for h in r.get("hero_history") or [] if h["start_seconds"] <= t <= h["end_seconds"]), None)
        out.append(seg["hero"] if seg else (r.get("hero") or "불명"))
    return out


def comp_note_lines(team: dict, notes: list[dict]) -> list[str]:
    """Coach notes on team compositions (heroes/common.yaml comp_notes) that apply to this video: our team
    shares at least `min_match` heroes with the note's team (and the enemy with its versus, if given)."""
    times = sorted({0.0, *(w["time_seconds"] for w in team.get("hero_swaps", []))})
    lines = []
    for n in notes:
        k = n.get("min_match", 4)
        hit = [t for t in times if len(set(_comp_at(team, "ally", t)) & set(n["team"])) >= k
               and (not n.get("versus") or len(set(_comp_at(team, "enemy", t)) & set(n["versus"])) >= k)]
        if hit:
            lines.append(f"- 코치 조합 메모 ({fmt_time(hit[0])}부터 해당하는 조합): {n['note'].strip()}")
    return lines


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
        self.prompts = {k: load_prompt(k) for k in ("team", "overview", "detail", "moment", "coaching", "killer", "killfeed")}
        self.usage: list[StageUsage] = []
        self.timings: dict[str, float] = {}
        self.warnings: list[str] = []
        self.discarded: list[DiscardedItem] = []
        self._team_record: dict | None = None
        self._hud_ult_uses: list[float] = []
        self._deaths: list[dict] = []

    # ---------------------------------------------------------------- AI call helper

    def _call(self, stage: str, prompt_id: str, model_cls: type[BaseModel], make_request) -> tuple[BaseModel, list[dict]]:
        """Call provider, validate against the stage schema; re-ask on schema mismatch."""
        schema = inline_refs(model_cls.model_json_schema())
        repairs = int(self.cfg["provider"].get("json_repair_retries", 1))
        usage_records, note = [], ""
        for attempt in range(repairs + 1):
            req = make_request(schema, note)
            try:
                if isinstance(req, VideoRequest):
                    resp = self.provider.analyze_video(req)
                elif isinstance(req, ImageRequest):
                    resp = self.provider.analyze_images(req)
                else:
                    resp = self.provider.generate_json(req)
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

    def _find_deaths(self, video: Path, duration: float, overview: OverviewResponse, hcfg: dict) -> list[dict]:
        dcfg = self.cfg.get("death_check") or {}
        if not dcfg.get("enabled"):
            return []
        merge = float(dcfg["merge_seconds"])
        periods = scan_dead_periods(video, duration, dcfg) if dcfg.get("health_bar") else []
        deaths = [{"time_seconds": s, "source": "체력 0", "respawn_seconds": e} for s, e in periods]
        for ev in overview.timeline:
            t = ev.time_seconds
            if not (any(k in ev.event for k in dcfg.get("model_keywords", [])) and 0 <= t <= duration):
                continue
            # a model-recorded death inside a health-0 stretch (or just before it) is that same death;
            # models often log it late, while the player is still waiting to respawn
            same = next((d for d in deaths if d["time_seconds"] - merge <= t <= d.get("respawn_seconds", d["time_seconds"]) + merge), None)
            if same:
                if "모델 기록" not in same["source"]:
                    same["source"] += " + 모델 기록"
            else:
                deaths.append({"time_seconds": t, "source": "모델 기록"})
        return sorted(deaths, key=lambda d: d["time_seconds"])

    def _team_stage(self, store: RunStore, video: Path, sha: str, duration: float, ai_fp_base: dict,
                    force: set[str]) -> tuple[TeamResponse | None, dict | None]:
        tcfg = self.cfg.get("team") or {"enabled": False}
        if not tcfg.get("enabled"):
            return None, None
        tp = self.prompts["team"]
        times = [round(duration * f, 2) for f in tcfg["frame_fractions"]]
        fp = fingerprint(ai_fp_base, sha, tcfg, tp.version)
        labels = {"team_lists": "팀 목록(화면 상단)", "player_hud": "플레이어 HUD(화면 좌하단)", "kill_feed": "킬피드(화면 우상단)"}
        image_list = "\n".join(f"{i}. {labels.get(k, k)}" for i, k in enumerate(tcfg["regions"], 1))
        prompt = tp.render(frame_count=len(times), image_list=image_list)

        def run_team():
            images = [(extract_crop_png(video, ts, tuple(tcfg["reference_resolution"]), region, int(tcfg["upscale"])),
                       "image/png") for ts in times for region in tcfg["regions"].values()]
            parsed, usage = self._call("team", "team", TeamResponse, lambda schema, note: ImageRequest(
                images=images, prompt=prompt + note, response_schema=schema,
                media_resolution=tcfg.get("media_resolution"), temperature=self.cfg["provider"].get("temperature"),
                context={"stage": "team"},
            ))
            return parsed.model_dump(), usage

        team = TeamResponse.model_validate(self._cached_or_run(store, "team", fp, run_team, "team" in force))
        ti = team.team_identification
        if ti.ally_side == "unknown":
            self.warnings.append(f"아군 팀을 판별하지 못함: {ti.evidence}")
        if ti.player_name and ti.player_name not in {r.player_name for r in team.roster}:
            self.warnings.append(f"플레이어 이름 '{ti.player_name}'이(가) 팀 목록 판독 결과에 없음")
        record = {**team.model_dump(), "frame_times_seconds": times, "portraits": None}
        if tcfg.get("portraits"):
            refs = load_portraits(tcfg["portraits"])
            ptimes = [round(duration * f, 2) for f in tcfg["portrait_slots"]["frame_fractions"]]
            matches = {m["slot"]: m for m in identify(video, ptimes, tcfg, refs["heroes"])}
            for r in record["roster"]:
                m = matches.get(r["slot"])
                if m:
                    r.update(hero=m["hero"], hero_confidence=m["confidence"], hero_match=m)
            record["portraits"] = {"source": refs["source"], "sheet": refs["sheet"]}
            if "timeline" in tcfg["portrait_slots"]:
                tl = roster_timeline(video, duration, tcfg, refs["heroes"])
                names = {r["slot"]: r["player_name"] for r in record["roster"]}
                for r in record["roster"]:
                    segs = tl["segments"].get(r["slot"]) or []
                    if segs:
                        r["hero_history"] = segs
                        if r.get("hero") != segs[-1]["hero"]:
                            # the hero playing at the end of the video (a swapped slot, or a portrait the
                            # 6-frame match left unknown but consecutive reads agree on)
                            r.update(hero=segs[-1]["hero"], hero_confidence="timeline")
                record["hero_swaps"] = [{**w, "player_name": names.get(w["slot"], "")} for w in tl["swaps"]]
                if record["hero_swaps"]:
                    self.log("  영웅 교체: " + ", ".join(f"{fmt_time(w['time_seconds'])} {w['player_name']} {w['from_hero']}→{w['to_hero']}"
                                                       for w in record["hero_swaps"]))
            low = [f"{r['player_name']}" for r in record["roster"] if r.get("hero_confidence") == "low"]
            if low:
                self.warnings.append("초상화 대조로 영웅을 확정하지 못함(불명 처리): " + ", ".join(low))
        return team, record

    def _presence_track(self, video: Path, start: float, end: float) -> list[dict]:
        """Allies / enemies on screen (outline colours) every 1/fps s, each sample marked alive or not."""
        pcfg = (self.cfg.get("hud_check") or {}).get("screen_presence")
        if not pcfg:
            return []
        ally_red = ((getattr(self, "_team_color", None) or {}).get("ally_color") or "빨강") == "빨강"
        track = screen_presence(video, start, end, pcfg, ally_red=ally_red)
        dead = [(d["time_seconds"], d.get("respawn_seconds", d["time_seconds"] + 10)) for d in self._deaths]
        for x in track:
            x["alive"] = not any(a - 0.5 <= x["time_seconds"] <= b for a, b in dead)
        return track

    @staticmethod
    def _spans(track: list[dict], hit: np.ndarray, seconds: float, share: float, fps: float,
               ends_on_hit: bool = False) -> list[tuple[int, int]]:
        """(first, last) sample index of runs where a window of `seconds` has at least `share` of its samples
        hit, all alive (and, with ends_on_hit, starting and ending on a hit). One stray sample does not
        break a run."""
        alive = np.array([x["alive"] for x in track])
        k = max(2, int(round(seconds * fps)))
        flag = np.zeros(len(track), dtype=bool)
        for i in range(len(track) - k + 1):
            ends = not ends_on_hit or (hit[i] and hit[i + k - 1])
            if alive[i:i + k].all() and ends and hit[i:i + k].mean() >= share:
                flag[i:i + k] = True
        runs, cur = [], None
        for i, f in enumerate(flag):
            if f:
                cur = (cur[0], i) if cur else (i, i)
            elif cur:
                runs.append(cur)
                cur = None
        return runs + ([cur] if cur else [])

    def _alone_spans(self, track: list[dict]) -> list[dict]:
        """Spans where enemies are on screen but no ally is (outline colours), while the player is alive:
        a window of alone_seconds in which at least alone_share of the samples show that. One stray frame
        (an ally outline flickering into view) does not break a span."""
        pcfg = (self.cfg.get("hud_check") or {}).get("screen_presence")
        if not track:
            return []
        lone = np.array([x["allies"] == 0 and x["enemies"] >= 1 and x["alive"] for x in track])
        out = []
        for i, j in self._spans(track, lone, pcfg["alone_seconds"], pcfg["alone_share"], pcfg["fps"]):
            s, e = track[i]["time_seconds"], track[j]["time_seconds"]
            n = max(x["enemies"] for x in track[i:j + 1])
            out.append({"start_seconds": s, "end_seconds": e, "max_enemies": n,
                        "note": f"{fmt_range(s, e)} 화면에 아군 없이 적 최대 {n}명과 대치 (코드 측정: 윤곽선)"})
        return out

    def _turned_away_spans(self, video: Path, track: list[dict], start: float, end: float) -> list[dict]:
        """Spans where the player looks away from where the team was last seen (coach, video 1 3:07: move
        toward the payload while facing the team, not with the back to it).

        The team's direction is where at least team_min_allies ally outlines were on screen (in two samples
        in a row, so one stray red mark does not move it), from the camera turn (camera_turn) plus where on
        screen they were. A sample is away when, at most team_min_allies - 1 allies on screen, the view is
        turned_widths or more screen widths from that direction (1 width: even an ally at the screen edge is
        out of view), within max_gap_seconds of the last sighting (the turn drifts). Measured (1.0 width):
        video 1 1:58-2:00 (facing enemies, no ally in view), 3:06-3:09 (walking to the payload, back to the
        team); video 2 4:21-4:28 (facing enemies through the bus, team behind)."""
        pcfg = (self.cfg.get("hud_check") or {}).get("screen_presence") or {}
        tcfg = pcfg.get("turn")
        if not track or not tcfg:
            return []
        times, turn = camera_turn(video, start, end, tcfg)
        if not len(turn):
            return []
        x0, _, x1, _ = pcfg["world"]
        world_share = (x1 - x0) / pcfg["reference_resolution"][0]       # ally_x is across the world view
        team_n = int(tcfg["team_min_allies"])
        anchor, seen_at, run, away = None, None, 0, []
        for x in track:
            t = x["time_seconds"]
            view = float(turn[min(len(turn) - 1, int(np.searchsorted(times, t)))])
            run = run + 1 if x["allies"] >= team_n else 0
            if run >= 2:
                anchor = view + float(np.mean([(a - 0.5) * world_share for a in x["ally_x"]]))
                seen_at = t
            fresh = anchor is not None and t - seen_at <= tcfg["max_gap_seconds"]
            away.append(abs(view - anchor) if fresh else 0.0)
        away = np.array(away)
        hit = (away >= tcfg["turned_widths"]) & np.array([x["allies"] < team_n for x in track])
        out = []
        for i, j in self._spans(track, hit, tcfg["min_seconds"], tcfg["share"], pcfg["fps"], ends_on_hit=True):
            s, e = track[i]["time_seconds"], track[j]["time_seconds"]
            # enemies as the median: a cyan billboard can read as several enemies in one sample (video 1 3:09.5: 7)
            en = [x["enemies"] for x in track[i:j + 1]]
            w, med, n = float(away[i:j + 1].max()), int(np.median(en)), max(en)
            out.append({"start_seconds": s, "end_seconds": e, "turned_widths": round(w, 1),
                        "median_enemies": med, "max_enemies": n,
                        "note": f"{fmt_range(s, e)} 본대(아군 {team_n}명 이상)가 보이던 방향에서 시선이 화면 약 {w:.1f}폭 돌아가 "
                                f"본대가 화면 밖, 화면 속 적 대개 {med}명(최대 {n}명) (코드 측정: 화면 이동량·윤곽선)"})
        return out

    def _game_facts(self) -> str:
        """Game numbers for this match only (player hero in detail, every other hero seen in the team list,
        swaps included, briefly), from the bundled stats snapshot. Empty when disabled or missing."""
        gcfg = self.cfg.get("game_data") or {}
        if not gcfg.get("enabled"):
            return "- 게임 수치 자료를 쓰지 않음"
        try:
            gd = load_gamedata(gcfg.get("path"))
        except Exception as e:  # noqa: BLE001 - missing / unreadable bundle: run without numbers
            self.warnings.append(f"게임 수치 자료를 열지 못함: {e}")
            return "- 게임 수치 자료를 열지 못함"
        heroes: list[str] = []
        for r in (self._team_record or {}).get("roster", []):
            for h in [x["hero"] for x in r.get("hero_history") or []] + [r.get("hero")]:
                if h and h != "불명" and h not in heroes:
                    heroes.append(h)
        return gd.facts_text(self.hero.display_name_ko, heroes, brief_keys=tuple(gcfg.get("brief_stats", ["duration"])))

    def _game_data_meta(self) -> dict | None:
        gcfg = self.cfg.get("game_data") or {}
        if not gcfg.get("enabled"):
            return None
        try:
            return {**load_gamedata(gcfg.get("path")).meta,
                    "match_patch": "영상으로 확인 불가 — 수집본 수치는 확인이 필요한 전제"}
        except Exception:  # noqa: BLE001
            return None

    def _cooldown_check(self) -> None:
        """Two HUD-detected uses of one ability closer than its base cooldown point to a detection error
        (or a perk / effect the video shows). Warned, not dropped: perks are not known."""
        gcfg = self.cfg.get("game_data") or {}
        if not gcfg.get("enabled"):
            return
        try:
            gd = load_gamedata(gcfg.get("path"))
        except Exception:  # noqa: BLE001
            return
        hid = gd.hero_id(self.hero.display_name_ko)
        names = {a.id: a for a in self.hero.abilities}
        for aid, xs in getattr(self, "_hud_skill_uses", {}).items():
            a = names.get(aid)
            dbid = gd.ability_id(hid, a.name_en) if hid and a else None
            cd = ((gd.get_ability_stats(hid, dbid).get("cooldown") or {}).get("seconds")) if dbid else None
            if not cd:
                continue
            for x, y in zip(xs, xs[1:]):
                if y - x < cd - gcfg.get("cooldown_tolerance_seconds", 1.0):
                    self.warnings.append(f"{a.name_ko} HUD 사용 {fmt_time(x)}·{fmt_time(y)} 간격 {y - x:.1f}초 < 기본 쿨타임 {cd:g}초 "
                                         "(위키 수집본): 감지 오류이거나 특전·효과로 쿨타임이 줄었을 수 있음")

    def _ally_health(self, video: Path, t: float, cfg: dict) -> dict:
        """Allies' health (0..1 of their bar) just before t, and who was critically low."""
        team = self._team_record
        tcfg = self.cfg.get("team") or {}
        if not team or "portrait_slots" not in tcfg:
            return {"status": "unverifiable", "note": "팀 목록 없음"}
        if getattr(self, "_team_health", None) is None:
            ps = tcfg["portrait_slots"]
            self._team_health = team_health_timeline(video, self._duration, ps["left_x"] + ps["right_x"], cfg)
        times, fills = self._team_health
        sel = (times >= t - cfg["before_seconds"] - 0.5) & (times <= t)
        if not sel.any():
            return {"status": "unverifiable", "note": "판별용 프레임 부족"}
        allies = []
        for r in team["roster"]:
            if r["team"] != "ally":
                continue
            vals = fills[sel, r["slot"] - 1]
            vals = vals[vals >= 0]
            frac = float(np.median(vals)) if len(vals) else -1.0
            if frac < cfg["dead_below"]:
                continue  # dead or unreadable
            allies.append({"player_name": r["player_name"], "hero": self._who(r["player_name"], t)["hero"], "health": round(frac, 2)})
        critical = [x for x in allies if x["health"] < cfg["critical"]]
        note = ("치명상 아군 " + ", ".join(f"{x['player_name']}({x['health'] * 100:.0f}%)" for x in critical)) if critical \
            else "치명상 아군 없음 (" + ", ".join(f"{x['player_name']} {x['health'] * 100:.0f}%" for x in allies) + ")"
        return {"status": "ok", "allies": allies, "critical": critical, "note": note + " (팀 목록 체력바)"}

    def _who(self, name: str, t: float) -> dict:
        """A kill-feed name resolved against the team list: team (ally/enemy) and the hero played at t."""
        team = self._team_record or {}
        r = next((x for x in team.get("roster", []) if name and x["player_name"] == name), None)
        if not r:
            return {"player_name": name, "team": "unknown", "hero": None}
        seg = next((h for h in r.get("hero_history") or [] if h["start_seconds"] <= t <= h["end_seconds"]), None)
        return {"player_name": name, "team": r["team"], "hero": (seg or {}).get("hero") or r.get("hero")}

    def _kill_feed_scene(self, store: RunStore, video: Path, sha: str, scene, duration: float, ai_fp_base: dict,
                         force: set[str]) -> list[dict]:
        """Kill-feed rows seen in a scene: enlarged crops every step_seconds, the model transcribes the rows,
        code merges a row seen in several crops and resolves both names against the team list."""
        kcfg = self.cfg.get("kill_feed") or {}
        if not kcfg.get("enabled"):
            return []
        step = float(kcfg["step_seconds"])
        end = min(scene.window_end + kcfg["tail_seconds"], duration - 0.1)
        times = [round(x, 2) for x in np.arange(scene.window_start, end + 1e-6, step)]
        kp = self.prompts["killfeed"]
        fp = fingerprint(ai_fp_base, sha, scene.window_start, scene.window_end, kcfg, kp.version)
        prompt = kp.render(image_list="\n".join(f"{i}. {fmt_time(x)} ({x}초)" for i, x in enumerate(times, 1)))

        def run_feed():
            images = [(extract_crop_png(video, x, tuple(kcfg["reference_resolution"]), kcfg["region"], int(kcfg["upscale"])), "image/png")
                      for x in times]
            parsed, usage = self._call(f"killfeed@{scene.scene_id}", "killfeed", KillFeedResponse, lambda schema, note: ImageRequest(
                images=images, prompt=prompt + note, response_schema=schema,
                media_resolution=kcfg.get("media_resolution"), temperature=self.cfg["provider"].get("temperature"),
                context={"stage": "killfeed", "times": times},
            ))
            return parsed.model_dump(), usage

        raw = KillFeedResponse.model_validate(self._cached_or_run(store, f"killfeed-{scene.scene_id}", fp, run_feed, "killfeed" in force))
        out: list[dict] = []
        for e in sorted(raw.entries, key=lambda e: e.time_seconds):
            if not (times[0] - 0.5 <= e.time_seconds <= times[-1] + 0.5):
                continue
            k, v = e.killer_name.strip(), e.victim_name.strip()
            if any(o["killer"]["player_name"] == k and o["victim"]["player_name"] == v
                   and e.time_seconds - o["time_seconds"] <= kcfg["merge_seconds"] for o in out):
                continue  # the same row read again in a later crop
            out.append({"time_seconds": round(e.time_seconds, 2), "killer": self._who(k, e.time_seconds),
                        "victim": self._who(v, e.time_seconds)})
        return out

    def _killer(self, store: RunStore, video: Path, sha: str, t: float, ai_fp_base: dict, force: set[str]) -> dict:
        """Who killed the player at t: the model reads the name from enlarged crops of the kill feed and the
        "killed by" notice; code accepts it only if it is an enemy in the team list (hero at that time)."""
        kcfg = self.cfg.get("killer_check") or {}
        team = self._team_record
        if not kcfg.get("enabled") or not team:
            return {"status": "not_checked", "note": "처치자 확인 안 함"}
        me = team["team_identification"]["player_name"] or "플레이어"
        times = [round(t + dt, 2) for dt in kcfg["offsets_seconds"]]
        kp = self.prompts["killer"]
        fp = fingerprint(ai_fp_base, sha, t, kcfg, kp.version, me)
        prompt = kp.render(player_name=me, frame_count=len(times))

        def run_killer():
            images = [(extract_crop_png(video, ts, tuple(kcfg["reference_resolution"]), region, int(kcfg["upscale"])), "image/png")
                      for ts in times for region in kcfg["regions"].values()]
            parsed, usage = self._call(f"killer@{fmt_time(t)}", "killer", KillerResponse, lambda schema, note: ImageRequest(
                images=images, prompt=prompt + note, response_schema=schema,
                media_resolution=kcfg.get("media_resolution"), temperature=self.cfg["provider"].get("temperature"),
                context={"stage": "killer"},
            ))
            return parsed.model_dump(), usage

        read = KillerResponse.model_validate(self._cached_or_run(store, f"killer-{int(round(t * 100))}", fp, run_killer, "killer" in force))
        name = read.killer_name.strip()
        enemy = next((r for r in team["roster"] if r["team"] == "enemy" and name and r["player_name"] == name), None)
        if not enemy:
            return {"status": "unverifiable", "read": name,
                    "note": f"킬피드 판독 '{name}'이(가) 적 팀 목록에 없음" if name else "킬피드에서 처치자를 읽지 못함"}
        seg = next((h for h in enemy.get("hero_history") or [] if h["start_seconds"] <= t <= h["end_seconds"]), None)
        hero = (seg or {}).get("hero") or enemy.get("hero")
        return {"status": "verified", "player_name": name, "hero": hero,
                "note": f"킬피드: {name}({hero or '?'}) > {me} (처치한 적은 이 한 명, 도움은 킬피드에 나오지 않음)"}

    def _hud_check_scene(self, video: Path, rec: dict, hcfg: dict) -> None:
        slots = {a.id: a.hud_slot for a in self.hero.abilities}
        markers = {a.id: a.target_marker for a in self.hero.abilities}
        by_id_all = {a.id: a for a in self.hero.abilities}
        events = rec["ability_events"]
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
                marker = markers.get(e["ability_id"])
                if marker and e["hud_check"]["status"] == "verified" and marker in hcfg:
                    tc = self._target_check(video, e["hud_check"]["hud_use_time_seconds"], hcfg[marker])
                    if tc["status"] in ("verified", "interrupted") and e.get("target") and e["target"] != tc["player_name"]:
                        tc["note"] += f"; 모델이 기록한 대상({e['target']})과 다름"
                    e["hud_check"]["target_check"] = tc
        # a disabling skill (sleep dart) followed at once by the player's death: a "hit" claim is doubtful
        window = float(self.cfg["output"].get("disable_then_death_seconds", 0) or 0)
        for e in events:
            a, hc = by_id_all.get(e["ability_id"]), e.get("hud_check") or {}
            x = hc.get("hud_use_time_seconds")
            if a and a.disables_target and hc.get("status") == "verified" and x is not None and window:
                died = [d["time_seconds"] for d in self._deaths if 0 <= d["time_seconds"] - x <= window]
                if died:
                    hc["death_after_seconds"] = round(died[0] - x, 2)

        # skill / ultimate uses seen on the HUD in this window but not (correctly) recorded by the model
        by_id = {a.id: a for a in self.hero.abilities}
        claimed = {(e["ability_id"], e["hud_check"].get("hud_use_time_seconds")) for e in events
                   if e["hud_check"]["status"] == "verified"}
        for u in rec.get("hud_uses", []):
            a, x = by_id.get(u["ability_id"]), u["time_seconds"]
            if a is None or (a.id, x) in claimed:
                continue
            hc = {"status": "verified", "hud_slot": a.hud_slot, "hud_use_time_seconds": x,
                  "note": f"HUD: {fmt_time(x)}에 준비 → 사용됨 전환 확인 (모델 기록 없음, 코드가 추가)"}
            if a.target_marker and a.target_marker in hcfg:
                hc["target_check"] = self._target_check(video, x, hcfg[a.target_marker])
            events.append({"time_seconds": x, "ability_id": a.id, "target": "", "source": "hud",
                           "evidence": "HUD에서 감지", "hud_check": hc})
        # did the skill come back at the player (deflected)? Not judged when an enemy plays the same hero,
        # whose own skill would leave the same mark
        enemy_heroes = {r.get("hero") for r in (self._team_record or {}).get("roster", []) if r.get("team") == "enemy"}
        for e in events:
            a, hc = by_id.get(e["ability_id"]), e["hud_check"]
            if not (a and a.reflect_marker and hc.get("status") == "verified" and a.reflect_marker in hcfg):
                continue
            if self.hero.display_name_ko in enemy_heroes:
                hc["reflect_check"] = {"status": "unverifiable",
                                       "note": f"적 팀에 {self.hero.display_name_ko}가 있어 반사 여부 판별 불가"}
            else:
                hc["reflect_check"] = detect_reflect(video, hc["hud_use_time_seconds"], hcfg[a.reflect_marker])
        # who got the skill's effect (grenade: which enemies are anti-healed), from the team list
        for e in events:
            a, hc = by_id.get(e["ability_id"]), e["hud_check"]
            if a and a.effect_marker and hc.get("status") == "verified" and a.effect_marker in hcfg:
                every = [{**x, "side": side} for side in ("ally", "enemy") for x in self._ally_slots(side=side)]
                hc["antiheal_check"] = detect_antiheal_targets(video, hc["hud_use_time_seconds"], every, hcfg[a.effect_marker])
        # how healthy the allies were when it was thrown (team-list health bars)
        th = hcfg.get("team_health")
        for e in events:
            a, hc = by_id.get(e["ability_id"]), e["hud_check"]
            if a and a.ally_health and th and hc.get("status") == "verified":
                hc["ally_health"] = self._ally_health(video, hc["hud_use_time_seconds"], th)
        # did it reach allies / the player (grenade heal: yellow-outlined overhead bars, own HUD icon)?
        for e in events:
            a, hc = by_id.get(e["ability_id"]), e["hud_check"]
            if a and a.ally_marker and hc.get("status") == "verified" and a.ally_marker in hcfg:
                hc["heal_check"] = detect_heal_boost(video, hc["hud_use_time_seconds"], hcfg[a.ally_marker])
        # did the player take part in a kill right after (e.g. anti-heal grenade -> enemy tank down)?
        for e in events:
            a, hc = by_id.get(e["ability_id"]), e["hud_check"]
            if a and a.kill_marker and hc.get("status") == "verified" and a.kill_marker in hcfg:
                kcfg = hcfg[a.kill_marker]
                hc["kill_check"] = detect_kill_credit(video, hc["hud_use_time_seconds"], self._ally_slots(side=kcfg["side"]), kcfg)
        # every verified use of a force_scene skill (and every ultimate) must get its own feedback item
        for e in events:
            a = by_id.get(e["ability_id"])
            if a and (a.force_scene or a.hud_slot == "ultimate") and e["hud_check"].get("status") == "verified":
                e["feedback_required"] = True

    def _moment_checks(self, store: RunStore, video: Path, sha: str, duration: float, scene_records: dict,
                       ai_fp_base: dict, team_context: str, pcfg: dict, force: set[str]) -> None:
        mcfg = self.cfg.get("moment_check") or {}
        if not mcfg.get("enabled"):
            return
        hero, mp = self.hero, self.prompts["moment"]
        by_id = {a.id: a for a in hero.abilities}
        counters = "\n".join(f"   - {c}" for c in hero.skill_counters) or "   (확인할 대응 스킬 없음)"
        done: dict[tuple, dict] = {}
        for rec in scene_records.values():
            for e in rec["ability_events"]:
                hc = e.get("hud_check") or {}
                a = by_id.get(e["ability_id"])
                if hc.get("status") != "verified" or a is None or (mcfg.get("skip_ultimate") and a.hud_slot == "ultimate"):
                    continue
                x = hc["hud_use_time_seconds"]
                key = (a.id, x)
                if key not in done:
                    start = max(0.0, x - float(mcfg["pre_seconds"]))
                    end = min(duration, x + float(mcfg["post_seconds"]))
                    fp = fingerprint(ai_fp_base, sha, a.id, x, mcfg, mp.version, team_context, hero.skill_counters)
                    name = f"moment-{a.id}-{int(round(x * 100))}"

                    def run_moment(a=a, x=x, start=start, end=end, name=name):
                        clip = store.dir / "media" / "clips" / f"{name}.mp4"
                        cut_clip(video, clip, start, end, pcfg["max_height"], pcfg["crf"])
                        prompt = mp.render(clip_duration_seconds=round(end - start, 2), use_offset_seconds=round(x - start, 2),
                                           hero_name_ko=hero.display_name_ko, ability_name=a.name_ko,
                                           team_context=team_context, counters=counters)
                        parsed, usage = self._call(f"moment:{a.id}@{fmt_time(x)}", "moment", MomentResponse,
                                                   lambda schema, note: VideoRequest(
                            video_path=clip, prompt=prompt + note, response_schema=schema, fps=float(mcfg["fps"]),
                            temperature=self.cfg["provider"].get("temperature"),
                            context={"stage": "moment", "clip_duration": round(end - start, 2)},
                        ))
                        return parsed.model_dump(), usage

                    done[key] = self._cached_or_run(store, name, fp, run_moment, "moment" in force)
                hc["moment_check"] = done[key]
        if done:
            n_yes = sum(m["counter_active"] == "yes" for m in done.values())
            self.log(f"  스킬 사용 순간 확인 {len(done)}건 (적 대응 스킬 활성: {n_yes}건)")

    def _hud_uses_in(self, video: Path, start: float, end: float, hcfg: dict) -> list[dict]:
        """HUD-detected uses inside [start, end]: ability tiles from this window's samples, the ultimate
        from the whole-video scan (its 2 s ready-state rule needs more context than a window edge)."""
        if not hcfg.get("enabled"):
            return []
        uses: list[dict] = []
        tiles = [a for a in self.hero.abilities if a.hud_slot and a.hud_slot != "ultimate" and a.hud_slot in hcfg["regions"]]
        if tiles:
            samples = sample_hud(video, max(0.0, start - 3.0), end, hcfg)
            for a in tiles:
                uses += [{"ability_id": a.id, "time_seconds": x}
                         for x in use_times(samples, a.hud_slot, min_run_for(hcfg, a.hud_slot)) if start <= x <= end]
        ult = next((a for a in self.hero.abilities if a.hud_slot == "ultimate"), None)
        if ult:
            uses += [{"ability_id": ult.id, "time_seconds": x} for x in self._hud_ult_uses if start <= x <= end]
        return sorted(uses, key=lambda u: u["time_seconds"])

    @staticmethod
    def _skill_uses(scene_records: dict) -> list[dict]:
        seen, out = set(), []
        for rec in scene_records.values():
            for e in rec["ability_events"]:
                hc = e.get("hud_check") or {}
                x = hc.get("hud_use_time_seconds")
                if hc.get("status") != "verified" or (e["ability_id"], x) in seen:
                    continue
                seen.add((e["ability_id"], x))
                out.append({"time_seconds": x, "ability_id": e["ability_id"], "source": e.get("source", "model"),
                            "target_check": hc.get("target_check"), "moment_check": hc.get("moment_check"),
                            "death_after_seconds": hc.get("death_after_seconds"), "reflect_check": hc.get("reflect_check"),
                            "kill_check": hc.get("kill_check"), "antiheal_check": hc.get("antiheal_check"),
                            "heal_check": hc.get("heal_check"), "ally_health": hc.get("ally_health")})
        return sorted(out, key=lambda u: u["time_seconds"])

    def _target_check(self, video: Path, use_time: float, mcfg: dict) -> dict:
        """Code-side target check from the team list: buff arrow on an ally, status icon on an enemy."""
        if mcfg.get("side") == "enemy":
            return detect_status_icon(video, use_time, self._ally_slots(side="enemy"), mcfg)
        return detect_buff_target(video, use_time, self._ally_slots(), mcfg)

    def _ally_slots(self, side: str = "ally") -> list[dict]:
        """Team-list slots of the player's teammates (player excluded) with portrait x positions."""
        team = self._team_record
        tcfg = self.cfg.get("team") or {}
        if not team or "portrait_slots" not in tcfg:
            return []
        ps = tcfg["portrait_slots"]
        xs = ps["left_x"] + ps["right_x"]
        me = team["team_identification"]["player_name"]
        return [{"slot": r["slot"], "x": xs[r["slot"] - 1], "player_name": r["player_name"], "hero": r.get("hero")}
                for r in team["roster"] if r["team"] == side and r["player_name"] != me and 1 <= r["slot"] <= len(xs)]

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
        # cache keys hold what each stage actually sends: the hero profile version is not part of the
        # base, so editing coaching criteria re-runs coaching only (detail keys include the abilities and
        # observable signals, coaching keys the criteria)
        ai_fp_base = {"provider": prov.name, "model": prov.model, "hero": hero.hero_id}

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
        self._duration, self._team_health = duration, None
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

        # 2a. team identification (cropped, upscaled HUD stills) ------------------------
        self.log("[2/5] 팀 판별 · 전체 흐름 분석")
        t = time.monotonic()
        team, team_record = self._team_stage(store, video, sha, duration, ai_fp_base, force)
        self._team_record = team_record
        tcfg = cfg.get("team") or {}
        self._team_color = player_team_color(video, duration, tcfg["player_portrait"]) if "player_portrait" in tcfg else None
        if self._team_color:
            self.log(f"  아군 색(내 초상화 테두리): {self._team_color.get('ally_color') or '판별 못 함'} — {self._team_color['note']}")
        team_context = team_context_text(team_record, self._team_color, load_comp_notes())
        self.timings["team"] = round(time.monotonic() - t, 3)

        # 2b. overview ------------------------------------------------------------------
        t = time.monotonic()
        ocfg = cfg["overview"]
        op = self.prompts["overview"]
        overview_fp = fingerprint(ai_fp_base, proxy_fp, ocfg, op.version, hero.observable_signals, team_context)
        prompt = op.render(
            hero_name_ko=hero.display_name_ko, hero_name_en=hero.display_name_en,
            duration_seconds=duration, max_candidates=ocfg["max_candidates"],
            observable_signals="\n".join(f"- {s}" for s in hero.observable_signals),
            team_context=team_context,
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

        # every ultimate use the HUD shows gets a scene, whether or not the overview model picked it
        hcfg = cfg.get("hud_check") or {"enabled": False}
        self._hud_ult_uses = []
        if hcfg.get("enabled") and any(a.hud_slot == "ultimate" for a in hero.abilities) and "ultimate" in hcfg["regions"]:
            self._hud_ult_uses = scan_uses(video, duration, "ultimate", hcfg)
            if self._hud_ult_uses:
                self.log("  HUD 궁극기 사용 감지: " + ", ".join(fmt_time(x) for x in self._hud_ult_uses))
        hud_candidates = [SceneCandidate(
            start_time_seconds=max(0.0, round(x - 2.0, 2)), end_time_seconds=min(duration, round(x + 2.0, 2)),
            category="ultimate_usage", reason_observed=f"HUD: {fmt_time(x)}에 궁극기 사용 감지", priority=5)
            for x in self._hud_ult_uses]
        # per-hero: abilities marked force_scene get a scene at every HUD-detected use
        self._hud_skill_uses: dict[str, list[float]] = {}
        if hcfg.get("enabled"):
            for a in hero.abilities:
                if a.force_scene and a.hud_slot and a.hud_slot != "ultimate" and a.hud_slot in hcfg["regions"]:
                    self._hud_skill_uses[a.id] = scan_uses(video, duration, a.hud_slot, hcfg)
                    if self._hud_skill_uses[a.id]:
                        self.log(f"  HUD {a.name_ko} 사용 감지: " + ", ".join(fmt_time(x) for x in self._hud_skill_uses[a.id]))
        names_ko = {a.id: a.name_ko for a in hero.abilities}
        skill_candidates = [SceneCandidate(
            start_time_seconds=max(0.0, round(x - 1.0, 2)), end_time_seconds=min(duration, round(x + 1.0, 2)),
            category="skill_usage", reason_observed=f"HUD: {fmt_time(x)}에 {names_ko[aid]} 사용 감지", priority=4)
            for aid, xs in self._hud_skill_uses.items() for x in xs]
        self._cooldown_check()
        self._deaths = self._find_deaths(video, duration, overview, hcfg)
        if self._deaths:
            self.log("  사망 감지: " + ", ".join(f"{fmt_time(d['time_seconds'])}({d['source']})" for d in self._deaths))
        for d in self._deaths:
            d["killer"] = self._killer(store, video, sha, d["time_seconds"], ai_fp_base, force)
        if any(d["killer"].get("status") == "verified" for d in self._deaths):
            self.log("  처치자(킬피드): " + ", ".join(f"{fmt_time(d['time_seconds'])} {d['killer']['player_name']}({d['killer'].get('hero') or '?'})"
                                                  for d in self._deaths if d["killer"].get("status") == "verified"))
        dpre = float((cfg.get("death_check") or {}).get("pre_seconds", 10.0))
        death_candidates = [SceneCandidate(
            start_time_seconds=max(0.0, round(d["time_seconds"] - dpre, 2)), end_time_seconds=d["time_seconds"],
            category="positioning", priority=5,
            reason_observed=f"사망 {fmt_time(d['time_seconds'])}: 직전 {dpre:g}초 상황 분석 필수 ({d['source']})")
            for d in self._deaths]
        scenes = plan_scenes(overview, duration, cfg["detail"], ocfg["max_candidates"], self.discarded,
                             forced=[*hud_candidates, *skill_candidates, *death_candidates])
        self.log(f"  후보 장면 {len(scenes)}개: " + ", ".join(f"{s.window_start:.1f}-{s.window_end:.1f}s" for s in scenes))

        # 3. detail ---------------------------------------------------------------------
        self.log("[3/5] 후보 장면 상세 분석")
        t = time.monotonic()
        dcfg = cfg["detail"]
        dp = self.prompts["detail"]
        scene_records: dict[str, dict] = {}
        detail_fps = []
        abilities = "\n".join(f"- id={a.id}: {a.name_ko} ({a.name_en}) — {a.hud_hint or ''}" for a in hero.abilities)
        names = {a.id: a.name_ko for a in hero.abilities}
        for scene in scenes:
            # skill / ultimate uses the HUD shows in this window, handed to the model as facts so it
            # describes the situation at those moments instead of guessing when skills were used
            hud_uses = self._hud_uses_in(video, scene.window_start, scene.window_end, hcfg)
            hud_events = "\n".join(f"- 구간 {u['time_seconds'] - scene.window_start:.1f}초: {names.get(u['ability_id'], u['ability_id'])}"
                                   f" (id={u['ability_id']})" for u in hud_uses) or "- 없음"
            fp = fingerprint(ai_fp_base, sha, scene.to_dict(), dcfg, pcfg, dp.version, team_context,
                             abilities, hero.observable_signals, hero.display_name_ko, hud_uses)
            detail_fps.append(fp)

            def run_detail(scene: Scene = scene):
                clip = store.dir / "media" / "clips" / f"{scene.scene_id}.mp4"
                cut_clip(video, clip, scene.window_start, scene.window_end, pcfg["max_height"], pcfg["crf"])
                prompt = dp.render(
                    clip_duration_seconds=scene.duration, hero_name_ko=hero.display_name_ko,
                    hero_name_en=hero.display_name_en, candidate_reasons=" / ".join(scene.reasons),
                    observable_signals="\n".join(f"- {s}" for s in hero.observable_signals), abilities=abilities,
                    team_context=team_context, hud_events=hud_events,
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
            scene_records[scene.scene_id]["hud_uses"] = hud_uses
            track = self._presence_track(video, scene.window_start, scene.window_end)
            scene_records[scene.scene_id]["alone_spans"] = self._alone_spans(track)
            scene_records[scene.scene_id]["turned_away_spans"] = self._turned_away_spans(
                video, track, scene.window_start, scene.window_end)
            scene_records[scene.scene_id]["kill_feed"] = self._kill_feed_scene(store, video, sha, scene, duration, ai_fp_base, force)
            scene_records[scene.scene_id]["player_deaths"] = [
                d for d in self._deaths if scene.window_start <= d["time_seconds"] <= scene.window_end + 0.5]
        self.timings["detail"] = round(time.monotonic() - t, 3)

        # 3b. HUD check of claimed ability/ultimate use (no AI) ---------------------------
        t = time.monotonic()
        for rec in scene_records.values():
            self._hud_check_scene(video, rec, hcfg)
        n_checked = [e["hud_check"]["status"] for r in scene_records.values() for e in r["ability_events"]]
        if n_checked:
            self.log("  HUD 대조: " + ", ".join(f"{s} {n_checked.count(s)}" for s in sorted(set(n_checked))))
        self.timings["hud_check"] = round(time.monotonic() - t, 3)

        # 3c. moment check: short high-fps look at each HUD-verified skill use (AI) -------
        t = time.monotonic()
        self._moment_checks(store, video, sha, duration, scene_records, ai_fp_base, team_context, pcfg, force)
        self.timings["moment"] = round(time.monotonic() - t, 3)

        # 4. coaching -------------------------------------------------------------------
        self.log("[4/5] 코칭 기준 적용")
        t = time.monotonic()
        cp = self.prompts["coaching"]
        out = cfg["output"]
        game_facts = self._game_facts()
        coaching_fp = fingerprint(ai_fp_base, detail_fps, scene_records, team_context, game_facts, cfg["coaching"], out["max_feedback_items"],
                                  out["max_action_items"], cp.version, hero.criteria_for_prompt())
        if scene_records:
            prompt = cp.render(
                hero_name_ko=hero.display_name_ko,
                criteria=json.dumps(hero.criteria_for_prompt(), ensure_ascii=False, indent=1),
                observations=json.dumps(list(scene_records.values()), ensure_ascii=False, indent=1),
                team_context=team_context, game_facts=game_facts,
                max_feedback_items=out["max_feedback_items"], max_action_items=out["max_action_items"],
            )

            def run_coaching():
                usage_all, extra = [], ""
                for attempt in range(1 + int(cfg["coaching"].get("coverage_retries", 1))):
                    parsed, usage = self._call("coaching", "coaching", CoachingResponse, lambda schema, note: TextRequest(
                        prompt=prompt + extra + note, response_schema=schema, temperature=cfg["coaching"].get("temperature"),
                        context={"stage": "coaching", "scenes": list(scene_records.values())},
                    ))
                    usage_all += usage
                    missing = coaching_gaps(parsed, scene_records)
                    if not missing:
                        break
                    # the model sometimes leaves out whole parts of what the prompt requires (seen: every
                    # positioning item dropped in one run): ask once more, naming what is missing
                    self.log(f"  coaching: 빠진 피드백 {len(missing)}건, 재요청 ({attempt + 1})")
                    extra = ("\n\n이전 응답에 다음 피드백이 빠졌습니다. 빠진 것을 포함해 전체 응답을 다시 작성하세요:\n"
                             + "\n".join(f"- {m}" for m in missing))
                else:
                    self.warnings.append("코칭 응답에 빠진 피드백: " + "; ".join(missing))
                return parsed.model_dump(), usage_all

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
                                              hud_enabled=bool(hcfg.get("enabled")),
                                              death_times=[d["time_seconds"] for d in self._deaths])
        dpre = float((cfg.get("death_check") or {}).get("pre_seconds", 10.0))
        for d in self._deaths:
            t = d["time_seconds"]
            # the feedback has to run up to the death itself, not merely overlap the 10 s before it
            if not any(f.start_time_seconds <= t + 1 and f.end_time_seconds >= t - 3 for f in feedback):
                self.warnings.append(f"사망 {fmt_time(t)} 직전 {dpre:g}초에 대한 피드백이 없음")
        # kill feed of each item's time span (all scenes: an item can sit at a scene's edge)
        feed = sorted({(e["time_seconds"], e["killer"]["player_name"], e["victim"]["player_name"]): e
                       for rec in scene_records.values() for e in rec.get("kill_feed", [])}.values(), key=lambda e: e["time_seconds"])
        pad = float((cfg.get("kill_feed") or {}).get("item_pad_seconds", 3.0))
        for f in feedback:
            f.kill_feed = [e for e in feed if f.start_time_seconds - pad <= e["time_seconds"] <= f.end_time_seconds + pad]
        ability_ko = {a.id: a.name_ko for a in hero.abilities}
        covered = {(c.ability_id, c.hud_use_time_seconds) for f in feedback for c in f.hud_checks if c.status == "verified"}
        for rec in scene_records.values():
            for e in rec["ability_events"]:
                if e.get("feedback_required") and (e["ability_id"], e["hud_check"]["hud_use_time_seconds"]) not in covered:
                    x = e["hud_check"]["hud_use_time_seconds"]
                    covered.add((e["ability_id"], x))
                    self.warnings.append(f"{ability_ko.get(e['ability_id'], e['ability_id'])} {fmt_time(x)} 사용에 대한 피드백이 없음")
        self.timings["finalize"] = round(time.monotonic() - t, 3)

        result = AnalysisResult(
            run_id=run_id,
            analysis_mode="mock" if prov.is_mock else "real",
            notice=MOCK_NOTICE if prov.is_mock else None,
            hero=hero.hero_id, hero_display_name=hero.display_name_ko,
            game_patch_version=game_patch, criteria_patch_version=hero.patch_version, team=team_record,
            hud_ultimate_uses_seconds=self._hud_ult_uses, player_deaths=self._deaths,
            skill_uses=self._skill_uses(scene_records),
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
                game_data=self._game_data_meta(),
            ),
            timings_seconds=self.timings, usage=self.usage,
            cost=compute_cost(self.usage, self.pricing, prov.is_mock),
            warnings=self.warnings, created_at=now_iso(),
        )
        errors = validate_result(result, store.dir)
        if errors:
            raise StageFailed("finalize", "결과 검증 실패: " + "; ".join(errors))
        return result

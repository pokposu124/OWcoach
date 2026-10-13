"""Analysis result viewer (HTML): uploaded video on the left (3/5), numbered feedback on the right (2/5).

Clicking a feedback number seeks the video to that feedback's start time. The video is
media/proxy.mp4 (same timeline as the original). Each card keeps observation / assessment /
suggestion separate, shows the grade, category, HUD checks and evidence frames, and has a
collapsible coach-review form whose answers are downloaded as coach_review.json.

fragment=True writes the page body only (no <html>/<head>/<body>), for hosts that wrap the page
themselves; the download button is left out there because such hosts may block downloads.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .schemas import AnalysisResult
from .timefmt import fmt_time

CATEGORY_KO = {"skill_usage": "스킬 사용", "positioning": "포지셔닝", "ultimate_usage": "궁극기 사용"}
GRADE_KO = {"excellent": "탁월", "best": "최선", "blunder": "블런더", "withheld": "판단 보류"}
HUD_KO = {"verified": "HUD 확인", "contradicted": "HUD 불일치", "unverifiable": "확인 불가", "not_checked": "대조 안 함"}
ABILITY_KO = {"sleep_dart": "수면총", "biotic_grenade": "생체 수류탄", "nano_boost": "나노 강화제"}

_t = fmt_time


def _e(x) -> str:
    return html.escape(str(x))


STYLE = """
/* layout: two panes, video 3/5 (sticky) | numbered feedback 2/5; stacks under 900px */
:root{--bg:#f3f4f7;--panel:#ffffff;--fg:#151a22;--muted:#5f6a78;--line:#dde1e8;--accent:#2457c5;
--excellent:#0e7c86;--excellent-bg:#dff3f4;--best:#257a3e;--best-bg:#e3f2e7;--blunder:#b4232a;--blunder-bg:#fbe5e6;
--hold:#6b6f78;--hold-bg:#eceef2;--warn:#8a5a00;--warn-bg:#fbefd6;
--sans:"IBM Plex Sans KR",system-ui,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1218;--panel:#171c24;--fg:#e5e9f0;--muted:#97a2b0;--line:#2a313c;--accent:#7ea6f2;
--excellent:#5fd0d8;--excellent-bg:#123336;--best:#6fd08c;--best-bg:#16301f;--blunder:#f4878b;--blunder-bg:#3a1a1c;
--hold:#a3a9b3;--hold-bg:#242a33;--warn:#e9b860;--warn-bg:#352a12;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1218;--panel:#171c24;--fg:#e5e9f0;--muted:#97a2b0;--line:#2a313c;--accent:#7ea6f2;
--excellent:#5fd0d8;--excellent-bg:#123336;--best:#6fd08c;--best-bg:#16301f;--blunder:#f4878b;--blunder-bg:#3a1a1c;
--hold:#a3a9b3;--hold-bg:#242a33;--warn:#e9b860;--warn-bg:#352a12;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.6 var(--sans);margin:0}
.app{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:20px;padding-inline:16px;padding-block:16px;max-width:1600px;margin:0 auto}
.stage{position:sticky;top:env(safe-area-inset-top,0px);align-self:start;display:grid;gap:10px;min-width:0}
video{width:100%;aspect-ratio:16/9;background:#000;border-radius:10px;display:block}
.title{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 12px}
.title h1{font-size:1.25rem;margin:0;text-wrap:balance}
.muted{color:var(--muted)} small.muted{font-size:.8rem}
.notice{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;margin:0;font-size:.88rem}
.notice.mock{background:var(--blunder-bg);color:var(--blunder);font-weight:700}
.teams{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.85rem}
.teams ul{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.teams li{display:flex;justify-content:space-between;gap:8px;padding:2px 8px;border-left:3px solid var(--line);min-width:0}
.teams li span{color:var(--muted);overflow-wrap:anywhere;text-align:right}
.teams .ally li{border-left-color:var(--blunder)} .teams .enemy li{border-left-color:var(--accent)}
.teams h2{font-size:.75rem;letter-spacing:.06em;color:var(--muted);margin:0 0 4px}
.feed{display:grid;gap:12px;align-content:start;min-width:0}
.summary{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0;color:var(--muted);font-size:.88rem}
.summary b{color:var(--fg)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:8px;min-width:0;scroll-margin-top:12px}
.card.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.card header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}
.num{width:2.1em;height:2.1em;border-radius:50%;border:none;background:var(--accent);color:var(--panel);font:700 .95rem var(--mono);cursor:pointer;flex:0 0 auto}
.num:focus-visible,.thumb:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.time{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.85rem;color:var(--accent)}
.chip{font-size:.75rem;font-weight:700;padding:1px 8px;border-radius:999px;white-space:nowrap}
.cat{background:var(--hold-bg);color:var(--fg)}
.g-excellent{background:var(--excellent-bg);color:var(--excellent)} .g-best{background:var(--best-bg);color:var(--best)}
.g-blunder{background:var(--blunder-bg);color:var(--blunder)} .g-withheld{background:var(--hold-bg);color:var(--hold)}
.mode{margin-left:auto} .m-assertive{background:var(--best-bg);color:var(--best)} .m-tentative{background:var(--warn-bg);color:var(--warn)}
.reasons{margin:0;font-size:.8rem;color:var(--warn)}
dl{display:grid;grid-template-columns:3em minmax(0,1fr);gap:3px 10px;margin:0} dt{color:var(--muted);font-size:.82rem;padding-top:1px} dd{margin:0}
ul.hud{list-style:none;margin:0;padding:0;display:grid;gap:3px;font-size:.84rem}
.h-verified{color:var(--best)} .h-contradicted{color:var(--blunder)} .h-unverifiable,.h-not_checked{color:var(--hold)}
.frames{display:flex;gap:6px;overflow-x:auto}
.thumb{border:none;padding:0;background:none;cursor:pointer;flex:0 0 auto;text-align:left;color:var(--muted);font:.72rem var(--mono)}
.thumb img{width:132px;max-width:100%;border-radius:5px;display:block}
details{font-size:.85rem} summary{cursor:pointer;color:var(--muted)}
fieldset{border:1px dashed var(--line);border-radius:8px;display:flex;flex-wrap:wrap;gap:6px 14px;margin:6px 0 0}
textarea{width:100%;min-height:44px;font:inherit}
.meta{display:grid;gap:6px}
table{border-collapse:collapse;font-size:.8rem} td,th{border:1px solid var(--line);padding:2px 6px;text-align:left}
.scroll{overflow-x:auto}
@media (max-width:900px){.app{grid-template-columns:minmax(0,1fr)} .stage{z-index:2;background:var(--bg);padding-bottom:6px}
  .stage .teams,.stage .notice{display:none}}
@media (max-width:480px){.teams{grid-template-columns:1fr} dl{grid-template-columns:minmax(0,1fr)}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

SCRIPT = """
const v=document.getElementById('v');
const cards=[...document.querySelectorAll('.card')];
function seek(t){v.currentTime=t;v.play().catch(()=>{});}
document.querySelectorAll('[data-seek]').forEach(el=>el.addEventListener('click',()=>seek(parseFloat(el.dataset.seek))));
v.addEventListener('timeupdate',()=>{const t=v.currentTime;
  cards.forEach(c=>c.classList.toggle('active',t>=parseFloat(c.dataset.start)-0.5&&t<=parseFloat(c.dataset.end)+0.5));});
"""

SAVE_SCRIPT = """
document.getElementById('save').addEventListener('click',()=>{
  const items=[...document.querySelectorAll('fieldset.review')].map(fs=>({feedback_id:fs.dataset.id,
    fact:fs.querySelector('[name=fact]').value,assessment:fs.querySelector('[name=assessment]').value,
    grade:fs.querySelector('[name=grade]').value,timing:fs.querySelector('[name=timing]').value,
    criterion_change:fs.querySelector('[name=criterion_change]').checked,comment:fs.querySelector('[name=comment]').value}));
  const out=Object.assign(JSON.parse(document.getElementById('review-meta').textContent),
    {reviewed_at:new Date().toISOString(),items,missed_scenes:document.getElementById('missed').value});
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));
  a.download='coach_review.json';a.click();
});
"""


def _hud_line(c) -> str:
    ability = ABILITY_KO.get(c.ability_id, c.ability_id)
    when = _t(c.hud_use_time_seconds) if c.status == "verified" and c.hud_use_time_seconds is not None else _t(c.time_seconds)
    target = ""
    if c.target_check:
        tc = c.target_check
        if tc.get("status") == "verified":
            target = f" → 대상 {_e(tc['player_name'])}({_e(tc.get('hero') or '?')})"
        elif tc.get("status") == "interrupted":
            target = f" → 대상 {_e(tc['player_name'])}({_e(tc.get('hero') or '?')}) 쓰러져 끊김(추정)"
        elif tc.get("status") == "missed":
            target = " → 빗나감(코드 확인)"
        else:
            target = " · 대상 확인 불가"
    if (c.reflect_check or {}).get("status") == "reflected":
        target += " → 반사되어 자신이 치유 차단(코드 확인)"
    return (f"<li class='h-{c.status}'>{_e(HUD_KO[c.status])} · {_e(ability)} {when}{target}"
            + (f" <span class=muted>— {_e(c.note)}</span>" if c.status != "verified" else "") + "</li>")


def _card(n: int, f) -> str:
    frames = "".join(
        f'<button class=thumb data-seek="{fr.timestamp_seconds}" type=button><img src="{_e(fr.path)}" loading="lazy" '
        f'alt="{_t(fr.timestamp_seconds)} 화면">{_t(fr.timestamp_seconds)}</button>' for fr in f.evidence_frames)
    if f.category == "positioning":
        hud = "<li class=muted>모델 관찰 · 위치·엄폐는 코드로 확인하지 않음</li>"
    else:
        hud = "".join(_hud_line(c) for c in f.hud_checks) or "<li class=muted>이 피드백의 스킬 사용 기록 없음</li>"
    mode = ('<span class="chip mode m-assertive">단정</span>' if f.display_mode == "assertive"
            else '<span class="chip mode m-tentative">참고·보류</span>')
    reasons = f'<p class=reasons>{_e(" · ".join(f.display_reasons))}</p>' if f.display_reasons else ""
    grade_opts = "".join(f"<option value={k}>{v}</option>" for k, v in GRADE_KO.items())
    return f"""
<article class=card id="fb-{n}" data-start="{f.start_time_seconds}" data-end="{f.end_time_seconds}">
  <header><button class=num type=button data-seek="{f.start_time_seconds}" aria-label="{n}번 피드백 {_t(f.start_time_seconds)}로 이동">{n}</button>
    <span class=time>{_t(f.start_time_seconds)} ~ {_t(f.end_time_seconds)}</span>
    <span class="chip cat">{_e(CATEGORY_KO[f.category])}</span>
    <span class="chip g-{f.grade}">{_e(GRADE_KO[f.grade])}</span>{mode}</header>
  {reasons}
  <dl><dt>관찰</dt><dd>{_e(f.observation)}</dd><dt>판단</dt><dd>{_e(f.assessment)}</dd><dt>제안</dt><dd>{_e(f.suggestion)}</dd>
  {f'<dt>불확실</dt><dd>{_e(f.uncertainty)}</dd>' if f.uncertainty else ''}</dl>
  <ul class=hud>{hud}</ul>
  {f'<div class=frames>{frames}</div>' if frames else ''}
  <details><summary>코치 검수 · 적용 기준 {_e(", ".join(f.criterion_ids) or "없음")} ({_e(f.criteria_review_status)})</summary>
  <fieldset class=review data-id="{_e(f.id)}">
    <label>관찰 사실 <select name=fact><option value="">-</option><option>정확</option><option>일부 틀림</option><option>틀림</option><option>확인 불가</option></select></label>
    <label>판단 <select name=assessment><option value="">-</option><option>동의</option><option>부분 동의</option><option>반대</option></select></label>
    <label>맞는 등급 <select name=grade><option value="">-</option>{grade_opts}</select></label>
    <label>시간 <select name=timing><option value="">-</option><option>정확</option><option>3초 이내</option><option>틀림</option></select></label>
    <label>기준 수정 필요 <input type=checkbox name=criterion_change></label>
    <textarea name=comment placeholder="코멘트 (오인식, 놓친 맥락, 기준 수정안 등)"></textarea>
  </fieldset></details>
</article>"""


def build_report_html(result: AnalysisResult, video_src: str, fragment: bool = False) -> str:
    feedback = sorted(result.feedback, key=lambda f: f.start_time_seconds)
    cards = "".join(_card(i, f) for i, f in enumerate(feedback, 1)) or "<p class=muted>근거가 충분한 피드백이 없습니다.</p>"
    counts = {k: sum(f.category == k for f in feedback) for k in CATEGORY_KO}
    grades = {k: sum(f.grade == k for f in feedback) for k in GRADE_KO}
    summary = ("".join(f"<span>{v} <b>{counts[k]}</b></span>" for k, v in CATEGORY_KO.items())
               + "".join(f"<span>{v} <b>{grades[k]}</b></span>" for k, v in GRADE_KO.items() if grades[k])
               + f"<span>사망 <b>{len(result.player_deaths)}</b></span>")
    num_of = {f.id: i for i, f in enumerate(feedback, 1)}
    actions = "".join(
        f"<li>{_e(a.task)} <span class=muted>— {_e(a.why)} ({', '.join(f'{num_of[r]}번' for r in a.related_feedback_ids if r in num_of)})</span></li>"
        for a in result.action_items) or "<li class=muted>없음 (단정 피드백이 있어야 과제가 만들어집니다)</li>"

    team = result.team or {}
    me = (team.get("team_identification") or {}).get("player_name")
    def side(name):
        return "".join(f"<li><b>{_e(r.get('hero') or '불명')}{' (나)' if r['player_name'] == me else ''}</b><span>{_e(r['player_name'])}</span></li>"
                       for r in team.get("roster", []) if r["team"] == name)
    teams = (f"<div class=teams><section class=ally><h2>아군</h2><ul>{side('ally')}</ul></section>"
             f"<section class=enemy><h2>적</h2><ul>{side('enemy')}</ul></section></div>") if team else ""

    notice = (f'<p class="notice mock">{_e(result.notice)}</p>' if result.analysis_mode == "mock" else "")
    if any(f.criteria_review_status != "approved" for f in feedback):
        notice += '<p class=notice>코칭 기준이 코치 검수 전 초안입니다. 등급과 판단은 참고용입니다.</p>'

    discarded = "".join(f"<li><b>{_e(d.reason)}</b> {_e(d.summary)}</li>" for d in result.discarded) or "<li>없음</li>"
    warnings = "".join(f"<li>{_e(w)}</li>" for w in result.warnings) or "<li>없음</li>"
    usage_rows = "".join(
        f"<tr><td>{_e(u.stage)}</td><td>{_e(u.model)}</td><td>{u.input_tokens}</td><td>{u.output_tokens}</td>"
        f"<td>{u.thinking_tokens}</td><td>{u.attempts}</td><td>{'예' if u.from_cache else ''}</td></tr>" for u in result.usage)
    cost = f"{result.cost.total} {result.cost.currency or ''}" if result.cost.total is not None else "미계산"
    review_meta = json.dumps({"run_id": result.run_id, "analysis_mode": result.analysis_mode, "model": result.versions.model,
                              "prompts": result.versions.prompts, "hero_profile": result.versions.hero_profile},
                             ensure_ascii=False).replace("</", "<\\/")
    save = "" if fragment else '<p><button id=save type=button>검수 결과 저장 (coach_review.json)</button></p>'

    body = f"""
<div class=app>
<section class=stage>
  <video id=v src="{_e(video_src)}" controls preload=metadata playsinline></video>
  <div class=title><h1>{_e(result.hero_display_name)} 플레이 분석</h1>
    <small class=muted>{_e(result.video.file_name)} · {_t(result.video.duration_seconds)} · {_e('실제 AI 분석' if result.analysis_mode == 'real' else '모의 실행')} · {_e(result.versions.model)}</small></div>
  {notice}{teams}
</section>
<section class=feed aria-label="피드백">
  <p class=summary>{summary}</p>
  {cards}
  <details><summary>다음 경기 과제</summary><ul>{actions}</ul></details>
  <details class=meta><summary>검수 · 기록</summary>
    <textarea id=missed placeholder="AI가 놓친 중요한 장면: 시간 + 설명"></textarea>{save}
    <details><summary>제외된 항목 ({len(result.discarded)})</summary><ul>{discarded}</ul></details>
    <details><summary>경고</summary><ul>{warnings}</ul></details>
    <details><summary>사용량 · 비용</summary><p>비용: {_e(cost)} — {_e(result.cost.note)}</p>
      <div class=scroll><table><tr><th>단계</th><th>모델</th><th>입력</th><th>출력</th><th>thinking</th><th>시도</th><th>캐시</th></tr>{usage_rows}</table></div></details>
    <p class=muted>run {_e(result.run_id)} · 프롬프트 {_e(', '.join(f'{k} {v}' for k, v in result.versions.prompts.items()))} · 영웅 기준 {_e(result.versions.hero_profile)} · 게임 패치 {_e(result.game_patch_version or '미입력')}</p>
  </details>
</section>
</div>
<script type="application/json" id=review-meta>{review_meta}</script>
<script>{SCRIPT}{'' if fragment else SAVE_SCRIPT}</script>"""
    head = (f'<title>{_e(result.hero_display_name)} 플레이 분석</title>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;700&family=IBM+Plex+Mono:wght@500;700&display=swap">'
            f"<style>{STYLE}</style>")
    if fragment:
        return head + body
    return f"<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">{head}</head><body>{body}</body></html>"


def write_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    result = AnalysisResult.model_validate_json((run_dir / "result.json").read_text("utf-8"))
    meta = json.loads((run_dir / "run.json").read_text("utf-8"))
    video_src = "media/proxy.mp4" if (run_dir / "media" / "proxy.mp4").exists() else os.path.relpath(meta["video"], run_dir)
    path = run_dir / "report.html"
    path.write_text(build_report_html(result, video_src), "utf-8")
    return path


def export_site(run_dir: Path, out_dir: Path, video: Path, video_height: int = 480, video_kbps: int = 360) -> Path:
    """Static, self-contained copy of the result view for web hosting (e.g. GitHub Pages).

    Writes out_dir/index.html, out_dir/evidence/*.jpg and out_dir/media/web.mp4, a smaller two-pass
    re-encode of the source video (same timeline) so the page stays light enough to host.
    """
    import shutil
    import subprocess

    from .media import _require

    run_dir, out_dir = Path(run_dir), Path(out_dir)
    result = AnalysisResult.model_validate_json((run_dir / "result.json").read_text("utf-8"))
    (out_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (out_dir / "media").mkdir(parents=True, exist_ok=True)
    for old in (out_dir / "evidence").glob("*.jpg"):
        old.unlink()
    for f in result.feedback:
        for fr in f.evidence_frames:
            shutil.copy2(run_dir / fr.path, out_dir / fr.path)
    web = out_dir / "media" / "web.mp4"
    common = ["-i", str(video), "-vf", f"scale=-2:{video_height}", "-c:v", "libx264", "-preset", "medium",
              "-b:v", f"{video_kbps}k", "-an", "-passlogfile", str(out_dir / "media" / "pass")]
    subprocess.run([_require("ffmpeg"), "-v", "error", "-y", *common, "-pass", "1", "-f", "mp4", "/dev/null"], check=True)
    subprocess.run([_require("ffmpeg"), "-v", "error", "-y", *common, "-pass", "2", "-movflags", "+faststart", str(web)], check=True)
    for log in (out_dir / "media").glob("pass*"):
        log.unlink()
    (out_dir / "index.html").write_text(build_report_html(result, "media/web.mp4"), "utf-8")
    return out_dir / "index.html"

"""Static HTML review report for coaches (step 2: 결과 검수). Not the product web UI.

The report plays media/proxy.mp4 (same timeline as the original), lists feedback with
observation / assessment / suggestion kept separate, shows evidence frames, and lets the
reviewer fill a per-item verdict that is downloaded as coach_review.json.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .schemas import AnalysisResult

CATEGORY_KO = {"positioning": "포지셔닝", "survival": "생존", "skill_usage": "스킬 사용"}
HUD_KO = {"verified": "HUD 확인", "contradicted": "HUD 불일치", "unverifiable": "확인 불가", "not_checked": "대조 안 함"}
HUD_CLASS = {"verified": "ok", "contradicted": "bad", "unverifiable": "warn", "not_checked": "warn"}
JUDGEMENT_KO = {"good": "잘한 플레이", "needs_improvement": "개선 필요", "withheld": "판단 보류"}


def _t(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:04.1f}"


def _e(x) -> str:
    return html.escape(str(x))


def write_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    result = AnalysisResult.model_validate_json((run_dir / "result.json").read_text("utf-8"))
    meta = json.loads((run_dir / "run.json").read_text("utf-8"))
    video_src = "media/proxy.mp4" if (run_dir / "media" / "proxy.mp4").exists() else os.path.relpath(meta["video"], run_dir)

    cards = []
    for f in result.feedback:
        frames = "".join(
            f'<figure><img src="{_e(fr.path)}" loading="lazy" data-t="{fr.timestamp_seconds}">'
            f'<figcaption>{_t(fr.timestamp_seconds)}</figcaption></figure>' for fr in f.evidence_frames
        ) or "<p class=muted>근거 프레임 없음</p>"
        badge = ('<span class="b ok">단정</span>' if f.display_mode == "assertive"
                 else f'<span class="b warn">참고·보류</span> <span class=muted>{_e(", ".join(f.display_reasons))}</span>')
        crit = ", ".join(f.criterion_ids) or "없음"
        hud = "".join(
            f"<li><span class='b {HUD_CLASS[c.status]}'>{_e(HUD_KO[c.status])}</span> {_t(c.time_seconds)} {_e(c.ability_id)}"
            f" — {_e(c.note)} <span class=muted>(모델 근거: {_e(c.claimed_by_model or '-')})</span></li>"
            for c in f.hud_checks) or "<li class=muted>이 구간에 스킬/궁극기 사용 기록 없음</li>"
        cards.append(f"""
<section class=card id="{_e(f.id)}">
  <header><button class=seek data-t="{f.start_time_seconds}">▶ {_t(f.start_time_seconds)} – {_t(f.end_time_seconds)}</button>
  <b>{_e(CATEGORY_KO[f.category])}</b> · {_e(JUDGEMENT_KO[f.judgement])} · 확신도 {f.confidence:.2f} {badge}</header>
  <dl>
    <dt>관찰 사실</dt><dd>{_e(f.observation)}</dd>
    <dt>판단</dt><dd>{_e(f.assessment)}</dd>
    <dt>제안</dt><dd>{_e(f.suggestion)}</dd>
    <dt>불확실성</dt><dd>[{_e(f.uncertainty_level)}] {_e(f.uncertainty or '-')}</dd>
    <dt>HUD 대조</dt><dd><ul class=hud>{hud}</ul></dd>
    <dt>적용 기준</dt><dd>{_e(crit)} <span class=muted>(기준 상태: {_e(f.criteria_review_status)}, 패치: {_e(f.patch_version)})</span></dd>
  </dl>
  <div class=frames>{frames}</div>
  <fieldset class=review data-id="{_e(f.id)}">
    <legend>코치 검수</legend>
    <label>관찰 사실 <select name=fact><option value="">-</option><option>정확</option><option>일부 틀림</option><option>틀림</option><option>확인 불가</option></select></label>
    <label>판단 <select name=assessment><option value="">-</option><option>동의</option><option>부분 동의</option><option>반대</option></select></label>
    <label>시간 정확도 <select name=timing><option value="">-</option><option>정확</option><option>±3초 이내</option><option>틀림</option></select></label>
    <label>기준 수정 필요 <input type=checkbox name=criterion_change></label>
    <textarea name=comment placeholder="코멘트 (오인식, 놓친 맥락, 기준 수정안 등)"></textarea>
  </fieldset>
</section>""")

    actions = "".join(f"<li>{_e(a.task)} <span class=muted>— {_e(a.why)} ({_e(', '.join(a.related_feedback_ids))})</span></li>"
                      for a in result.action_items) or "<li class=muted>없음</li>"
    discarded = "".join(f"<li><b>{_e(d.reason)}</b> {_e(d.scene_id or '')} {_e(d.summary)}</li>" for d in result.discarded) or "<li>없음</li>"
    warnings = "".join(f"<li>{_e(w)}</li>" for w in result.warnings) or "<li>없음</li>"
    usage_rows = "".join(
        f"<tr><td>{_e(u.stage)}</td><td>{_e(u.model)}</td><td>{_e(u.prompt_version)}</td><td>{u.input_tokens}</td>"
        f"<td>{u.output_tokens}</td><td>{u.thinking_tokens}</td><td>{u.latency_seconds}</td><td>{u.attempts}</td><td>{'예' if u.from_cache else ''}</td></tr>"
        for u in result.usage)
    mock_banner = f'<div class="banner">{_e(result.notice)}</div>' if result.analysis_mode == "mock" else ""
    ov = result.team or {}
    ti = ov.get("team_identification") or {}
    def _match(r):
        m = r.get("hero_match")
        return f"{m['score']:.2f} (2위 {_e(m['runner_up'])} 차이 {m['margin']:.2f})" if m else "-"
    roster = "".join(f"<tr><td>{r.get('slot', '')}</td><td>{_e(r['player_name'])}</td><td>{_e(r['team'])}</td><td>{_e(r.get('hero') or '불명')}</td>"
                     f"<td>{_e(r.get('hero_confidence') or '-')}</td><td>{_match(r)}</td></tr>"
                     for r in ov.get("roster") or [])
    team = (f"<p>플레이어 {_e(ti.get('player_name') or '-')} · 아군 팀 목록 {_e(ti.get('ally_side', '-'))} · 아군 색 {_e(ti.get('ally_color', '-'))}"
            f" · 적 색 {_e(ti.get('enemy_color', '-'))}<br><span class=muted>근거: {_e(ti.get('evidence', '-'))}</span></p>"
            f"<table><tr><th>칸</th><th>플레이어</th><th>팀</th><th>영웅(초상화 대조)</th><th>확신도</th><th>대조 점수</th></tr>{roster}</table>") if ti else "<p class=muted>없음</p>"
    timings = ", ".join(f"{k} {v}s" for k, v in result.timings_seconds.items())
    cost = f"{result.cost.total} {result.cost.currency or ''}" if result.cost.total is not None else "미계산"

    doc = f"""<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>OWcoach 검수 리포트 · {_e(result.run_id)}</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--card:#f5f5f7;--ok:#1b7f3b;--warn:#a15c00;--bd:#d9d9de}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#ececf0;--muted:#9a9aa2;--card:#202024;--ok:#4cc276;--warn:#e6a23c;--bd:#34343a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
.banner{{background:#b00020;color:#fff;padding:10px 14px;border-radius:8px;font-weight:600;margin-bottom:12px}}
video{{width:100%;max-height:60vh;background:#000;border-radius:8px;position:sticky;top:0;z-index:1}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:12px 0}}
dl{{display:grid;grid-template-columns:7em 1fr;gap:4px 10px;margin:8px 0}} dt{{color:var(--muted)}} dd{{margin:0}}
.frames{{display:flex;gap:8px;overflow-x:auto}} figure{{margin:0}} img{{height:120px;border-radius:6px;cursor:pointer}}
figcaption,.muted{{color:var(--muted);font-size:13px}} .b{{font-size:12px;padding:1px 6px;border-radius:6px;color:#fff}}
.ok{{background:var(--ok)}} .warn{{background:var(--warn)}} .bad{{background:#b00020}} ul.hud{{margin:0;padding-left:1.1em}} button{{cursor:pointer}}
fieldset{{border:1px dashed var(--bd);border-radius:8px;margin-top:10px;display:flex;flex-wrap:wrap;gap:8px 16px}}
textarea{{width:100%;min-height:48px}} table{{border-collapse:collapse;font-size:13px}} td,th{{border:1px solid var(--bd);padding:2px 6px}}
@media (max-width:600px){{dl{{grid-template-columns:1fr}}}}
</style></head><body>
{mock_banner}
<h1>{_e(result.hero_display_name)} 분석 검수 · <small>{_e(result.video.file_name)}</small></h1>
<p class=muted>run {_e(result.run_id)} · 모드 {_e(result.analysis_mode)} · 모델 {_e(result.versions.model)} · 프롬프트 {_e(', '.join(f'{k} {v}' for k, v in result.versions.prompts.items()))} ·
영웅 기준 {_e(result.versions.hero_profile)} ({_e(result.versions.hero_profile_review_status)}) · 기준 패치 {_e(result.criteria_patch_version)} · 게임 패치 {_e(result.game_patch_version or '미입력')}</p>
<video id=v src="{_e(video_src)}" controls preload=metadata></video>
<details open><summary>팀 판별 (이름·팀: 모델 판독, 영웅: 초상화 이미지 대조 — 검수 필요)</summary>{team}</details>
<h2>피드백 ({len(result.feedback)})</h2>
{''.join(cards) or '<p class=muted>근거가 충분한 피드백이 없습니다.</p>'}
<h2>다음 경기 과제</h2><ul>{actions}</ul>
<h2>놓친 장면 (코치 입력)</h2><textarea id=missed placeholder="AI가 놓친 중요한 장면: 시간 + 설명"></textarea>
<p><button id=save>검수 결과 저장 (coach_review.json)</button></p>
<details><summary>제외된 항목 ({len(result.discarded)})</summary><ul>{discarded}</ul></details>
<details><summary>경고</summary><ul>{warnings}</ul></details>
<details><summary>전체 흐름 분석 (overview)</summary><pre style="white-space:pre-wrap">{_e(json.dumps(result.overview, ensure_ascii=False, indent=1))}</pre></details>
<details><summary>사용량 · 처리 시간 · 비용</summary><p>처리 시간: {_e(timings)}</p><p>비용: {_e(cost)} — {_e(result.cost.note)}</p>
<table><tr><th>단계</th><th>모델</th><th>프롬프트</th><th>입력</th><th>출력</th><th>thinking</th><th>지연(s)</th><th>시도</th><th>캐시</th></tr>{usage_rows}</table></details>
<script>
const v=document.getElementById('v');
document.querySelectorAll('[data-t]').forEach(el=>el.addEventListener('click',()=>{{v.currentTime=parseFloat(el.dataset.t);v.play();}}));
document.getElementById('save').addEventListener('click',()=>{{
  const items=[...document.querySelectorAll('fieldset.review')].map(fs=>({{feedback_id:fs.dataset.id,
    fact:fs.querySelector('[name=fact]').value,assessment:fs.querySelector('[name=assessment]').value,
    timing:fs.querySelector('[name=timing]').value,criterion_change:fs.querySelector('[name=criterion_change]').checked,
    comment:fs.querySelector('[name=comment]').value}}));
  const out={{run_id:{json.dumps(result.run_id)},analysis_mode:{json.dumps(result.analysis_mode)},
    model:{json.dumps(result.versions.model)},prompts:{json.dumps(result.versions.prompts)},
    hero_profile:{json.dumps(result.versions.hero_profile)},reviewed_at:new Date().toISOString(),
    items,missed_scenes:document.getElementById('missed').value}};
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{{type:'application/json'}}));
  a.download='coach_review.json';a.click();
}});
</script></body></html>"""
    path = run_dir / "report.html"
    path.write_text(doc, "utf-8")
    return path

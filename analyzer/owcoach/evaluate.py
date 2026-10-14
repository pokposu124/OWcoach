"""Compare an analysis result with user-confirmed facts (eval/*.yaml).

Checks what code and model produced against what the player confirmed: deaths, ultimate uses and
their targets, team-list heroes, events the model is known to have invented, and the player's own
grades for specific plays. Prints a short report and returns it as a dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_yaml
from .schemas import AnalysisResult
from .timefmt import fmt_time


def _near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _moment(run_dir: Path, fb_checks: list, ability: str, t: float) -> dict | None:
    """Moment check of the use of `ability` near t: from a feedback item, else from the stored stage."""
    mc = next((c.moment_check for c in fb_checks if c.ability_id == ability and c.hud_use_time_seconds is not None
               and _near(c.hud_use_time_seconds, t, 1.5) and c.moment_check), None)
    if mc is None:  # the use may not be in any feedback; its moment check is still stored as a stage
        for path in sorted((Path(run_dir) / "stages").glob(f"moment-{ability}-*.json")):
            if _near(int(path.stem.rsplit("-", 1)[1]) / 100, t, 1.5):
                return json.loads(path.read_text("utf-8"))["output"]
    return mc


def evaluate(run_dir: Path, labels_path: Path) -> dict:
    result = AnalysisResult.model_validate_json((Path(run_dir) / "result.json").read_text("utf-8"))
    labels = load_yaml(Path(labels_path))
    if not result.video.sha256.startswith(labels["video_sha12"]):
        raise ValueError(f"영상이 다릅니다: 결과 {result.video.sha256[:12]} / 정답 기록 {labels['video_sha12']}")
    facts = labels.get("facts", {})
    out: dict = {"run_id": result.run_id, "labels": str(labels_path), "checks": []}

    def check(kind: str, ok: bool | None, detail: str) -> None:
        out["checks"].append({"kind": kind, "ok": ok, "detail": detail})

    # deaths (code: health 0)
    found = [d["time_seconds"] for d in result.player_deaths]
    for d in facts.get("deaths", []):
        check("사망 감지", any(_near(t, d["time_seconds"], 2.0) for t in found), f"{fmt_time(d['time_seconds'])}")
    for t in found:
        if not any(_near(t, d["time_seconds"], 2.0) for d in facts.get("deaths", [])):
            check("사망 오탐", False, f"{fmt_time(t)} (정답에 없음)")

    # ultimates and their targets (code: HUD + team-list arrow)
    fb_checks = [c for f in result.feedback for c in f.hud_checks]
    for u in facts.get("ultimates", []):
        t = u["time_seconds"]
        check("궁극기 감지", any(_near(x, t, 1.5) for x in result.hud_ultimate_uses_seconds), fmt_time(t))
        tc = next((c.target_check for c in fb_checks if c.hud_use_time_seconds is not None
                   and _near(c.hud_use_time_seconds, t, 1.5) and c.target_check), None)
        want = u.get("target_player")
        if want:
            got = (tc or {}).get("player_name")
            status = (tc or {}).get("status")
            ok = got == want and (status == "interrupted") == bool(u.get("interrupted"))
            check("궁극기 대상", ok, f"{fmt_time(t)} 정답 {want}{' (끊김)' if u.get('interrupted') else ''} / 결과 {got or '없음'} ({status or '-'})")

    # roster heroes (code: portrait matching)
    roster = {r["slot"]: r for r in (result.team or {}).get("roster", [])}
    for r in facts.get("roster", []):
        got = (roster.get(r["slot"]) or {}).get("hero")
        check("영웅 판독", got == r["hero"], f"{r['slot']}번 칸 정답 {r['hero']} / 결과 {got or '없음'}")

    # hero swaps (code: team-list portraits over time)
    swaps = (result.team or {}).get("hero_swaps", [])
    for w in facts.get("hero_swaps", []):
        got = next((x for x in swaps if x["slot"] == w["slot"] and x["to_hero"] == w["to_hero"]), None)
        check("영웅 교체", bool(got) and _near(got["time_seconds"], w["time_seconds"], 10.0),
              f"{w['slot']}번 칸 {w['from_hero']}→{w['to_hero']} 정답 약 {fmt_time(w['time_seconds'])} / 결과 "
              + (fmt_time(got["time_seconds"]) if got else "감지 없음"))
    for x in swaps:
        if not any(x["slot"] == w["slot"] and x["to_hero"] == w["to_hero"] for w in facts.get("hero_swaps", [])):
            check("영웅 교체 오탐", False, f"{x['slot']}번 칸 {x['from_hero']}→{x['to_hero']} {fmt_time(x['time_seconds'])} (정답에 없음)")

    # events the model invented before: no feedback near them should state them as fact
    # (a withheld grade with the claim only in the uncertainty does not count)
    for n in facts.get("not_happened", []):
        kws = n.get("keywords", [])
        near = [f for f in result.feedback if f.start_time_seconds - 2 <= n["time_seconds"] <= f.end_time_seconds + 2
                and f.grade != "withheld" and any(k in f.observation + f.assessment for k in kws)]
        check("지어낸 사건 재발", not near, f"{fmt_time(n['time_seconds'])} {n['what']}"
              + (f" → 피드백 {', '.join(f.id for f in near)}" if near else ""))

    # who / where a skill was aimed at, as the moment check saw it
    for st in facts.get("skill_targets", []):
        t = st["time_seconds"]
        mc = _moment(run_dir, fb_checks, st["ability"], t)
        if mc is None:
            check("스킬 대상", None, f"{fmt_time(t)} {st['ability']} — 결과에 해당 피드백/순간 확인 없음")
        else:
            said = mc.get("target", "")
            if any(k in said for k in ("발밑", "자신", "자기", "바닥")):
                got = "self"
            elif "아군" in said and "적" not in said.replace("적중", ""):
                got = "ally"
            elif "적" in said.replace("적중", ""):
                got = "enemy"
            else:
                got = "unknown"
            want = st["target"]
            ok = got == want if want == "ally" or got == "ally" else (got == "self") == (want == "self")
            check("스킬 대상", ok, f"{fmt_time(t)} 정답 {want} / 순간 확인 '{said}'")

    # hit / miss and target as the code saw it (team-list status icon)
    for so in facts.get("skill_outcomes", []):
        t = so["time_seconds"]
        use = next((u for u in result.skill_uses if u["ability_id"] == so["ability"] and _near(u["time_seconds"], t, 1.5)), None)
        tc = (use or {}).get("target_check") or {}
        if not tc:
            check("스킬 명중(코드)", None, f"{fmt_time(t)} {so['ability']} — 코드 판별 없음")
        else:
            ok = (tc.get("status") == "verified") == so["hit"] and (not so.get("target_player") or tc.get("player_name") == so["target_player"])
            check("스킬 명중(코드)", ok, f"{fmt_time(t)} 정답 {'명중 ' + so.get('target_player', '') if so['hit'] else '빗나감'}"
                  f" / 코드 {tc.get('status')} {tc.get('player_name') or ''}")

    # hit / miss, as the moment check (model) reported the result
    for so in facts.get("skill_outcomes", []):
        t = so["time_seconds"]
        mc = _moment(run_dir, fb_checks, so["ability"], t)
        if mc is None:
            check("스킬 명중(모델)", None, f"{fmt_time(t)} {so['ability']} — 순간 확인 없음")
            continue
        said = mc.get("result", "")
        missed = any(k in said for k in ("빗나", "맞지 않", "실패"))
        check("스킬 명중(모델)", missed == (not so["hit"]), f"{fmt_time(t)} 정답 {'명중' if so['hit'] else '빗나감'} / 순간 확인 '{said}'")

    # reflected skills (code: anti-heal icon on the player right after the grenade)
    rf = facts.get("reflected")
    if rf:
        for u in (u for u in result.skill_uses if u["ability_id"] == rf["ability"]):
            want = any(_near(u["time_seconds"], t, 1.5) for t in rf["times"])
            got = (u.get("reflect_check") or {}).get("status")
            check("반사 판별(코드)", (got == "reflected") == want,
                  f"{fmt_time(u['time_seconds'])} 정답 {'반사됨' if want else '반사 없음'} / 코드 {got or '판별 없음'}")

    # the player's own grades for specific plays
    for j in labels.get("judgements", []):
        t = j["time_seconds"]
        hits = [f for f in result.feedback if f.category == j["category"] and f.start_time_seconds - 1 <= t <= f.end_time_seconds + 1]
        grades = [f.grade for f in hits]
        if "not_grade" in j:
            # the coach said it could have been better: no feedback there may call it best / excellent
            # (no feedback at all is a miss as well: the play the coach pointed at was not reviewed)
            check("사용자 판정 일치", bool(grades) and not set(grades) & set(j["not_grade"]),
                  f"{fmt_time(t)} {j['category']} 정답 {'/'.join(j['not_grade'])} 아님 / 결과 {', '.join(grades) or '피드백 없음'} — {j.get('note', '')}")
        else:
            check("사용자 판정 일치", j["grade"] in grades,
                  f"{fmt_time(t)} {j['category']} 정답 {j['grade']} / 결과 {', '.join(grades) or '피드백 없음'} — {j.get('note', '')}")

    judged = [c for c in out["checks"] if c["ok"] is not None]
    n_ok = sum(c["ok"] is True for c in judged)
    out["summary"] = f"{n_ok}/{len(judged)} 일치" + (f" (판단 불가 {len(out['checks']) - len(judged)})" if len(judged) < len(out["checks"]) else "")
    return out


def print_report(report: dict) -> None:
    print(f"{report['run_id']} vs {report['labels']}: {report['summary']}")
    for c in report["checks"]:
        mark = {True: "OK ", False: "NG ", None: "-- "}[c["ok"]]
        print(f"  {mark}{c['kind']}: {c['detail']}")


if __name__ == "__main__":  # pragma: no cover
    import sys

    print(json.dumps(evaluate(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, indent=1))

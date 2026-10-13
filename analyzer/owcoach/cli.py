"""Command line entry point.

  python -m owcoach analyze VIDEO --hero ana [--provider gemini|mock] [--run-id ID] [--rerun team,overview,detail,coaching]
  python -m owcoach probe VIDEO
  python -m owcoach report RUN_DIR
  python -m owcoach heroes
  python -m owcoach schema [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config, load_pricing
from .heroes import available_heroes, load_hero
from .media import InputRejected, probe, validate_input
from .pipeline import DEFAULT_RUNS_DIR, Pipeline, StageFailed
from .providers import ProviderError, create_provider
from .schemas import AnalysisResult


def cmd_analyze(args) -> int:
    cfg = load_config(args.config)
    hero = load_hero(args.hero)
    provider_name = args.provider or cfg["provider"]["name"]
    try:
        provider = create_provider(provider_name, cfg["provider"])
    except ProviderError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    if provider.is_mock:
        print("※ mock 공급자로 실행합니다. 실제 AI 분석이 아닙니다.")
    pipe = Pipeline(cfg, load_pricing(), provider, hero, runs_dir=Path(args.runs_dir))
    rerun = {s.strip() for s in args.rerun.split(",") if s.strip()} if args.rerun else set()
    try:
        result, run_dir = pipe.run(Path(args.video), run_id=args.run_id, game_patch=args.patch, force_stages=rerun)
    except InputRejected as e:
        print("입력 조건 불충족:\n- " + "\n- ".join(e.reasons), file=sys.stderr)
        return 3
    except StageFailed as e:
        print(f"분석 실패 (단계: {e.stage}): {e.reason}", file=sys.stderr)
        if e.stage != "probe":
            print("같은 명령을 다시 실행하면 완료된 단계는 재사용하고 실패한 단계부터 진행합니다.", file=sys.stderr)
        return 4
    from .report import write_report

    report = write_report(run_dir)
    assertive = sum(f.display_mode == "assertive" for f in result.feedback)
    print(f"\n완료: {run_dir / 'result.json'}")
    print(f"검수 리포트: {report}")
    print(f"분석 모드: {result.analysis_mode} | 피드백 {len(result.feedback)}개 (단정 {assertive}, 보류/참고 {len(result.feedback) - assertive})"
          f" | 과제 {len(result.action_items)}개 | 제외 {len(result.discarded)}개")
    tokens = sum((u.total_tokens or 0) for u in result.usage if not u.from_cache)
    print(f"이번 실행 API 토큰 합계: {tokens} | 비용: {result.cost.total if result.cost.total is not None else '미계산'} ({result.cost.note})")
    return 0


def cmd_probe(args) -> int:
    cfg = load_config(args.config)
    path = Path(args.video)
    try:
        info = probe(path)
    except InputRejected as e:
        print("입력 조건 불충족: " + "; ".join(e.reasons))
        return 3
    print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
    reasons = validate_input(path, info, cfg["input"])
    print("입력 조건: 통과" if not reasons else "입력 조건 불충족:\n- " + "\n- ".join(reasons))
    return 0 if not reasons else 3


def cmd_report(args) -> int:
    from .report import write_report

    print(write_report(Path(args.run_dir)))
    return 0


def cmd_heroes(args) -> int:
    for h in available_heroes():
        p = load_hero(h)
        print(f"{p.hero_id}\t{p.display_name_ko}\tprofile {p.profile_version}\t{p.review_status}\tpatch {p.patch_version}\t기준 {len(p.criteria)}개")
    return 0


def cmd_schema(args) -> int:
    text = json.dumps(AnalysisResult.model_json_schema(), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", "utf-8")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="owcoach")
    ap.add_argument("--config", help="analysis.yaml 경로")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="로컬 영상 분석")
    a.add_argument("video")
    a.add_argument("--hero", required=True)
    a.add_argument("--provider", choices=["gemini", "mock"])
    a.add_argument("--run-id", help="작업 ID. 같은 ID로 재실행하면 완료된 단계를 재사용")
    a.add_argument("--patch", help="영상이 녹화된 게임 패치 버전(기록용)")
    a.add_argument("--rerun", help="강제로 다시 실행할 단계(쉼표 구분): proxy,team,overview,detail,coaching")
    a.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    a.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("probe", help="영상 입력 조건만 확인")
    p.add_argument("video")
    p.set_defaults(fn=cmd_probe)

    r = sub.add_parser("report", help="검수용 HTML 리포트 생성")
    r.add_argument("run_dir")
    r.set_defaults(fn=cmd_report)

    sub.add_parser("heroes", help="지원 영웅 목록").set_defaults(fn=cmd_heroes)

    s = sub.add_parser("schema", help="결과 JSON Schema 출력")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_schema)

    args = ap.parse_args(argv)
    return args.fn(args)

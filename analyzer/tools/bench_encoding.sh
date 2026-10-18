#!/usr/bin/env bash
# 인코딩 단축(74fc138) 효과 측정: 이전/새 코드를 같은 영상·같은 1코어(taskset)·mock 으로 처음부터 돌려
# 분석기(단계별 timings)와 결과 화면 만들기(owcoach export)의 걸린 시간을 비교한다. Gemini 비용 없음.
#   analyzer/tools/bench_encoding.sh VIDEO [이전 커밋=7b9161a] [새 커밋=74fc138] [CPU 번호=0]
# 결과: $BENCH_OUT(기본 임시 폴더)/summary.tsv. 영웅은 $BENCH_HERO(기본 auto). 서버와 같은 인자(--max-scenes 40 --max-feedback 100, 480p 360kbps).
set -euo pipefail
video=$(realpath "$1"); old=${2:-7b9161a}; new=${3:-74fc138}; cpu=${4:-0}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
out=${BENCH_OUT:-$(mktemp -d)}; mkdir -p "$out"
trap 'for r in "$old" "$new"; do git -C "$repo" worktree remove --force "$out/src-$r" 2>/dev/null || true; done' EXIT
printf "rev\tstage\tseconds\n" > "$out/summary.tsv"
for rev in "$old" "$new"; do
  wt="$out/src-$rev"
  [ -d "$wt" ] || git -C "$repo" worktree add -f --detach "$wt" "$rev" >/dev/null
  rm -rf "$out/runs-$rev" "$out/site-$rev"
  echo "== $rev 분석기 (CPU $cpu)"
  s=$(date +%s.%N)
  (cd "$wt/analyzer" && taskset -c "$cpu" python -m owcoach analyze "$video" --hero "${BENCH_HERO:-auto}" --provider mock \
     --max-scenes 40 --max-feedback 100 --runs-dir "$out/runs-$rev") > "$out/$rev.analyze.log" 2>&1
  m=$(date +%s.%N)
  run_dir=$(dirname "$(sed -n 's/^완료: //p' "$out/$rev.analyze.log" | tail -1)")
  echo "== $rev 결과 화면"
  (cd "$wt/analyzer" && taskset -c "$cpu" python -m owcoach export "$run_dir" "$out/site-$rev" --video "$video" \
     --height 480 --kbps 360) > "$out/$rev.export.log" 2>&1
  e=$(date +%s.%N)
  python3 - "$rev" "$run_dir/result.json" "$s" "$m" "$e" "$out/site-$rev/media/web.mp4" >> "$out/summary.tsv" <<'PY'
import json, os, sys
rev, res, s, m, e, web = sys.argv[1], sys.argv[2], *map(float, sys.argv[3:6]), sys.argv[6]
for k, v in (json.load(open(res, encoding="utf-8")).get("timings_seconds") or {}).items():
    print(f"{rev}\t{k}\t{v}")
print(f"{rev}\tanalyze_wall\t{m - s:.1f}\n{rev}\texport_wall\t{e - m:.1f}\n{rev}\tall_wall\t{e - s:.1f}")
print(f"{rev}\tweb_mp4_MB\t{os.path.getsize(web) / 1e6:.2f}")
PY
done
python3 - "$out/summary.tsv" "$old" "$new" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8"), delimiter="\t"))
old, new = sys.argv[2], sys.argv[3]
val = {(r["rev"], r["stage"]): float(r["seconds"]) for r in rows}
stages = list(dict.fromkeys(r["stage"] for r in rows))
print(f"{'단계':<14}{old:>10}{new:>10}{'차이':>10}")
for st in stages:
    a, b = val.get((old, st)), val.get((new, st))
    f = lambda x: f"{x:10.1f}" if x is not None else f"{'-':>10}"
    print(f"{st:<14}{f(a)}{f(b)}" + (f"{b - a:+10.1f}" if a is not None and b is not None else ""))
PY
echo "기록: $out"

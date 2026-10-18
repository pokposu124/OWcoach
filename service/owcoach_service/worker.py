"""Background worker: runs one analysis at a time with the analyzer CLI, then exports the result page.

The analyzer runs as a child process (python -m owcoach analyze) so a crash or a long ffmpeg call never
takes the web server down, and its "[n/5] ..." progress lines become the job's stage. A run that stops
half way keeps its finished stages in the analyzer's cache (runs dir, keyed by the video's hash), so a
requeued job does not pay for them again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .db import Store, now_iso

ANALYZER_DIR = Path(__file__).resolve().parents[2] / "analyzer"


class Worker:
    def __init__(self, store: Store, cfg: dict, data_dir: Path, log=print):
        self.store, self.cfg, self.data_dir, self.log = store, cfg, Path(data_dir), log
        self.runs_dir = self.data_dir / "runs"
        self.results_dir = self.data_dir / "results"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        n = self.store.requeue_running()
        if n:
            self.log(f"재시작: 진행 중이던 분석 {n}건을 다시 대기열에 넣음")
        self._thread = threading.Thread(target=self._loop, name="owcoach-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.next_queued()
            if job is None:
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            self.run_job(job)

    # one job ---------------------------------------------------------------------
    def run_job(self, job: dict) -> None:
        jid = job["id"]
        self.store.update_job(jid, status="running", started_at=now_iso(), stage="분석 시작", error="")
        video = Path(job["video_path"])
        cmd = [sys.executable, "-m", "owcoach", "analyze", str(video), "--hero", job.get("hero") or self.cfg["default_hero"],
               *self.cfg["analyze_args"],
               "--provider", self.cfg["provider"], "--runs-dir", str(self.runs_dir)]
        if job["patch"]:
            cmd += ["--patch", job["patch"]]
        tail: list[str] = []
        run_dir = None
        try:
            proc = subprocess.Popen(cmd, cwd=ANALYZER_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
            for line in proc.stdout:
                line = line.rstrip()
                tail = (tail + [line])[-20:]
                if line.startswith("[") and "/5]" in line[:6]:
                    self.store.update_job(jid, stage=line)
                if line.startswith("완료: "):
                    run_dir = Path(line[len("완료: "):]).parent
            code = proc.wait()
            if code != 0 or run_dir is None:
                raise RuntimeError(_failure_text(code, tail))
            self.store.update_job(jid, stage="결과 화면 만드는 중")
            out = self.results_dir / jid
            web = self.cfg["web_video"]
            exp = subprocess.run([sys.executable, "-m", "owcoach", "export", str(run_dir), str(out), "--video", str(video),
                                  "--height", str(web["height"]), "--kbps", str(web["kbps"])],
                                 cwd=ANALYZER_DIR, capture_output=True, text=True)
            if exp.returncode != 0:
                raise RuntimeError("결과 화면을 만들지 못했습니다: " + exp.stderr[-300:])
            result = json.loads((run_dir / "result.json").read_text("utf-8"))
            cost = (result.get("cost") or {}).get("total")
            self.store.update_job(jid, status="done", stage="완료", finished_at=now_iso(),
                                  cost_usd=float(cost) if cost is not None else None, hero=result.get("hero") or job.get("hero"),
                                  map_name=result.get("map_name") or "",
                                  main_heroes=json.dumps(result.get("main_heroes") or [], ensure_ascii=False))
            if not self.cfg.get("keep_original_video"):
                video.unlink(missing_ok=True)  # the result page has its own small copy (media/web.mp4)
            self.log(f"분석 완료 {jid} (비용 ${cost})")
        except Exception as e:  # noqa: BLE001 - any failure ends this job, the worker keeps going
            self.store.update_job(jid, status="failed", finished_at=now_iso(), error=str(e)[-1000:])
            self.log(f"분석 실패 {jid}: {e}")


def _failure_text(code: int, tail: list[str]) -> str:
    # the analyzer's own Korean message (입력 조건 불충족 / 분석 실패 (단계: ...)) when there is one
    msg = [l for l in tail if l.startswith(("입력 조건", "분석 실패", "오류", "- "))]
    return "\n".join(msg) if msg else f"분석기가 코드 {code}로 끝났습니다.\n" + "\n".join(tail[-5:])

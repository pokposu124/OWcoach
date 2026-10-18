"""SQLite store for the beta service: users (Discord accounts) and analysis jobs.

One server, one worker, a handful of users: the standard library's sqlite3 is enough. Every call opens
its own connection so the web handlers and the worker thread never share one.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  discord_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_login_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(discord_id),
  status TEXT NOT NULL,            -- queued | running | done | failed | rejected
  stage TEXT NOT NULL DEFAULT '',  -- last progress line of the analyzer ("[3/5] 후보 장면 상세 분석")
  file_name TEXT NOT NULL,
  video_path TEXT NOT NULL,
  video_sha TEXT NOT NULL DEFAULT '',
  hero TEXT NOT NULL DEFAULT 'auto',   -- auto until the analyzer recognises the player's hero
  patch TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  cost_usd REAL,                   -- this run's Gemini cost from result.json (null until done)
  map_name TEXT NOT NULL DEFAULT '',     -- map the analyzer read (result.json map_name, model reading)
  main_heroes TEXT NOT NULL DEFAULT '',  -- JSON [{hero, seconds, share}], the player's most played heroes (up to 3)
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
            if "hero" not in cols:   # DB made before hero selection
                c.execute("ALTER TABLE jobs ADD COLUMN hero TEXT NOT NULL DEFAULT 'ana'")
            for col in ("map_name", "main_heroes"):   # DB made before the list showed map and heroes
                if col not in cols:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # users ---------------------------------------------------------------------
    def upsert_user(self, discord_id: str, name: str) -> None:
        t = now_iso()
        with self.conn() as c:
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT(discord_id) DO UPDATE SET name=excluded.name, "
                      "last_login_at=excluded.last_login_at", (discord_id, name, t, t))

    def user(self, discord_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        return dict(r) if r else None

    # jobs ----------------------------------------------------------------------
    def create_job(self, user_id: str, file_name: str, video_path: str, patch: str, video_sha: str = "",
                   status: str = "queued", error: str = "", hero: str = "auto") -> str:
        job_id = uuid.uuid4().hex[:16]
        with self.conn() as c:
            c.execute("INSERT INTO jobs (id, user_id, status, file_name, video_path, video_sha, hero, patch, created_at, error) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (job_id, user_id, status, file_name, video_path, video_sha, hero, patch, now_iso(), error))
        return job_id

    def done_job_for_video(self, user_id: str, video_sha: str) -> dict | None:
        """The user's analysis of the same video file, if any (shown again instead of re-running)."""
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE user_id=? AND video_sha=? AND status IN ('queued','running','done') "
                          "ORDER BY created_at DESC LIMIT 1", (user_id, video_sha)).fetchone()
        return dict(r) if r else None

    def job(self, job_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def jobs_of(self, user_id: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC", (user_id,))]

    def user_names(self) -> dict[str, str]:
        with self.conn() as c:
            return {r["discord_id"]: r["name"] for r in c.execute("SELECT discord_id, name FROM users")}

    def jobs_since(self, since_iso: str, user_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM jobs WHERE created_at >= ?", [since_iso]
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def next_queued(self) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return dict(r) if r else None

    def requeue_running(self) -> int:
        """Jobs left 'running' by a stopped server go back to the queue (finished stages are cached)."""
        with self.conn() as c:
            return c.execute("UPDATE jobs SET status='queued', stage='' WHERE status='running'").rowcount

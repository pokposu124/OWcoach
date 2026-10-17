"""Usage limits for the free beta: requests per user per 7 days and a monthly Gemini budget.

The owner pays for every analysis (server-side API key), so both limits are checked before a job is
accepted. Cost is known only after a run (result.json), so queued and running jobs hold a reserve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Store

COUNTED = ("queued", "running", "done")   # rejected input and failed runs do not use up a user's requests


@dataclass
class Usage:
    week_used: int
    week_limit: int
    next_slot_at: datetime | None         # when the oldest counted request leaves the 7-day window
    month_spent: float                    # finished runs' cost + reserves of queued/running/failed ones
    month_budget: float
    reserve: float

    @property
    def week_left(self) -> int:
        return max(0, self.week_limit - self.week_used)

    @property
    def budget_left(self) -> float:
        return max(0.0, self.month_budget - self.month_spent)

    def refusal(self) -> str | None:
        """Why a new request is refused now (Korean, shown to the user), or None."""
        if self.week_left <= 0:
            when = self.next_slot_at.strftime("%m월 %d일 %H:%M") if self.next_slot_at else "잠시 후"
            return f"이번 주 분석 {self.week_limit}회를 모두 썼습니다. {when}부터 다시 요청할 수 있습니다."
        if self.month_spent + self.reserve > self.month_budget:
            return f"이번 달 분석 예산(${self.month_budget:.0f})을 다 써서 다음 달까지 새 분석을 받지 않습니다."
        return None


def month_start(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def job_cost(job: dict, reserve: float) -> float:
    if job["status"] == "done" and job["cost_usd"] is not None:
        return float(job["cost_usd"])
    if job["status"] in ("queued", "running", "failed"):
        return reserve       # failed: some stages may have been paid; count the reserve to stay safe
    return 0.0               # rejected before any API call


def usage(store: Store, user_id: str, cfg: dict, now: datetime | None = None) -> Usage:
    now = now or datetime.now(timezone.utc)
    tz = cfg["timezone"]
    week = [j for j in store.jobs_since((now - timedelta(days=7)).isoformat(timespec="seconds"), user_id)
            if j["status"] in COUNTED]
    oldest = min((datetime.fromisoformat(j["created_at"]) for j in week), default=None)
    reserve = float(cfg["job_reserve_usd"])
    month = store.jobs_since(month_start(now, tz).astimezone(timezone.utc).isoformat(timespec="seconds"))
    return Usage(
        week_used=len(week), week_limit=int(cfg["weekly_limit"]),
        next_slot_at=(oldest + timedelta(days=7)).astimezone(ZoneInfo(tz)) if oldest else None,
        month_spent=round(sum(job_cost(j, reserve) for j in month), 4),
        month_budget=float(cfg["monthly_budget_usd"]), reserve=reserve,
    )

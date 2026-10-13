"""User-facing time format: "n분 n초" (whole seconds), "n초" under a minute.

Model-written text uses seconds ("132.75초"); normalize_times() rewrites every "<number>초" in it.
Because the output is still a length of time, durations ("쿨다운 12초") stay correct too.
"""

from __future__ import annotations

import re

_SECONDS = re.compile(r"(\d+(?:\.\d+)?)\s*초")


def fmt_time(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m}분 {s}초" if m else f"{s}초"


def fmt_range(start: float, end: float) -> str:
    return f"{fmt_time(start)} ~ {fmt_time(end)}"


_DUPLICATE = re.compile(r"((?:\d+분 )?\d+초)\s*\(\s*\1\s*\)")


def normalize_times(text: str) -> str:
    """Rewrite "<seconds>초" as "n분 n초" and drop the duplicate a model's "134초(2분 14초)" turns into."""
    return _DUPLICATE.sub(r"\1", _SECONDS.sub(lambda m: fmt_time(float(m.group(1))), text))

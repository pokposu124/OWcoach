"""Read-only game data: hero / ability numbers from the bundled Overwatch stats snapshot.

Source: analyzer/data/overwatch_stats (Overwatch Wiki snapshot 2026-10-15, CC BY-NC-SA 3.0, selected values
checked against official patch notes up to 2026-10-13). Rules from its CLAUDE_USAGE.md:
- look up only the heroes / abilities of the match, never hand the whole dataset to the model;
- every value goes out with its unit, condition, status (wiki_only / official_component_matched ...),
  review flag and pinned source revision;
- base values, perk-modified values and what the video shows are kept apart: an unknown perk is never
  assumed active, and current health / remaining cooldown come from the HUD, not from this data;
- values flagged for review are not used as grounds for confident judgements;
- the match's patch is not known from the video, so the data is a premise to be confirmed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .config import ANALYZER_ROOT

DEFAULT_DB = ANALYZER_ROOT / "data" / "overwatch_stats" / "overwatch.sqlite"
SUPPORTED_MODES = ("5v5_role_queue",)
STATUS_KO = {"wiki_only": "위키", "official_component_matched": "공식 패치 대조(이 항목만)",
             "inferred_absent_zero": "표기 없음→0 추정"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class GameData:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        meta = {r["key"]: json.loads(r["value_json"]) for r in self.db.execute("SELECT key, value_json FROM metadata")}
        self.meta = {
            "snapshot_date": meta.get("snapshot_date_kst"),
            "latest_official_patch_checked": meta.get("latest_official_patch_date_checked"),
            "whole_dataset_officially_verified": meta.get("whole_dataset_officially_verified"),
            "target_mode": meta.get("target_mode"),
            "attribution": meta.get("attribution"),
        }

    # ------------------------------------------------------------------ lookup
    def hero_id(self, name: str) -> str | None:
        """hero_id from a hero_id, English name or Korean display name (as our portrait sheet uses)."""
        row = self.db.execute("SELECT hero_id FROM heroes WHERE hero_id=? OR name_en=? COLLATE NOCASE OR name_ko=?",
                              (name, name, name)).fetchone()
        return row["hero_id"] if row else None

    def ability_id(self, hero_id: str, name_en: str) -> str | None:
        row = self.db.execute("SELECT ability_id FROM abilities WHERE hero_id=? AND (ability_id=? OR name_en=? COLLATE NOCASE)",
                              (hero_id, f"{hero_id}:{_slug(name_en)}", name_en)).fetchone()
        return row["ability_id"] if row else None

    def _source(self, source_id: str) -> dict:
        r = self.db.execute("SELECT revision_url, last_edited_text, fetched_at_utc, license FROM sources WHERE source_id=?",
                            (source_id,)).fetchone()
        return dict(r) if r else {}

    def get_hero_stats(self, hero_id: str, mode: str = "5v5_role_queue") -> dict:
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"지원하지 않는 모드: {mode} (수집본은 {', '.join(SUPPORTED_MODES)} 기본 형태만)")
        row = self.db.execute("SELECT * FROM heroes WHERE hero_id=?", (hero_id,)).fetchone()
        if not row:
            raise KeyError(hero_id)
        data = json.loads(row["data_json"])
        hp = {k: {"value": v.get("value"), "text": v.get("text"), "condition": v.get("selected_condition"),
                  "variants": v.get("variants", []), "status": v.get("status")}
              for k, v in (data.get("hitpoints") or {}).items() if isinstance(v, dict)}
        return {"hero_id": hero_id, "name_en": row["name_en"], "name_ko": row["name_ko"], "role": row["role"],
                "subrole": row["subrole"], "mode": mode, "total_hp": row["total_hp"], "hitpoints": hp,
                "patch_id": data.get("patch_id"), "review_required": bool(row["review_required"]),
                "source": self._source(row["source_id"])}

    def get_ability_stats(self, hero_id: str, ability_id: str) -> dict:
        row = self.db.execute("SELECT * FROM abilities WHERE hero_id=? AND ability_id=?", (hero_id, ability_id)).fetchone()
        if not row:
            raise KeyError(ability_id)
        data = json.loads(row["data_json"])
        return {
            "ability_id": ability_id, "name_en": row["name_en"], "kind": row["kind"], "input_slot": row["input_slot"],
            "is_perk": bool(row["is_perk"]), "description": row["description"],
            "cooldown": data.get("cooldown"), "ultimate_charge_cost": row["ultimate_charge_cost"],
            "stats": [{"label": s["label"], "text": s["text"], "unit": (s.get("components") or [{}])[0].get("unit")
                       if len(s.get("components") or []) == 1 else None, "status": s.get("status"),
                       "review_required": bool(s.get("review_required"))} for s in data.get("stats", [])],
            "effects": [{"label": e["label"], "text": e.get("effect_and_exceptions")} for e in data.get("effects", [])],
            "review_required": bool(row["review_required"]), "review_notes": data.get("review_notes"),
            "source": self._source(row["source_id"]),
        }

    def get_perks(self, hero_id: str) -> list[dict]:
        rows = self.db.execute("SELECT ability_id FROM abilities WHERE hero_id=? AND is_perk=1 ORDER BY kind, name_en",
                               (hero_id,)).fetchall()
        return [self.get_ability_stats(hero_id, r["ability_id"]) for r in rows]

    def get_review_items(self, hero_id: str) -> list[dict]:
        unresolved = [dict(r) for r in self.db.execute("SELECT * FROM review_queue WHERE hero_id=?", (hero_id,))]
        manual = [dict(r) for r in self.db.execute("SELECT * FROM manual_reviews WHERE hero_id=?", (hero_id,))]
        return unresolved + manual

    def abilities(self, hero_id: str) -> list[str]:
        return [r["ability_id"] for r in self.db.execute(
            "SELECT ability_id FROM abilities WHERE hero_id=? AND is_perk=0 ORDER BY rowid", (hero_id,))]

    # ------------------------------------------------------------------ prompt text
    def facts_text(self, player_hero: str, other_heroes: list[str], full_keys: tuple[str, ...] = (), brief_keys: tuple[str, ...] = ()) -> str:
        """Compact numbers for this match only: the player's hero in detail, the other heroes briefly
        (total HP, each ability's cooldown and the stats in brief_keys). Every value carries its status."""
        def stat_line(s: dict) -> str:
            flag = " ⚠검토 필요" if s["review_required"] else ""
            return f"{s['label']} {s['text'].replace(chr(10), ' / ')} [{STATUS_KO.get(s['status'], s['status'])}]{flag}"

        def ability_line(hid: str, aid: str, keys: tuple[str, ...], brief: bool = False) -> str:
            a = self.get_ability_stats(hid, aid)
            parts = []
            cd = a.get("cooldown") or {}
            if cd.get("text"):
                parts.append(f"쿨타임 {cd['text'].replace(chr(10), ' ')}" + (f" (최대 {cd['max_charges']}회 충전)" if cd.get("max_charges") else "")
                             + f" [{STATUS_KO.get(cd.get('status'), cd.get('status'))}]")
            if a["ultimate_charge_cost"]:
                parts.append(f"궁극기 충전 비용 {a['ultimate_charge_cost']:g} (초 단위 대기시간 아님)")
            parts += [stat_line(s) for s in a["stats"] if not keys or _slug(s["label"]).replace("-", "_") in keys]
            flag = " ⚠검토 필요" if a["review_required"] else ""
            if not parts:
                return "" if brief else f"  - {a['name_en']} ({a['kind']}){flag}"
            return f"  - {a['name_en']} ({a['kind']}){flag}: " + "; ".join(parts)

        def hero_line(h: dict) -> str:
            variants = [f"{k} {v2.get('value')}({v2.get('condition')})" for k, v in h["hitpoints"].items()
                        for v2 in v.get("variants", []) if v2.get("condition") not in (None, "default", v.get("condition"))]
            return f"{h['name_ko']} (기본 총체력 {h['total_hp']:g}" + (f"; 변형: {', '.join(variants)}" if variants else "") + ")"

        lines = [f"- 출처: Overwatch Wiki 수집본 {self.meta['snapshot_date']} (CC BY-NC-SA 3.0). 공식 패치 대조는 "
                 f"{self.meta['latest_official_patch_checked']}까지 일부 항목만. 경기 패치는 영상으로 확인할 수 없어 이 수치는 확인이 필요한 전제입니다.",
                 "- 기본 수치입니다. 선택한 특전은 화면으로 확인되지 않으면 적용됐다고 단정하지 마세요. 현재 체력·남은 쿨타임은 HUD 관찰을 따르세요.",
                 "- ⚠검토 필요 표시 수치는 확정 판단의 근거로 쓰지 마세요."]
        hid = self.hero_id(player_hero)
        if hid:
            h = self.get_hero_stats(hid)
            lines.append("플레이어 영웅 " + hero_line(h))
            lines += [ability_line(hid, aid, full_keys) for aid in self.abilities(hid)]
            lines += [f"  - ⚠검토 항목: {r.get('ability_id') or ''} {r.get('reason') or r.get('field_path') or ''}"
                      for r in self.get_review_items(hid)]
            perks = self.get_perks(hid)
            if perks:
                lines.append("  - 특전(선택 여부 미확인): " + ", ".join(f"{p['name_en']}({p['kind']})" for p in perks))
        seen = {hid}
        for name in other_heroes:
            oid = self.hero_id(name)
            if not oid or oid in seen:
                continue
            seen.add(oid)
            h = self.get_hero_stats(oid)
            lines.append(hero_line(h))
            lines += [x for x in (ability_line(oid, aid, brief_keys, brief=True) for aid in self.abilities(oid)) if x]
            for r in self.get_review_items(oid):
                lines.append(f"  - ⚠검토 항목: {r.get('ability_id') or ''} {r.get('reason') or r.get('field_path') or ''}")
        return "\n".join(lines)


@lru_cache(maxsize=2)
def load_gamedata(path: str | None = None) -> GameData:
    return GameData(Path(path) if path else DEFAULT_DB)

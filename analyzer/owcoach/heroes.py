"""Hero coaching profiles, kept as data files separate from the common pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import ANALYZER_ROOT, HEROES_DIR, load_yaml

Category = Literal["skill_usage", "positioning", "ultimate_usage"]
ReviewStatus = Literal["draft", "approved", "deprecated"]


class Source(BaseModel):
    type: str
    reference: str | None = None
    patch_version: str | None = None


class Criterion(BaseModel):
    id: str
    category: Category
    status: ReviewStatus
    # skill_usage / ultimate_usage 기준이 다루는 스킬 id. 지정되면 그 스킬의 사용 기록만 HUD 대조 근거로 쓴다
    ability: str | None = None
    title: str
    description: str
    good_signs: list[str] = Field(default_factory=list)
    bad_signs: list[str] = Field(default_factory=list)
    cannot_judge_when: list[str] = Field(default_factory=list)
    source: Source


class Ability(BaseModel):
    id: str
    name_ko: str
    name_en: str
    hud_hint: str | None = None
    # HUD 대조용 슬롯 (config hud_check.regions 의 키). 없으면 HUD로 검증하지 않음
    hud_slot: str | None = None
    # 대상 판별 방식 (config hud_check 의 키, 예: buff_arrow). 없으면 대상을 판별하지 않음
    target_marker: str | None = None
    # 맞으면 대상이 몇 초간 행동하지 못하는 스킬(수면총 등). 사용 직후 플레이어가 죽었다면 명중 주장을 의심한다
    disables_target: bool = False
    # True면 HUD로 감지한 이 스킬의 모든 사용 순간을 후보 장면으로 강제 포함 (전체 흐름 모델 선택·장면 수 제한과 무관).
    # 영웅마다 따로 정한다. 궁극기는 이 값과 상관없이 항상 강제 포함
    force_scene: bool = False
    # 반사(겐지 튕겨내기 등)되어 돌아왔을 때 플레이어에게 남는 표시 (config hud_check 의 키, 예: self_antiheal).
    # 적 팀에 같은 영웅이 있으면 그 표시가 적의 스킬일 수 있어 판별하지 않음
    reflect_marker: str | None = None
    # 사용 직후 플레이어의 처치 관여를 확인하는 방식 (config hud_check 의 키, 예: kill_banner)
    kill_marker: str | None = None
    # 사용 효과를 받은 플레이어를 확인하는 방식 (config hud_check 의 키, 예: antiheal_bars)
    effect_marker: str | None = None
    # 아군·자신이 효과를 받았는지 확인하는 방식 (config hud_check 의 키, 예: heal_boost)
    ally_marker: str | None = None
    # 사용 순간 아군 체력 상태를 붙일지 (config hud_check.team_health): 적에게 써도 되는 상황이었는지 판단용
    ally_health: bool = False


class NumericFact(BaseModel):
    id: str
    value: float | str
    unit: str | None = None
    source: str  # required: no number without a source
    patch_version: str  # required


class HeroProfile(BaseModel):
    hero_id: str
    display_name_ko: str
    display_name_en: str
    role: str
    profile_version: str
    review_status: ReviewStatus
    patch_version: str
    patch_note: str | None = None
    abilities: list[Ability]
    observable_signals: list[str]
    criteria: list[Criterion]
    numeric_facts: list[NumericFact] = Field(default_factory=list)
    # enemy skills that block or reflect the player's skills (checked at every HUD-detected use)
    skill_counters: list[str] = Field(default_factory=list)
    # coaching prompt paragraph on how this hero positions (and how to read the code-measured on-screen
    # presence spans); Ana's is the user's own rule, generated profiles get the role's common principles
    positioning_guidance: str = ""
    # code-measured "no ally on screen" / "looking away from the team" spans withhold a good positioning grade
    # (Ana: the user's rule "behind the team by default"; not for roles that stand ahead or flank)
    presence_withholds_good_positioning: bool = False
    # built from the stats DB and review packs, not from a heroes/<id>.yaml: no HUD slot is calibrated
    generated: bool = False

    def criterion(self, criterion_id: str) -> Criterion | None:
        return next((c for c in self.criteria if c.id == criterion_id), None)

    def active_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria if c.status != "deprecated"]

    def criteria_for_prompt(self) -> list[dict[str, Any]]:
        out = []
        for c in self.active_criteria():
            out.append({
                "id": c.id,
                "category": c.category,
                # draft criteria are offered as references; a grade resting only on them stays tentative
                "review_status": "approved" if c.status == "approved" else "draft (검수 전 참고)",
                "title": c.title,
                "description": c.description.strip(),
                "good_signs": c.good_signs,
                "bad_signs": c.bad_signs,
                "cannot_judge_when": c.cannot_judge_when,
            })
        if self.numeric_facts:
            out.append({"verified_numeric_facts": [f.model_dump() for f in self.numeric_facts]})
        return out


COMMON_FILE = "common.yaml"


def available_heroes(heroes_dir: Path = HEROES_DIR, knowledge_packs: list[dict] | None = None,
                     generated: dict | None = None) -> list[str]:
    """Heroes with a profile file, plus (when generated profiles are on) every hero a review pack covers."""
    own = {p.stem for p in heroes_dir.glob("*.yaml") if p.name != COMMON_FILE}
    if generated and generated.get("enabled"):
        own |= pack_heroes(knowledge_packs)
    return sorted(own)


def load_comp_notes(heroes_dir: Path = HEROES_DIR) -> list[dict]:
    """Coach notes on team compositions (heroes/common.yaml comp_notes): which comp wins how."""
    path = heroes_dir / COMMON_FILE
    return list(load_yaml(path).get("comp_notes") or []) if path.exists() else []


def _pack_files(pack: dict, hero_id: str, role: str) -> list[Path]:
    """The hero's file and the pack's common file for the role, when the pack covers that role."""
    if not pack.get("enabled") or role not in (pack.get("roles") or [role]):
        return []
    root = ANALYZER_ROOT / pack["path"]
    common = pack["common_file"]
    common = common.get(role) if isinstance(common, dict) else common
    return [p for p in (root / pack["hero_file"].format(hero=hero_id), root / common if common else None) if p and p.exists()]


def knowledge_criteria(hero_id: str, have: set[str], packs: list[dict] | None, role: str = "support") -> list[dict]:
    """Criteria from review packs under analyzer/ (e.g. knowledge/support_positioning): the hero's file and
    the pack's common file for the hero's role. A pack criterion listed in covered_by is skipped when the
    profile already has one of the criteria it maps to (the pack's own table: the same mistake is not
    graded twice)."""
    out = []
    for pack in packs or []:
        covered = pack.get("covered_by") or {}
        for path in _pack_files(pack, hero_id, role):
            for c in load_yaml(path).get("criteria") or []:
                if not set(covered.get(c["id"], [])) & have:
                    out.append(c)
    return out


def pack_heroes(packs: list[dict] | None) -> set[str]:
    """Heroes an enabled review pack has a criteria file for."""
    out = set()
    for pack in packs or []:
        if pack.get("enabled"):
            out |= {p.stem for p in (ANALYZER_ROOT / pack["path"]).glob(pack["hero_file"].format(hero="*"))}
    return out


def hero_id_for_name(name_ko: str) -> str | None:
    """Stats-DB hero id for a Korean hero name as the portrait sheet writes it (e.g. "솔저: 76" -> soldier-76)."""
    import json

    path = ANALYZER_ROOT / "data" / "overwatch_stats" / "hero_localizations.json"
    return next((x["key"] for x in json.loads(path.read_text("utf-8")) if x["name"] == name_ko), None)


def generated_profile_data(hero_id: str, packs: list[dict] | None, gcfg: dict) -> dict | None:
    """Profile data for a hero without heroes/<id>.yaml, from the bundled stats DB (names, role, abilities)
    and the review packs (criteria, role principles). Ability names are the DB's English names (the DB has
    no Korean ones; none is made up). No ability gets a HUD slot: HUD reading is calibrated on Ana only."""
    import json
    import sqlite3

    data_dir = ANALYZER_ROOT / "data" / "overwatch_stats"
    db = sqlite3.connect(f"file:{data_dir / 'overwatch.sqlite'}?mode=ro", uri=True)
    row = db.execute("SELECT name_en, lower(role) FROM heroes WHERE hero_id=?", (hero_id,)).fetchone()
    if not row or hero_id not in pack_heroes(packs):
        return None
    name_en, role = row
    names_ko = {x["key"]: x["name"] for x in json.loads((data_dir / "hero_localizations.json").read_text("utf-8"))}
    abilities = [{"id": aid.split(":", 1)[1].replace("-", "_"), "name_ko": n, "name_en": n, "hud_hint": kind}
                 for aid, n, kind in db.execute(
                     "SELECT ability_id, name_en, kind FROM abilities WHERE hero_id=? AND is_perk=0 "
                     "AND kind IN ('Weapon','Ability','Ultimate Ability') ORDER BY rowid", (hero_id,))]
    rcfg = (gcfg.get("roles") or {}).get(role) or {}
    titles = []
    for pack in packs or []:
        if pack.get("enabled") and role in (pack.get("roles") or [role]):
            common = pack["common_file"]
            common = common.get(role) if isinstance(common, dict) else common
            path = ANALYZER_ROOT / pack["path"] / common if common else None
            if path and path.exists():
                titles += [f"{c['id']}: {c['title']}" for c in load_yaml(path).get("criteria") or []]
    guidance = "\n".join([
        f"{names_ko.get(hero_id, name_en)}({rcfg.get('name_ko', role)}) 포지셔닝: 아래 역할 공통 원칙(검수 전 초안, 기준 목록에 있음)과 이 영웅의 기준으로 판단하세요.",
        *(f"- {t}" for t in titles),
        *(gcfg.get("presence_guidance") or "").strip().splitlines(),
    ])
    return {
        "hero_id": hero_id, "display_name_ko": names_ko.get(hero_id, name_en), "display_name_en": name_en,
        "role": role, "profile_version": f"generated-{gcfg.get('version', '0')}", "review_status": "draft",
        "patch_version": "unverified",
        "patch_note": "자동 생성 프로필: 수집본 DB의 이름·역할·스킬과 검수 전 지식베이스 기준. HUD 판독은 아나 영상으로만 맞춰져 있어 연결하지 않음.",
        "abilities": abilities, "observable_signals": list(gcfg.get("observable_signals") or []), "criteria": [],
        "positioning_guidance": guidance, "generated": True,
    }


def load_hero(hero_id: str, heroes_dir: Path = HEROES_DIR, knowledge_packs: list[dict] | None = None,
              generated: dict | None = None) -> HeroProfile:
    path = heroes_dir / f"{hero_id}.yaml"
    if path.exists():
        data = load_yaml(path)
    else:
        data = generated_profile_data(hero_id, knowledge_packs, generated) if generated and generated.get("enabled") else None
        if data is None:
            raise ValueError(f"지원하지 않는 영웅: {hero_id!r} (지원: {', '.join(available_heroes(heroes_dir, knowledge_packs, generated))})")
    common_path = heroes_dir / COMMON_FILE
    if common_path.exists():
        # knowledge that holds for every hero (e.g. no skills into an enemy Genji's deflect)
        common = load_yaml(common_path)
        data["observable_signals"] = [*data.get("observable_signals", []), *common.get("observable_signals", [])]
        # approvals were given for the roles in approved_roles (Ana coaching): for another role the same
        # criteria come in as drafts until someone reviews them for that role
        ok_roles = common.get("approved_roles")
        shared = []
        for c in common.get("criteria", []):
            roles = c.get("approved_roles") or ok_roles
            c = {k: v for k, v in c.items() if k != "approved_roles"}
            shared.append(c if not roles or data.get("role") in roles or c.get("status") != "approved" else {**c, "status": "draft"})
        data["criteria"] = [*data.get("criteria", []), *shared]
        data["skill_counters"] = [*data.get("skill_counters", []), *common.get("skill_counters", [])]
        data["profile_version"] = f"{data['profile_version']}+common-{common.get('common_version', '?')}"
    if knowledge_packs:
        extra = knowledge_criteria(hero_id, {c["id"] for c in data["criteria"]}, knowledge_packs, data.get("role", "support"))
        if extra:
            data["criteria"] = [*data["criteria"], *extra]
            data["profile_version"] += "+knowledge-" + "-".join(
                str(load_yaml(f).get("pack_version", "?")) for p in knowledge_packs
                for f in _pack_files(p, hero_id, data.get("role", "support"))[-1:])
    profile = HeroProfile.model_validate(data)
    if profile.hero_id != hero_id:
        raise ValueError(f"{path}: hero_id {profile.hero_id!r} does not match file name")
    ids = [c.id for c in profile.criteria]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate criterion ids")
    return profile

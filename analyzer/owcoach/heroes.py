"""Hero coaching profiles, kept as data files separate from the common pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import HEROES_DIR, load_yaml

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


def available_heroes(heroes_dir: Path = HEROES_DIR) -> list[str]:
    return sorted(p.stem for p in heroes_dir.glob("*.yaml") if p.name != COMMON_FILE)


def load_hero(hero_id: str, heroes_dir: Path = HEROES_DIR) -> HeroProfile:
    path = heroes_dir / f"{hero_id}.yaml"
    if not path.exists():
        raise ValueError(f"지원하지 않는 영웅: {hero_id!r} (지원: {', '.join(available_heroes(heroes_dir))})")
    data = load_yaml(path)
    common_path = heroes_dir / COMMON_FILE
    if common_path.exists():
        # knowledge that holds for every hero (e.g. no skills into an enemy Genji's deflect)
        common = load_yaml(common_path)
        data["observable_signals"] = [*data.get("observable_signals", []), *common.get("observable_signals", [])]
        data["criteria"] = [*data.get("criteria", []), *common.get("criteria", [])]
        data["profile_version"] = f"{data['profile_version']}+common-{common.get('common_version', '?')}"
    profile = HeroProfile.model_validate(data)
    if profile.hero_id != hero_id:
        raise ValueError(f"{path}: hero_id {profile.hero_id!r} does not match file name")
    ids = [c.id for c in profile.criteria]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate criterion ids")
    return profile

"""Versioned prompt templates (Markdown with a small YAML front matter)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

from .config import PROMPTS_DIR


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    template: str

    def render(self, **values: object) -> str:
        # substitute() raises on missing placeholders so a template/code mismatch fails loudly
        return Template(self.template).substitute({k: str(v) for k, v in values.items()})


def load_prompt(prompt_id: str, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    text = (prompts_dir / f"{prompt_id}.md").read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"prompt {prompt_id} is missing front matter")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    return Prompt(id=meta["id"], version=str(meta["version"]), template=body.strip())

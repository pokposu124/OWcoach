"""Provider interface. The pipeline only talks to this, so the AI vendor can be swapped."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProviderError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    input_tokens_by_modality: dict[str, int] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    data: Any  # parsed JSON
    raw_text: str
    usage: Usage
    model: str
    latency_seconds: float
    attempts: int = 1
    retry_errors: list[str] = field(default_factory=list)


@dataclass
class VideoRequest:
    video_path: Path
    prompt: str
    response_schema: dict
    fps: float | None = None
    media_resolution: str | None = None
    temperature: float | None = None
    # free-form hints only used by the mock provider (stage name, clip duration, ...)
    context: dict = field(default_factory=dict)


@dataclass
class TextRequest:
    prompt: str
    response_schema: dict
    temperature: float | None = None
    context: dict = field(default_factory=dict)


class AnalysisProvider(ABC):
    name: str
    is_mock: bool = False

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    def sdk_version(self) -> str | None:
        return None

    @abstractmethod
    def analyze_video(self, req: VideoRequest) -> ProviderResponse: ...

    @abstractmethod
    def generate_json(self, req: TextRequest) -> ProviderResponse: ...

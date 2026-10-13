from .base import AnalysisProvider, ProviderError, ProviderResponse, TextRequest, Usage, VideoRequest


def create_provider(name: str, settings: dict) -> AnalysisProvider:
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(settings)
    if name == "mock":
        from .mock import MockProvider

        return MockProvider(settings)
    raise ProviderError(f"unknown provider: {name}")


__all__ = [
    "AnalysisProvider", "ProviderError", "ProviderResponse", "TextRequest", "Usage", "VideoRequest",
    "create_provider",
]

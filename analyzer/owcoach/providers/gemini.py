"""Gemini provider (google-genai SDK, Gemini Developer API).

SDK usage follows the official google-genai README (files.upload / files.get / files.delete,
generate_content with response_mime_type + response_json_schema) and the SDK's own types
(Part.video_metadata = VideoMetadata(fps=...), FileState.PROCESSING/ACTIVE/FAILED).
The model id is never hard-coded: it must be supplied through OWCOACH_GEMINI_MODEL.
"""

from __future__ import annotations

import json
import os
import sys
import time

from .base import (
    AnalysisProvider,
    ProviderError,
    ProviderResponse,
    TextRequest,
    Usage,
    VideoRequest,
)

RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


class GeminiProvider(AnalysisProvider):
    name = "gemini"

    def __init__(self, settings: dict):
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as e:  # pragma: no cover
            raise ProviderError("google-genai 패키지가 필요합니다: pip install -r requirements.txt") from e
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ProviderError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
        model = os.environ.get("OWCOACH_GEMINI_MODEL", "").strip()
        if not model:
            raise ProviderError(
                "OWCOACH_GEMINI_MODEL 환경 변수가 비어 있습니다. 공식 모델 목록에서 영상 입력을 지원하는 모델 ID를 확인해 설정하세요."
            )
        self._genai, self._types, self._errors = genai, types, errors
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._settings = settings

    @property
    def model(self) -> str:
        return self._model

    @property
    def sdk_version(self) -> str | None:
        return f"google-genai {self._genai.__version__}"

    # ------------------------------------------------------------------ helpers

    def _with_retries(self, fn):
        retries = int(self._settings.get("max_retries", 3))
        errors: list[str] = []
        for attempt in range(retries + 1):
            try:
                return fn(), attempt + 1, errors
            except self._errors.APIError as e:
                if e.code not in RETRYABLE_CODES or attempt == retries:
                    raise ProviderError(f"Gemini API 오류 {e.code}: {e.message} (시도 {attempt + 1}회)") from e
                errors.append(f"{e.code} {e.message}"[:300])
                wait = min(float(self._settings.get("retry_base_seconds", 1)) * 2 ** (attempt + 1),
                           float(self._settings.get("retry_max_wait_seconds", 60)))
                print(f"  Gemini API 오류 {e.code}, {wait:g}초 후 재시도 ({attempt + 1}/{retries})", file=sys.stderr, flush=True)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _upload_and_wait(self, path):
        types = self._types
        f = self._client.files.upload(
            file=str(path), config=types.UploadFileConfig(mime_type="video/mp4", display_name=path.name)
        )
        deadline = time.monotonic() + float(self._settings.get("file_poll_timeout_seconds", 600))
        interval = float(self._settings.get("file_poll_interval_seconds", 3))
        while f.state == types.FileState.PROCESSING:
            if time.monotonic() > deadline:
                self._safe_delete(f)
                raise ProviderError("Gemini 파일 처리 대기 시간이 초과되었습니다.")
            time.sleep(interval)
            f = self._client.files.get(name=f.name)
        if f.state != types.FileState.ACTIVE:
            self._safe_delete(f)
            raise ProviderError(f"Gemini 파일 처리 실패: state={f.state} error={f.error}")
        return f

    def _safe_delete(self, f) -> None:
        if not self._settings.get("delete_uploaded_files", True):
            return
        try:
            self._client.files.delete(name=f.name)
        except Exception:  # best effort; files also expire server-side
            pass

    def _config(self, schema: dict, temperature: float | None, media_resolution: str | None):
        kwargs = {"response_mime_type": "application/json", "response_json_schema": schema}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if media_resolution:
            kwargs["media_resolution"] = media_resolution
        return self._types.GenerateContentConfig(**kwargs)

    def _parse(self, resp, started: float, attempts: int, retry_errors: list[str]) -> ProviderResponse:
        text = resp.text
        if not text:
            reason = None
            if resp.candidates:
                reason = resp.candidates[0].finish_reason
            raise ProviderError(f"모델 응답이 비어 있습니다 (finish_reason={reason}, prompt_feedback={resp.prompt_feedback})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"모델 응답이 JSON이 아닙니다: {e}") from e
        um = resp.usage_metadata
        usage = Usage()
        if um is not None:
            usage = Usage(
                input_tokens=um.prompt_token_count,
                output_tokens=um.candidates_token_count,
                thinking_tokens=um.thoughts_token_count,
                total_tokens=um.total_token_count,
                input_tokens_by_modality={
                    str(d.modality.value if hasattr(d.modality, "value") else d.modality): d.token_count or 0
                    for d in (um.prompt_tokens_details or [])
                },
            )
        return ProviderResponse(
            data=data, raw_text=text, usage=usage, model=resp.model_version or self._model,
            latency_seconds=round(time.monotonic() - started, 3), attempts=attempts,
            retry_errors=retry_errors,
        )

    # ------------------------------------------------------------------ API

    def analyze_video(self, req: VideoRequest) -> ProviderResponse:
        types = self._types
        started = time.monotonic()
        f = self._upload_and_wait(req.video_path)
        try:
            video_part = types.Part(
                file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type),
                video_metadata=types.VideoMetadata(fps=req.fps) if req.fps else None,
            )
            contents = [types.Content(role="user", parts=[video_part, types.Part(text=req.prompt)])]
            config = self._config(req.response_schema, req.temperature, req.media_resolution)
            resp, attempts, retry_errors = self._with_retries(
                lambda: self._client.models.generate_content(model=self._model, contents=contents, config=config)
            )
            return self._parse(resp, started, attempts, retry_errors)
        finally:
            self._safe_delete(f)

    def generate_json(self, req: TextRequest) -> ProviderResponse:
        started = time.monotonic()
        config = self._config(req.response_schema, req.temperature, None)
        resp, attempts, retry_errors = self._with_retries(
            lambda: self._client.models.generate_content(model=self._model, contents=req.prompt, config=config)
        )
        return self._parse(resp, started, attempts, retry_errors)

"""Strict JSON-to-speech adapter for the isolated Kokoro process."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from ..config import settings


class InvalidSpeechContent(ValueError):
    """The model response is not a JSON object containing speakable text."""


class SpeechServiceUnavailable(RuntimeError):
    """The configured Kokoro process could not be reached."""


class SpeechSynthesisError(RuntimeError):
    """Kokoro rejected the request or returned an invalid response."""


@dataclass(frozen=True)
class SpeechAudio:
    content: bytes
    headers: dict[str, str]


def extract_json_text(content: str) -> str:
    """Return only a top-level JSON ``text`` string; reject every other shape."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidSpeechContent(
            'Speech is available only for valid JSON responses with a non-empty "text" field.'
        ) from exc
    if not isinstance(payload, dict) or isinstance(payload, bool):
        raise InvalidSpeechContent(
            'Speech is available only for JSON objects with a non-empty "text" field.'
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidSpeechContent(
            'Speech is available only when the JSON response has a non-empty string "text" field.'
        )
    return text.strip()


def synthesize_json_text(content: str) -> SpeechAudio:
    """Extract the allowed JSON field and request WAV audio from Kokoro."""
    text = extract_json_text(content)
    url = settings.kokoro_base_url.rstrip("/") + "/v1/audio/speech"
    try:
        response = httpx.post(
            url,
            json={
                "model": "kokoro",
                "input": text,
                "voice": settings.kokoro_voice,
                "speed": settings.kokoro_speed,
                "response_format": "wav",
                "language": settings.kokoro_language,
            },
            timeout=settings.kokoro_timeout,
        )
    except httpx.RequestError as exc:
        raise SpeechServiceUnavailable(
            f"Kokoro is unavailable at {settings.kokoro_base_url}."
        ) from exc

    if response.status_code >= 400:
        try:
            detail = response.json().get("error")
        except (ValueError, AttributeError):
            detail = None
        suffix = f": {detail}" if isinstance(detail, str) and detail else ""
        raise SpeechSynthesisError(f"Kokoro returned HTTP {response.status_code}{suffix}")
    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "audio/wav":
        raise SpeechSynthesisError("Kokoro returned a response that was not WAV audio.")

    forwarded_headers = {
        name: value
        for name in ("X-Audio-Duration", "X-Render-Seconds", "X-Real-Time-Factor")
        if (value := response.headers.get(name)) is not None
    }
    return SpeechAudio(response.content, forwarded_headers)

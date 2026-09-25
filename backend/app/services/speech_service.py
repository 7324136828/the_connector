"""Text and JSON-response speech adapter for the isolated Kokoro process."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings


class InvalidSpeechContent(ValueError):
    """The model response does not contain speakable text."""


class SpeechServiceUnavailable(RuntimeError):
    """The configured Kokoro process could not be reached."""


class SpeechSynthesisError(RuntimeError):
    """Kokoro rejected the request or returned an invalid response."""


@dataclass(frozen=True)
class SpeechAudio:
    content: bytes
    headers: dict[str, str]


def extract_speech_text(content: str) -> str:
    """Speak plain text as-is, or only ``text`` from a JSON object."""
    if not isinstance(content, str) or not content.strip():
        raise InvalidSpeechContent("Speech requires a non-empty model response.")
    candidate = content.strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate
    if not isinstance(payload, dict) or isinstance(payload, bool):
        raise InvalidSpeechContent(
            'A valid JSON response must be an object with a non-empty string "text" field.'
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidSpeechContent(
            'Speech is available only when the JSON response has a non-empty string "text" field.'
        )
    return text.strip()


def synthesize_text(content: str) -> SpeechAudio:
    """Resolve speakable response text and request WAV audio from Kokoro."""
    text = extract_speech_text(content)
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


def health() -> dict[str, Any]:
    """Return verified health data from the required Kokoro backend."""
    url = settings.kokoro_base_url.rstrip("/") + "/health"
    try:
        response = httpx.get(url, timeout=min(settings.kokoro_timeout, 5.0))
    except httpx.RequestError as exc:
        raise SpeechServiceUnavailable(
            f"Kokoro is unavailable at {settings.kokoro_base_url}."
        ) from exc
    if response.status_code >= 400:
        raise SpeechSynthesisError(f"Kokoro health check returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpeechSynthesisError("Kokoro health check did not return JSON.") from exc
    if not isinstance(payload, dict) or payload.get("service") != "python-kokoro" or payload.get("status") != "ok":
        raise SpeechSynthesisError("The configured speech backend is not a healthy python-kokoro service.")
    return payload

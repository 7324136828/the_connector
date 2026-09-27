"""Text and JSON-response speech adapter for the isolated Kokoro process."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings

DEFAULT_ACTOR = "af_heart"


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


def extract_speech_request(content: str) -> tuple[str, str | None]:
    """Read speech text and optional narrator from a model response."""
    if not isinstance(content, str) or not content.strip():
        raise InvalidSpeechContent("Speech requires a non-empty model response.")
    candidate = content.strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate, None
    if not isinstance(payload, dict) or isinstance(payload, bool):
        raise InvalidSpeechContent(
            'A valid JSON response must be an object with a non-empty string "text" field.'
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidSpeechContent(
            'Speech is available only when the JSON response has a non-empty string "text" field.'
        )
    actor = payload.get("actor")
    if actor is not None and (not isinstance(actor, str) or not actor.strip()):
        raise InvalidSpeechContent('The JSON response "actor" must be a non-empty string.')
    return text.strip(), actor.strip() if actor is not None else None


def extract_speech_text(content: str) -> str:
    """Speak plain text as-is, or only ``text`` from a JSON object."""
    return extract_speech_request(content)[0]


def synthesize_text(content: str, actor: str | None = None) -> SpeechAudio:
    """Resolve speakable response text and request WAV audio from Kokoro."""
    text, response_actor = extract_speech_request(content)
    voice = actor or response_actor or DEFAULT_ACTOR
    url = settings.kokoro_base_url.rstrip("/") + "/v1/audio/speech"
    try:
        response = httpx.post(
            url,
            json={
                "model": "kokoro",
                "input": text,
                "voice": voice,
                "speed": settings.kokoro_speed,
                "response_format": "wav",
                "language": voice[0],
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
        if response.status_code == 400:
            raise InvalidSpeechContent(f"Kokoro rejected the speech request{suffix}")
        raise SpeechSynthesisError(f"Kokoro returned HTTP {response.status_code}{suffix}")
    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "audio/wav":
        raise SpeechSynthesisError("Kokoro returned a response that was not WAV audio.")

    forwarded_headers = {
        name: value
        for name in ("X-Audio-Duration", "X-Render-Seconds", "X-Real-Time-Factor")
        if (value := response.headers.get(name)) is not None
    }
    return SpeechAudio(response.content, forwarded_headers)


def list_voices() -> dict[str, Any]:
    """Return the voices supported by the configured Kokoro process."""
    url = settings.kokoro_base_url.rstrip("/") + "/v1/audio/voices"
    try:
        response = httpx.get(url, timeout=min(settings.kokoro_timeout, 5.0))
    except httpx.RequestError as exc:
        raise SpeechServiceUnavailable(
            f"Kokoro is unavailable at {settings.kokoro_base_url}."
        ) from exc
    if response.status_code >= 400:
        raise SpeechSynthesisError(f"Kokoro voice list returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpeechSynthesisError("Kokoro voice list did not return JSON.") from exc
    if (not isinstance(payload, dict) or not isinstance(payload.get("default"), str)
            or not isinstance(payload.get("voices"), list)
            or not all(isinstance(voice, str) for voice in payload["voices"])):
        raise SpeechSynthesisError("Kokoro voice list has an invalid format.")
    return payload


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

"""Speech synthesis accepts only the JSON text field and proxies WAV audio."""

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services import speech_service


client = TestClient(main.app)


@pytest.mark.parametrize(
    "content",
    [
        "plain model response",
        '```json\n{"text":"fenced"}\n```',
        '"a JSON string is not an object"',
        '[{"text":"arrays are not accepted"}]',
        '{"final_answer":"wrong field"}',
        '{"text":42}',
        '{"text":"   "}',
    ],
)
def test_extract_json_text_rejects_non_speakable_content(content):
    with pytest.raises(speech_service.InvalidSpeechContent):
        speech_service.extract_json_text(content)


def test_speech_endpoint_forwards_only_the_json_text_field(monkeypatch):
    captured = {}

    def kokoro_post(url, *, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return httpx.Response(
            200,
            content=b"RIFF-test-wave",
            headers={
                "Content-Type": "audio/wav",
                "X-Audio-Duration": "1.25",
            },
        )

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post(
        "/api/speech",
        json={"content": '{"text":"  Read this only.  ","thought":"Never speak this."}'},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-audio-duration"] == "1.25"
    assert response.content == b"RIFF-test-wave"
    assert captured["url"] == "http://127.0.0.1:8302/v1/audio/speech"
    assert captured["payload"]["input"] == "Read this only."
    assert "thought" not in captured["payload"]["input"]


def test_speech_endpoint_rejects_plain_text_without_calling_kokoro(monkeypatch):
    called = False

    def kokoro_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={"content": "Do not speak this."})

    assert response.status_code == 422
    assert called is False


def test_speech_endpoint_reports_unavailable_kokoro(monkeypatch):
    def kokoro_post(url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={"content": '{"text":"Hello"}'})

    assert response.status_code == 503
    assert "http://127.0.0.1:8302" in response.json()["detail"]

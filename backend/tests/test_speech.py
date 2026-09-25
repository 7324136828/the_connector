"""Speech synthesis accepts plain text and only the JSON text field."""

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services import speech_service


client = TestClient(main.app)


@pytest.mark.parametrize(
    "content",
    [
        '"a JSON string is not an object"',
        '[{"text":"arrays are not accepted"}]',
        '{"final_answer":"wrong field"}',
        '{"text":42}',
        '{"text":"   "}',
    ],
)
def test_extract_speech_text_rejects_unsupported_json_content(content):
    with pytest.raises(speech_service.InvalidSpeechContent):
        speech_service.extract_speech_text(content)


def test_extract_speech_text_accepts_plain_text_and_only_json_text():
    assert speech_service.extract_speech_text("  Plain model response.  ") == "Plain model response."
    assert speech_service.extract_speech_text(
        '{"text":"Read this.","metadata":"Do not read this."}'
    ) == "Read this."


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


def test_speech_endpoint_forwards_plain_text_to_kokoro(monkeypatch):
    captured = {}

    def kokoro_post(url, *, json, timeout):
        captured["input"] = json["input"]
        return httpx.Response(200, content=b"RIFF-plain", headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={"content": "  Speak this plain response.  "})

    assert response.status_code == 200
    assert response.content == b"RIFF-plain"
    assert captured["input"] == "Speak this plain response."


def test_speech_endpoint_reports_unavailable_kokoro(monkeypatch):
    def kokoro_post(url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={"content": '{"text":"Hello"}'})

    assert response.status_code == 503
    assert "http://127.0.0.1:8302" in response.json()["detail"]


def test_connector_exposes_verified_kokoro_health(monkeypatch):
    def kokoro_get(url, *, timeout):
        assert url == "http://127.0.0.1:8302/health"
        assert timeout == 5.0
        return httpx.Response(200, json={
            "status": "ok",
            "service": "python-kokoro",
            "device": "cpu",
            "modelLoaded": False,
        })

    monkeypatch.setattr(speech_service.httpx, "get", kokoro_get)
    response = client.get("/api/speech/health")

    assert response.status_code == 200
    assert response.json()["service"] == "python-kokoro"
    assert response.json()["device"] == "cpu"


def test_connector_rejects_an_unexpected_health_service(monkeypatch):
    monkeypatch.setattr(
        speech_service.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(200, json={"status": "ok", "service": "other"}),
    )
    response = client.get("/api/speech/health")

    assert response.status_code == 502


def test_main_health_is_unavailable_when_required_kokoro_is_down(monkeypatch):
    def unavailable():
        raise speech_service.SpeechServiceUnavailable("Kokoro is unavailable.")

    monkeypatch.setattr(speech_service, "health", unavailable)
    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["speech"] == {
        "required": True,
        "status": "unavailable",
        "detail": "Kokoro is unavailable.",
    }

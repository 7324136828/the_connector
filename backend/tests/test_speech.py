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
    assert captured["payload"]["voice"] == "af_heart"
    assert "thought" not in captured["payload"]["input"]


def test_speech_endpoint_uses_actor_from_model_json(monkeypatch):
    captured = {}

    def kokoro_post(url, *, json, timeout):
        captured.update(json)
        return httpx.Response(200, content=b"RIFF-actor", headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={
        "content": '{"text":"Read this.","actor":"am_michael"}',
    })

    assert response.status_code == 200
    assert captured["input"] == "Read this."
    assert captured["voice"] == "am_michael"
    assert captured["language"] == "a"


@pytest.mark.parametrize("content,actor,expected_voice", [
    ("Hello", None, "bm_george"),
    ('{"text":"Hello","actor":"am_michael"}', None, "am_michael"),
    ('{"text":"Hello","actor":"am_michael"}', "af_heart", "af_heart"),
])
def test_configured_actor_is_used_only_when_request_has_no_actor(monkeypatch, content, actor, expected_voice):
    captured = {}
    monkeypatch.setattr(speech_service.settings, "kokoro_voice", "bm_george")

    def kokoro_post(url, *, json, timeout):
        captured.update(json)
        return httpx.Response(200, content=b"RIFF-actor", headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={"content": content, "actor": actor})
    assert response.status_code == 200
    assert captured["voice"] == expected_voice
    assert captured["language"] == expected_voice[0]


def test_speech_endpoint_explicit_actor_overrides_model_json(monkeypatch):
    captured = {}

    def kokoro_post(url, *, json, timeout):
        captured.update(json)
        return httpx.Response(200, content=b"RIFF-actor", headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(speech_service.httpx, "post", kokoro_post)
    response = client.post("/api/speech", json={
        "content": '{"text":"Read this.","actor":"af_heart"}',
        "actor": "bm_george",
    })

    assert response.status_code == 200
    assert captured["voice"] == "bm_george"
    assert captured["language"] == "b"


def test_speech_endpoint_rejects_empty_actor():
    response = client.post("/api/speech", json={"content": "Hello", "actor": "  "})
    assert response.status_code == 422


def test_speech_endpoint_rejects_invalid_model_json_actor():
    response = client.post("/api/speech", json={"content": '{"text":"Hello","actor":42}'})
    assert response.status_code == 422


def test_speech_endpoint_reports_unknown_actor_as_validation_error(monkeypatch):
    monkeypatch.setattr(speech_service.httpx, "post", lambda *args, **kwargs:
                        httpx.Response(400, json={"error": "Unknown voice: missing_actor"}))
    response = client.post("/api/speech", json={"content": "Hello", "actor": "missing_actor"})
    assert response.status_code == 422
    assert "Unknown voice: missing_actor" in response.json()["detail"]


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


def test_connector_lists_kokoro_voices(monkeypatch):
    def kokoro_get(url, *, timeout):
        assert url == "http://127.0.0.1:8302/v1/audio/voices"
        return httpx.Response(200, json={
            "default": "af_heart", "voices": ["af_heart", "am_michael"],
        })

    monkeypatch.setattr(speech_service.httpx, "get", kokoro_get)
    response = client.get("/api/speech/voices")
    assert response.status_code == 200
    assert response.json() == {"default": "af_heart", "voices": ["af_heart", "am_michael"]}


def test_connector_voice_list_reports_configured_default(monkeypatch):
    monkeypatch.setattr(speech_service.settings, "kokoro_voice", "am_michael")
    monkeypatch.setattr(speech_service.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(200, json={"default": "af_heart", "voices": ["af_heart", "am_michael"]}))
    response = client.get("/api/speech/voices")
    assert response.status_code == 200
    assert response.json()["default"] == "am_michael"


def test_connector_voice_list_reports_unavailable_kokoro(monkeypatch):
    def kokoro_get(url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(speech_service.httpx, "get", kokoro_get)
    assert client.get("/api/speech/voices").status_code == 503


def test_connector_voice_list_rejects_invalid_upstream_data(monkeypatch):
    monkeypatch.setattr(speech_service.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(200, json={"default": "af_heart", "voices": "am_michael"}))
    assert client.get("/api/speech/voices").status_code == 502


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

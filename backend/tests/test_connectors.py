"""Provider request regressions using fake SDK clients and in-memory HTTP replies."""

import io
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services.connectors import (
    claude_connector,
    gemini_connector,
    ollama_connector,
    openai_connector,
    openrouter_connector,
)
from backend.app.services.connectors.common import capture_token_usage


MESSAGES = [
    {"role": "user", "content": "My favorite number is 42."},
    {"role": "assistant", "content": "I will use that context."},
    {"role": "user", "content": "What number did I mention?"},
]
SYSTEM = "Use the provided conversation context."


@pytest.fixture(autouse=True)
def prevent_network(monkeypatch):
    """A regression must never accidentally use local or paid provider endpoints."""
    def blocked(*args, **kwargs):
        pytest.fail("Connector tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(gemini_connector.urllib.request, "urlopen", blocked)
    monkeypatch.setattr(ollama_connector, "urlopen", blocked)
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")


def completion_client():
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  The number is 42.  "))],
        usage=SimpleNamespace(prompt_tokens=17, completion_tokens=6),
    ))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


def capture_http(monkeypatch, module, data):
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request, json.loads(request.data.decode("utf-8")), timeout))
        return io.BytesIO(json.dumps(data).encode("utf-8"))

    if module is ollama_connector:
        monkeypatch.setattr(module, "urlopen", urlopen)
    else:
        monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize("model,selected,expected", [
    ("gpt-5", None, "minimal"),
    ("gpt-5-mini", "high", "high"),
    ("gpt-5-nano-2025-08-07", "medium", "medium"),
    ("o3-mini", None, "low"),
    ("o4-mini", "low", "low"),
])
def test_openai_chat_uses_selected_effort_and_reasoning_token_limit(model, selected, expected):
    client, create = completion_client()
    with capture_token_usage() as usage:
        reply = openai_connector.openai_chat(
            client, model=model, messages=MESSAGES, system_prompt=SYSTEM,
            max_output_tokens=512, temperature=0.2, effort=selected,
        )
    options = create.call_args.kwargs
    assert options["reasoning_effort"] == expected
    assert options["max_completion_tokens"] == 512
    assert "max_tokens" not in options
    assert "temperature" not in options
    assert options["messages"] == [{"role": "system", "content": SYSTEM}, *MESSAGES]
    assert reply == "The number is 42."
    assert (usage.input_tokens, usage.output_tokens) == (17, 6)


@pytest.mark.parametrize("selected,expected", [(None, "minimal"), ("high", "high")])
def test_openai_responses_fallback_preserves_effort_context_and_token_limit(selected, expected):
    client, create = completion_client()
    create.side_effect = RuntimeError("This model requires the Responses API")
    responses_create = Mock(return_value=SimpleNamespace(
        output_text="  Recalled 42.  ",
        usage=SimpleNamespace(input_tokens=23, output_tokens=4),
    ))
    client.responses = SimpleNamespace(create=responses_create)
    with capture_token_usage() as usage:
        reply = openai_connector.openai_chat(
            client, model="gpt-5-mini", messages=MESSAGES,
            system_prompt=SYSTEM, max_output_tokens=768, effort=selected,
        )
    assert create.call_args.kwargs["reasoning_effort"] == expected
    assert responses_create.call_args.kwargs == {
        "model": "gpt-5-mini", "input": MESSAGES, "instructions": SYSTEM,
        "reasoning": {"effort": expected}, "max_output_tokens": 768,
    }
    assert reply == "Recalled 42."
    assert (usage.input_tokens, usage.output_tokens) == (23, 4)


def test_openai_non_reasoning_model_keeps_temperature_and_standard_limit():
    client, create = completion_client()
    openai_connector.openai_chat(
        client, model="gpt-4o-mini", messages=MESSAGES, temperature=0.2, max_output_tokens=256,
    )
    options = create.call_args.kwargs
    assert options["temperature"] == 0.2
    assert options["max_tokens"] == 256
    assert "max_completion_tokens" not in options
    assert "reasoning_effort" not in options


@pytest.mark.parametrize("model,selected,expected", [
    ("claude-opus-4-5", None, "low"),
    ("claude-sonnet-4-6", "high", "high"),
    ("claude-opus-4-6", "max", "max"),
])
def test_claude_sends_output_config_effort(model, selected, expected):
    create = Mock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="  42.  ")],
        usage=SimpleNamespace(input_tokens=13, output_tokens=2),
    ))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with capture_token_usage() as usage:
        reply = claude_connector.claude_chat(
            client, model=model, messages=MESSAGES, system_prompt=SYSTEM,
            max_output_tokens=1024, effort=selected,
        )
    options = create.call_args.kwargs
    assert options["output_config"] == {"effort": expected}
    assert options["max_tokens"] == 1024
    assert options["system"] == SYSTEM
    assert options["messages"] == MESSAGES
    assert "reasoning_effort" not in options
    assert reply == "42."
    assert (usage.input_tokens, usage.output_tokens) == (13, 2)


GEMINI_EFFORTS = [(None, 0), ("none", 0), ("low", 1024), ("medium", 8192), ("high", 24576)]


@pytest.mark.parametrize("selected,budget", GEMINI_EFFORTS)
def test_gemini_sdk_maps_effort_to_thinking_budget(selected, budget):
    generate = Mock(return_value=SimpleNamespace(
        text="  42.  ", usage_metadata=SimpleNamespace(prompt_token_count=11, candidates_token_count=2),
    ))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    with capture_token_usage() as usage:
        reply = gemini_connector.gemini_chat(
            client, model="gemini-2.5-flash", messages=MESSAGES, system_prompt=SYSTEM,
            max_output_tokens=600, effort=selected,
        )
    options = generate.call_args.kwargs
    assert options["config"]["thinking_config"] == {"thinking_budget": budget}
    assert options["config"]["max_output_tokens"] == 600
    assert options["config"]["system_instruction"] == SYSTEM
    assert [message["role"] for message in options["contents"]] == ["user", "model", "user"]
    assert reply == "42."
    assert (usage.input_tokens, usage.output_tokens) == (11, 2)


@pytest.mark.parametrize("selected,budget", GEMINI_EFFORTS)
def test_gemini_rest_maps_effort_to_camelcase_budget(monkeypatch, selected, budget):
    calls = capture_http(monkeypatch, gemini_connector, {
        "candidates": [{"content": {"parts": [{"text": "  42.  "}]}}],
        "usageMetadata": {"promptTokenCount": 19, "candidatesTokenCount": 2},
    })
    with capture_token_usage() as usage:
        reply = gemini_connector.gemini_chat(
            None, model="gemini-2.5-flash", messages=MESSAGES, system_prompt=SYSTEM,
            max_output_tokens=600, effort=selected, timeout=12,
        )
    assert len(calls) == 1
    request, payload, timeout = calls[0]
    assert request.get_method() == "POST"
    assert "/models/gemini-2.5-flash:generateContent" in request.full_url
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": budget}
    assert payload["generationConfig"]["maxOutputTokens"] == 600
    assert payload["systemInstruction"] == {"parts": [{"text": SYSTEM}]}
    assert [message["role"] for message in payload["contents"]] == ["user", "model", "user"]
    assert timeout == 12
    assert reply == "42."
    assert (usage.input_tokens, usage.output_tokens) == (19, 2)


def test_gemini_sdk_failure_keeps_selected_effort_in_rest_fallback(monkeypatch):
    generate = Mock(side_effect=TypeError("Older SDK cannot parse thinking_config"))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    calls = capture_http(monkeypatch, gemini_connector, {
        "candidates": [{"content": {"parts": [{"text": "Fallback answer."}]}}],
    })
    reply = gemini_connector.gemini_chat(
        client, model="gemini-2.5-pro", messages=MESSAGES, system_prompt=SYSTEM,
        effort="medium", max_output_tokens=900,
    )
    assert generate.call_args.kwargs["config"]["thinking_config"] == {"thinking_budget": 8192}
    assert len(calls) == 1
    assert calls[0][1]["generationConfig"] == {
        "temperature": 0.7, "maxOutputTokens": 900, "thinkingConfig": {"thinkingBudget": 8192},
    }
    assert reply == "Fallback answer."


@pytest.mark.parametrize("selected,expected", [(None, "minimal"), ("high", "high")])
def test_openrouter_sdk_uses_extra_body_for_reasoning(selected, expected):
    client, create = completion_client()
    reply = openrouter_connector.openrouter_chat(
        client, model="openai/gpt-5-mini", messages=MESSAGES, system_prompt=SYSTEM,
        effort=selected, max_output_tokens=300,
    )
    options = create.call_args.kwargs
    assert options["extra_body"] == {"reasoning": {"effort": expected}}
    assert "reasoning" not in options
    assert "reasoning_effort" not in options
    assert "temperature" not in options
    assert options["max_tokens"] == 300
    assert options["messages"] == [{"role": "system", "content": SYSTEM}, *MESSAGES]
    assert reply == "The number is 42."


@pytest.mark.parametrize("selected,expected", [(None, "low"), ("high", "high")])
def test_openrouter_rest_uses_reasoning_payload(monkeypatch, selected, expected):
    calls = capture_http(monkeypatch, openrouter_connector, {
        "choices": [{"message": {"content": "  42.  "}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    })
    with capture_token_usage() as usage:
        reply = openrouter_connector.openrouter_chat(
            None, model="anthropic/claude-sonnet-4-6", messages=MESSAGES, system_prompt=SYSTEM,
            effort=selected, max_output_tokens=300, timeout=15,
        )
    assert len(calls) == 1
    request, payload, timeout = calls[0]
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer unit-test-openrouter-key"
    assert payload["reasoning"] == {"effort": expected}
    assert "extra_body" not in payload
    assert "temperature" not in payload
    assert payload["messages"] == [{"role": "system", "content": SYSTEM}, *MESSAGES]
    assert payload["max_tokens"] == 300
    assert timeout == 15
    assert reply == "42."
    assert (usage.input_tokens, usage.output_tokens) == (12, 2)


@pytest.mark.parametrize("selected,expected", [(None, "low"), ("medium", "medium"), ("high", "high")])
def test_ollama_uses_top_level_think_for_supported_model(monkeypatch, selected, expected):
    calls = capture_http(monkeypatch, ollama_connector, {
        "message": {"content": "  42.  "}, "prompt_eval_count": 21, "eval_count": 2,
    })
    with capture_token_usage() as usage:
        reply = ollama_connector.ollama_chat(
            "http://localhost:11434/api", model="gpt-oss:20b", messages=MESSAGES,
            system_prompt=SYSTEM, effort=selected, max_output_tokens=512,
            context_window=8192, timeout=20,
        )
    assert len(calls) == 1
    request, payload, timeout = calls[0]
    assert request.full_url == "http://localhost:11434/api/chat"
    assert payload["think"] == expected
    assert "think" not in payload["options"]
    assert payload["options"]["num_predict"] == 512
    assert payload["options"]["num_ctx"] == 8192
    assert payload["messages"] == [{"role": "system", "content": SYSTEM}, *MESSAGES]
    assert payload["stream"] is False
    assert timeout == 20
    assert reply == "42."
    assert (usage.input_tokens, usage.output_tokens) == (21, 2)


@pytest.mark.parametrize("connector,model,selected", [
    (openai_connector.openai_chat, "gpt-4o-mini", "high"),
    (openai_connector.openai_chat, "gpt-5", "easy"),
    (claude_connector.claude_chat, "claude-3-5-haiku-20241022", "low"),
    (claude_connector.claude_chat, "claude-sonnet-4-6", "max"),
    (gemini_connector.gemini_chat, "gemini-2.0-flash", "high"),
    (gemini_connector.gemini_chat, "gemini-2.5-pro", "none"),
    (openrouter_connector.openrouter_chat, "openai/gpt-4o-mini", "high"),
    (openrouter_connector.openrouter_chat, "openai/gpt-5-mini", "max"),
    (ollama_connector.ollama_chat, "llama3.2", "high"),
    (ollama_connector.ollama_chat, "gpt-oss:20b", "none"),
])
def test_unsupported_effort_is_rejected_before_any_provider_call(connector, model, selected):
    with pytest.raises(ValueError, match="Unsupported effort"):
        connector(None, model=model, messages=MESSAGES, effort=selected)

"""Structured-completion transport contracts; all provider calls are simulated."""

from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest

from backend.app.services.completion_service import CompletionService, UnsupportedCompletionOption
from backend.app.services.connectors import ChatAPIError


module = importlib.import_module("backend.app.services.completion_service")
TOOL = {"type": "function", "function": {"name": "echo", "description": "Return supplied text",
        "parameters": {"type": "object", "properties": {"text": {"type": "string", "default": "fixture"}}, "required": ["text"]}}}
CALL = {"id": "call_one", "type": "function", "function": {"name": "echo", "arguments": '{"text":"hello"}'}}
MESSAGES = [
    {"role": "system", "content": "Client system instruction"},
    {"role": "developer", "content": "Client developer instruction"},
    {"role": "user", "content": "Echo hello"},
    {"role": "assistant", "content": None, "tool_calls": [CALL]},
    {"role": "tool", "tool_call_id": "call_one", "content": "hello"},
]


def config(provider="mock", model="mock-assistant", **route):
    return {"system_prompt": "Configuration instruction", "sequences": [{"provider": provider, "model": model, "retries": 0, **route}]}


def completion(message=None, **extra):
    return {"model": "actual-model", "choices": [{"message": message or {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, **extra}


def install_openai(service, provider, handler):
    service._clients[provider] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=handler)))


def test_openai_preserves_tool_round_trip_instructions_and_reasoning():
    service, captured = CompletionService(), []
    reply = {"role": "assistant", "content": None, "tool_calls": [CALL], "reasoning_content": "provider summary",
             "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]}

    def create(**request):
        captured.append(request)
        result = completion(reply)
        result["choices"][0]["finish_reason"] = "tool_calls"
        return result

    install_openai(service, "openai", create)
    original = deepcopy(MESSAGES)
    options = {"tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": False,
               "temperature": 0.4, "max_tokens": 256, "stop": ["END"], "seed": 8,
               "response_format": {"type": "json_object"}}
    result = service.complete(MESSAGES, config("openai", "gpt-4o-mini"), options)
    request = captured[0]
    assert request["messages"] == [{"role": "system", "content": "Configuration instruction"}] + original
    assert MESSAGES == original
    assert request["tools"] == [TOOL]
    assert all(request[key] == value for key, value in options.items())
    assert request["stream"] is False
    assert result["message"] == reply
    assert result["finish_reason"] == "tool_calls"
    assert result["model"] == "actual-model"
    assert result["provider"] == "openai"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_configured_effort_wins_and_reasoning_token_limit_is_translated():
    service, captured = CompletionService(), []
    install_openai(service, "openai", lambda **request: captured.append(request) or completion())
    service.complete([{"role": "user", "content": "Think"}], config("openai", "gpt-5-nano", effort="low"),
                     {"reasoning_effort": "high", "temperature": 1, "max_tokens": 123})
    assert captured[0]["reasoning_effort"] == "low"
    assert captured[0]["max_completion_tokens"] == 123
    assert "max_tokens" not in captured[0] and "temperature" not in captured[0]


def test_openrouter_effort_and_provider_extra_fields_survive():
    service, captured = CompletionService(), []
    reply = {"role": "assistant", "content": "Ready", "reasoning_content": "Summary"}
    install_openai(service, "openrouter", lambda **request: captured.append(request) or completion(reply))
    result = service.complete([{"role": "user", "content": "Go"}],
                              config("openrouter", "anthropic/claude-opus-4.6", effort="max"), {"reasoning_effort": "low", "temperature": 0.3})
    assert captured[0]["extra_body"] == {"reasoning": {"effort": "max"}}
    assert captured[0]["temperature"] == 0.3
    assert "reasoning_effort" not in captured[0]
    assert result["message"]["reasoning_content"] == "Summary"


def test_zero_usage_and_nullable_content_are_not_replaced_by_estimates():
    service = CompletionService()
    install_openai(service, "openai", lambda **kwargs: completion(
        {"role": "assistant", "content": None, "refusal": "Declined"},
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    result = service.complete([{"role": "user", "content": "Go"}], config("openai", "gpt-4o-mini"), {})
    assert result["message"]["content"] is None
    assert result["message"]["refusal"] == "Declined"
    assert result["usage"]["total_tokens"] == 0


def test_claude_translates_tools_parallel_results_and_signed_thinking():
    service, captured = CompletionService(), []
    blocks = [{"type": "thinking", "thinking": "Summary", "signature": "signature"},
              {"type": "tool_use", "id": "call_native", "name": "echo", "input": {"text": "hi"}}]

    def create(**request):
        captured.append(request)
        return {"model": "claude-model", "content": blocks, "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 7, "output_tokens": 3}}

    service._clients["claude"] = SimpleNamespace(messages=SimpleNamespace(create=create))
    options = {"tools": [TOOL], "tool_choice": {"type": "function", "function": {"name": "echo"}},
               "parallel_tool_calls": False, "max_completion_tokens": 99, "stop": "END"}
    cfg = config("claude", "claude-opus-4-6", effort="high")
    result = service.complete(MESSAGES, cfg, options)
    request = captured[0]
    assert request["system"] == "Configuration instruction\n\nClient system instruction\n\nClient developer instruction"
    assert request["messages"][-2]["content"] == [{"type": "tool_use", "id": "call_one", "name": "echo", "input": {"text": "hello"}}]
    assert request["messages"][-1]["content"] == [{"type": "tool_result", "tool_use_id": "call_one", "content": "hello"}]
    assert request["tool_choice"] == {"type": "tool", "name": "echo", "disable_parallel_tool_use": True}
    assert request["tools"][0]["input_schema"] == TOOL["function"]["parameters"]
    assert request["output_config"] == {"effort": "high"}
    assert request["max_tokens"] == 99 and request["stop_sequences"] == ["END"]
    assert result["message"]["tool_calls"][0]["id"] == "call_native"
    assert result["message"]["reasoning_content"] == "Summary"
    assert result["usage"]["prompt_tokens"] == 12
    assert result["finish_reason"] == "tool_calls"
    replay = MESSAGES + [result["message"], {"role": "tool", "tool_call_id": "call_native", "content": "hi"}]
    service.complete(replay, cfg, {"tools": [TOOL]})
    assert captured[1]["messages"][-2]["content"] == blocks


def test_claude_groups_parallel_tool_results_before_user_text():
    service, captured = CompletionService(), []
    service._clients["claude"] = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: captured.append(kw) or
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    second = {**CALL, "id": "call_two"}
    messages = [{"role": "user", "content": "Run both"}, {"role": "assistant", "content": None, "tool_calls": [CALL, second]},
                {"role": "tool", "tool_call_id": "call_one", "content": "one"},
                {"role": "tool", "tool_call_id": "call_two", "content": "two"},
                {"role": "user", "content": "Summarize"}]
    service.complete(messages, config("claude", "claude-opus-4-6"), {})
    assert [part["type"] for part in captured[0]["messages"][-1]["content"]] == ["tool_result", "tool_result", "text"]


def test_gemini_preserves_native_function_ids_and_thought_signatures(monkeypatch):
    service, captured = CompletionService(), []
    monkeypatch.setattr(module, "get_gemini_api_key", lambda: "fixture-key")
    parts = [{"text": "Summary", "thought": True},
             {"functionCall": {"name": "echo", "args": {"text": "hello"}, "id": "native_id"}, "thoughtSignature": "opaque-signature"}]

    def post(url, payload, headers=None):
        captured.append((url, deepcopy(payload), headers))
        return {"modelVersion": "gemini-response-model", "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3, "totalTokenCount": 18}}

    monkeypatch.setattr(service, "_post_json", post)
    cfg = config("gemini", "gemini-2.5-pro", effort="high")
    result = service.complete([{"role": "user", "content": "Echo hello"}], cfg,
                              {"tools": [TOOL], "tool_choice": "required", "max_tokens": 100, "seed": 1})
    assert result["message"]["content"] is None
    assert result["message"]["reasoning_content"] == "Summary"
    assert result["message"]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "opaque-signature"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
    request = captured[0][1]
    assert request["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert request["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 24576}
    assert request["generationConfig"]["maxOutputTokens"] == 100
    assert captured[0][2] == {"x-goog-api-key": "fixture-key"}
    assert "fixture-key" not in captured[0][0]
    replay = [{"role": "user", "content": "Echo hello"}, result["message"],
              {"role": "tool", "tool_call_id": "native_id", "content": '{"text":"hello"}'}]
    service.complete(replay, cfg, {"tools": [TOOL]})
    assert captured[1][1]["contents"][-2]["parts"] == parts
    assert captured[1][1]["contents"][-1]["parts"] == [{"functionResponse": {"name": "echo", "id": "native_id", "response": {"text": "hello"}}}]


def test_ollama_translates_call_arguments_tool_names_and_controls(monkeypatch):
    service, captured = CompletionService(), []

    def post(url, payload, headers=None):
        captured.append(deepcopy(payload))
        return {"model": "gpt-oss:20b", "message": {"content": "", "thinking": "Summary", "tool_calls": [
                    {"function": {"name": "echo", "arguments": {"text": "next"}}}]},
                "prompt_eval_count": 10, "eval_count": 2, "done_reason": "stop"}

    monkeypatch.setattr(service, "_post_json", post)
    result = service.complete(MESSAGES, config("ollama", "gpt-oss:20b", effort="high"),
                              {"tools": [TOOL], "max_completion_tokens": 128, "temperature": 0.2, "seed": 7, "stop": "END"})
    assert captured[0]["messages"][-1] == {"role": "tool", "content": "hello", "tool_name": "echo"}
    assert captured[0]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"text": "hello"}
    assert captured[0]["think"] == "high"
    assert captured[0]["options"] == {"num_predict": 128, "temperature": 0.2, "seed": 7, "stop": ["END"]}
    assert result["message"]["reasoning_content"] == "Summary"
    assert json.loads(result["message"]["tool_calls"][0]["function"]["arguments"]) == {"text": "next"}
    assert result["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("provider,model,options", [
    ("claude", "claude-opus-4-6", {"seed": 1}),
    ("claude", "claude-opus-4-6", {"response_format": {"type": "json_object"}}),
    ("gemini", "gemini-2.5-pro", {"tools": [TOOL], "parallel_tool_calls": False}),
    ("ollama", "gpt-oss:20b", {"tools": [TOOL], "tool_choice": "required"}),
    ("openai", "gpt-5-nano", {"temperature": 0.2}),
])
def test_unsupported_controls_raise_explicit_error_before_provider_call(provider, model, options):
    with pytest.raises(UnsupportedCompletionOption):
        CompletionService().complete([{"role": "user", "content": "go"}], config(provider, model), options)


@pytest.mark.parametrize("options", [
    {"unknown": 1}, {"max_tokens": 1, "max_completion_tokens": 2}, {"max_tokens": 0},
    {"tools": [{"type": "web_search"}]}, {"tool_choice": "required"},
    {"tools": [TOOL], "tool_choice": {"type": "function", "function": {"name": "absent"}}},
    {"response_format": {"type": "json_schema"}},
    {"response_format": {"type": "json_schema", "json_schema": {"name": "missing_schema"}}},
    {"response_format": []}, {"temperature": float("nan")}, {"stop": {"invalid": "object"}},
])
def test_invalid_completion_options_are_rejected(options):
    with pytest.raises(UnsupportedCompletionOption):
        CompletionService().complete([{"role": "user", "content": "go"}], config(), options)


def test_unknown_tool_result_id_is_rejected():
    with pytest.raises(UnsupportedCompletionOption, match="tool_call_id"):
        CompletionService().complete([{"role": "tool", "tool_call_id": "absent", "content": "go"}], config(), {})


def test_only_configured_routes_are_attempted_with_retries(monkeypatch):
    service, calls = CompletionService(), []
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    def execute(route, messages, options):
        calls.append(route["model"])
        if route["model"] == "first":
            raise RuntimeError("Simulated outage")
        return {"message": {"role": "assistant", "content": "fallback"}, "finish_reason": "stop"}

    monkeypatch.setattr(service, "_execute", execute)
    cfg = config("openai", "first", retries=1)
    cfg["sequences"].append({"provider": "openai", "model": "second", "retries": 0})
    result = service.complete([{"role": "user", "content": "go"}], cfg, {})
    assert calls == ["first", "first", "second"]
    assert result["provider"] == "openai" and result["model"] == "second"
    assert result["usage"]["total_tokens"] > 0


def test_probability_selected_route_then_configured_fallback(monkeypatch):
    service, calls = CompletionService(), []
    monkeypatch.setattr(module.random, "choices", lambda *args, **kwargs: [1])

    def execute(route, messages, options):
        calls.append(route["model"])
        if route["model"] == "chosen":
            raise RuntimeError("Selected provider unavailable")
        return {"message": {"role": "assistant", "content": "fallback"}, "finish_reason": "stop"}

    monkeypatch.setattr(service, "_execute", execute)
    cfg = {"sequences": [{"type": "probability", "retries": 0, "choices": [
        {"provider": "mock", "model": "fallback", "probability": 1}, {"provider": "mock", "model": "chosen", "probability": 9}]}]}
    result = service.complete([{"role": "user", "content": "go"}], cfg, {})
    assert calls == ["chosen", "fallback"]
    assert result["model"] == "fallback"


def test_no_implicit_mock_on_exhausted_routes(monkeypatch):
    service = CompletionService()
    monkeypatch.setattr(service, "_execute", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture outage")))
    with pytest.raises(ChatAPIError, match="All configured completion routes failed"):
        service.complete([{"role": "user", "content": "go"}], config("openai", "gpt-4o-mini"), {})


def test_empty_provider_message_uses_next_configured_fallback(monkeypatch):
    service, calls = CompletionService(), []

    def execute(route, messages, options):
        calls.append(route["model"])
        return {"message": {"role": "assistant", "content": None if route["model"] == "empty" else "Ready"}, "finish_reason": "stop"}

    monkeypatch.setattr(service, "_execute", execute)
    cfg = config("mock", "empty")
    cfg["sequences"].append({"provider": "mock", "model": "fallback", "retries": 0})
    result = service.complete([{"role": "user", "content": "go"}], cfg, {})
    assert calls == ["empty", "fallback"]
    assert result["message"]["content"] == "Ready"


@pytest.mark.parametrize("message,reason", [
    ({"role": "assistant", "content": ""}, "stop"),
    ({"role": "assistant", "content": None, "refusal": "Declined"}, "stop"),
    ({"role": "assistant", "content": None}, "content_filter"),
])
def test_empty_text_refusal_and_content_filter_are_valid(monkeypatch, message, reason):
    service = CompletionService()
    monkeypatch.setattr(service, "_execute", lambda *args: {"message": message, "finish_reason": reason})
    result = service.complete([{"role": "user", "content": "go"}], config(), {})
    assert result["message"] == message
    assert result["finish_reason"] == reason


def test_unsupported_native_route_can_fall_back_to_explicit_compatible_route():
    cfg = config("ollama", "gpt-oss:20b")
    cfg["sequences"].append({"provider": "mock", "model": "mock-assistant", "retries": 0})
    result = CompletionService().complete([{"role": "user", "content": "go"}], cfg,
                                          {"tools": [TOOL], "tool_choice": "required"})
    assert result["provider"] == "mock"
    assert result["finish_reason"] == "tool_calls"


def test_mock_deterministic_tool_request_and_result_do_not_execute_any_tool():
    service = CompletionService()
    messages = [{"role": "user", "content": "Echo fixture"}]
    first = service.complete(messages, config(), {"tools": [TOOL]})
    assert service.complete(messages, config(), {"tools": [TOOL]}) == first
    assert first["message"]["content"] is None
    call = first["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"text": "fixture"}
    replay = messages + [first["message"], {"role": "tool", "tool_call_id": call["id"], "content": "provided by external caller"}]
    second = service.complete(replay, config(), {"tools": [TOOL]})
    assert second["finish_reason"] == "stop"
    assert second["message"]["content"] == "Mock tool result received: provided by external caller"


def test_mock_json_stop_and_token_limit_options():
    service = CompletionService()
    messages = [{"role": "user", "content": "hello END leftover"}]
    structured = service.complete(messages, config(), {"response_format": {"type": "json_object"}})
    assert json.loads(structured["message"]["content"])["mock"] is True
    stopped = service.complete(messages, config(), {"stop": " END"})
    assert stopped["message"]["content"] == "Mock completion: hello"
    limited = service.complete(messages, config(), {"max_tokens": 1})
    assert limited["finish_reason"] == "length"
    assert limited["message"]["content"] == "Mock"

"""Structured, stateless completions for external clients that execute their own tools.

Unlike the text-only chat router, this transport preserves assistant tool calls,
tool results, and provider reasoning metadata. It never executes a tool locally.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import random
import time
import uuid
from typing import Any

import httpx

from ..config import settings
from ..audit_context import record_route_attempt
from ..model_capabilities import gemini_thinking_config
from ..schemas.configuration import normalize_config
from .connectors import ChatAPIError, create_claude_client, create_openai_client, create_openrouter_client, estimate_tokens
from .connectors.gemini_connector import get_gemini_api_key
from .connectors.ollama_connector import ollama_base_url


class UnsupportedCompletionOption(ValueError):
    """The supplied request cannot be represented faithfully by a transport."""


OPTION_NAMES = {
    "tools", "tool_choice", "parallel_tool_calls", "temperature", "max_tokens",
    "max_completion_tokens", "stop", "response_format", "seed", "reasoning_effort",
    "top_p", "frequency_penalty", "presence_penalty",
}


def _dump(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_dump(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    if hasattr(value, "__dict__"):
        return _dump(vars(value))
    return value


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in content
    ):
        return "\n".join(part["text"] for part in content)
    raise UnsupportedCompletionOption("This native transport supports text content and function tools only.")


def _arguments(call: dict) -> dict:
    try:
        value = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedCompletionOption("Tool-call arguments must be a JSON object string.") from exc
    if not isinstance(value, dict):
        raise UnsupportedCompletionOption("Tool-call arguments must decode to an object.")
    return value


def _tool_call(name: str, arguments: dict, call_id: str | None = None) -> dict:
    return {"id": call_id or "call_" + uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _usage(raw: dict | None, messages: list[dict], message: dict) -> dict:
    raw = raw or {}
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if type(prompt) is not int or prompt < 0:
        prompt = estimate_tokens(json.dumps(messages, ensure_ascii=False))
    if type(completion) is not int or completion < 0:
        completion = estimate_tokens(json.dumps(message, ensure_ascii=False))
    result = {"prompt_tokens": prompt, "completion_tokens": completion,
              "total_tokens": raw.get("total_tokens") if type(raw.get("total_tokens")) is int else prompt + completion}
    for field in ("prompt_tokens_details", "completion_tokens_details"):
        if raw.get(field) is not None:
            result[field] = raw[field]
    return result


def _validate(messages: list[dict], options: dict) -> dict:
    if not isinstance(messages, list) or not messages:
        raise UnsupportedCompletionOption("messages must be a nonempty list.")
    known_calls = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise UnsupportedCompletionOption("Messages must have a supported chat role.")
        content = message.get("content")
        if not isinstance(content, (str, list)) and not (content is None and message["role"] == "assistant"):
            raise UnsupportedCompletionOption("Message content must be text or content parts; only assistant content may be null.")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or (calls and message["role"] != "assistant"):
            raise UnsupportedCompletionOption("Only assistant messages may contain tool_calls.")
        for call in calls:
            if (not isinstance(call, dict) or call.get("type") != "function"
                    or not isinstance(call.get("id"), str) or not call["id"]
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)):
                raise UnsupportedCompletionOption("tool_calls must contain function calls with IDs, names, and JSON argument strings.")
            known_calls.add(call["id"])
        if message["role"] == "tool" and message.get("tool_call_id") not in known_calls:
            raise UnsupportedCompletionOption("Each tool result must reference an earlier assistant tool_call_id.")
    if not isinstance(options, dict):
        raise UnsupportedCompletionOption("Completion options must be an object.")
    unknown = options.keys() - OPTION_NAMES
    if unknown:
        raise UnsupportedCompletionOption("Unsupported completion option(s): " + ", ".join(sorted(unknown)))
    options = deepcopy({key: value for key, value in options.items() if value is not None})
    for name in ("max_tokens", "max_completion_tokens"):
        if name in options and (type(options[name]) is not int or options[name] < 1):
            raise UnsupportedCompletionOption(f"{name} must be a positive integer.")
    if "max_tokens" in options and "max_completion_tokens" in options:
        raise UnsupportedCompletionOption("Specify either max_tokens or max_completion_tokens, not both.")
    if "parallel_tool_calls" in options and type(options["parallel_tool_calls"]) is not bool:
        raise UnsupportedCompletionOption("parallel_tool_calls must be a boolean.")
    for name, minimum, maximum in (("temperature", 0, 2), ("top_p", 0, 1), ("frequency_penalty", -2, 2), ("presence_penalty", -2, 2)):
        if name in options and (type(options[name]) not in (int, float) or not math.isfinite(options[name]) or not minimum <= options[name] <= maximum):
            raise UnsupportedCompletionOption(f"{name} must be a finite number between {minimum} and {maximum}.")
    if "seed" in options and type(options["seed"]) is not int:
        raise UnsupportedCompletionOption("seed must be an integer.")
    if "reasoning_effort" in options and not isinstance(options["reasoning_effort"], str):
        raise UnsupportedCompletionOption("reasoning_effort must be a string; configured route effort takes precedence.")
    if "stop" in options:
        stops = [options["stop"]] if isinstance(options["stop"], str) else options["stop"]
        if not isinstance(stops, list) or not 1 <= len(stops) <= 4 or not all(isinstance(stop, str) and stop for stop in stops):
            raise UnsupportedCompletionOption("stop must be a nonempty string or one to four nonempty strings.")
    if "response_format" in options:
        output_format = options["response_format"]
        if not isinstance(output_format, dict) or output_format.get("type") not in {"text", "json_object", "json_schema"}:
            raise UnsupportedCompletionOption("response_format must be a text, json_object, or json_schema object.")
        if output_format["type"] == "json_schema":
            definition = output_format.get("json_schema")
            if not isinstance(definition, dict) or not isinstance(definition.get("schema"), dict):
                raise UnsupportedCompletionOption("response_format.json_schema.schema must be a JSON schema object.")
    tools = options.get("tools", [])
    if not isinstance(tools, list):
        raise UnsupportedCompletionOption("tools must be an array of function definitions.")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise UnsupportedCompletionOption("Only function tools are supported by this gateway.")
        definition = tool["function"]
        if not isinstance(definition.get("name"), str) or not definition["name"]:
            raise UnsupportedCompletionOption("Each function tool needs a nonempty name.")
        if definition["name"] in names:
            raise UnsupportedCompletionOption("Function tool names must be unique.")
        if "parameters" in definition and not isinstance(definition["parameters"], dict):
            raise UnsupportedCompletionOption("Function parameters must be a JSON schema object.")
        names.add(definition["name"])
    choice = options.get("tool_choice", "auto" if tools else "none")
    if isinstance(choice, dict):
        if choice.get("type") != "function" or not isinstance(choice.get("function"), dict) or choice["function"].get("name") not in names:
            raise UnsupportedCompletionOption("tool_choice must name one of the supplied function tools.")
    elif choice not in ("auto", "none", "required"):
        raise UnsupportedCompletionOption("tool_choice must be auto, none, required, or a named function.")
    if choice == "required" and not tools:
        raise UnsupportedCompletionOption("tool_choice=required needs at least one tool.")
    return options


def _validate_response(response: dict) -> None:
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ChatAPIError("Provider returned no structured assistant message.")
    if message.get("content") is None and not (
        message.get("tool_calls") or message.get("refusal") or response.get("finish_reason") == "content_filter"
    ):
        raise ChatAPIError("Provider returned no assistant content, tool calls, or refusal.")


def _native_options(options: dict, provider: str, unsupported: set[str]) -> None:
    rejected = options.keys() & unsupported
    if rejected:
        raise UnsupportedCompletionOption(f"{provider} cannot represent: {', '.join(sorted(rejected))}.")


def _append_content(messages: list[dict], role: str, parts: list[dict]) -> None:
    if not parts:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(parts)
    else:
        messages.append({"role": role, "content": parts})


class CompletionService:
    def __init__(self):
        self._clients: dict[str, Any] = {}

    def complete(self, messages: list[dict], config: dict, options: dict) -> dict:
        """Return a structured assistant turn using only explicitly configured routes."""
        options = _validate(messages, options)
        config = normalize_config(config)
        supplied_messages = deepcopy(messages)
        if config["system_prompt"]:
            supplied_messages.insert(0, {"role": "system", "content": config["system_prompt"]})
        errors, unsupported = [], []
        for step in config["sequences"]:
            if step.get("type") == "probability":
                choices = step["choices"]
                index = random.choices(range(len(choices)), weights=[c["probability"] for c in choices], k=1)[0]
                routes = [choices[index]] + [route for i, route in enumerate(choices) if i != index]
            else:
                routes = [step]
            for route in routes:
                for attempt in range(route["retries"] + 1):
                    attempt_started = time.perf_counter()
                    routing_type = "probability" if step.get("type") == "probability" else "sequence_step"
                    try:
                        response = self._execute(route, supplied_messages, options)
                        _validate_response(response)
                        returned_model = response.get("model")
                        response.update(provider=route["provider"], model=response.get("model") or route["model"])
                        response["usage"] = _usage(response.get("usage"), supplied_messages, response["message"])
                        record_route_attempt(
                            route["provider"], route["model"], route.get("effort"), "success",
                            attempt=attempt + 1, elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            returned_model=returned_model, routing_type=routing_type,
                        )
                        return response
                    except UnsupportedCompletionOption as exc:
                        record_route_attempt(
                            route["provider"], route["model"], route.get("effort"), "unsupported",
                            attempt=attempt + 1, elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            error_type=type(exc).__name__, routing_type=routing_type,
                        )
                        unsupported.append(f"{route['provider']}/{route['model']}: {exc}")
                        break
                    except Exception as exc:
                        record_route_attempt(
                            route["provider"], route["model"], route.get("effort"), "failed",
                            attempt=attempt + 1, elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            error_type=type(exc).__name__, routing_type=routing_type,
                        )
                        errors.append(f"{route['provider']}/{route['model']}: {exc}")
                        if attempt < route["retries"]:
                            time.sleep(0.5)
        if not errors and unsupported:
            raise UnsupportedCompletionOption("No configured route supports this request. " + "; ".join(unsupported))
        raise ChatAPIError("All configured completion routes failed. " + "; ".join(errors + unsupported))

    def _execute(self, route: dict, messages: list[dict], options: dict) -> dict:
        provider = route["provider"]
        if provider in {"openai", "openrouter"}:
            return self._openai(route, messages, options)
        return getattr(self, "_" + provider)(route, messages, options)

    def _post_json(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        response = httpx.post(url, json=payload, headers=headers, timeout=settings.default_timeout)
        response.raise_for_status()
        return response.json()

    def _openai(self, route: dict, messages: list[dict], options: dict) -> dict:
        provider, effort = route["provider"], route.get("effort")
        request = deepcopy(options)
        # Session routing configuration owns effort; clients cannot override it.
        request.pop("reasoning_effort", None)
        if effort is not None:
            if provider == "openrouter":
                request["extra_body"] = {"reasoning": {"effort": effort}}
            else:
                request["reasoning_effort"] = effort
            if "max_tokens" in request and provider == "openai":
                request["max_completion_tokens"] = request.pop("max_tokens")
            openai_model = provider == "openai" or route["model"].startswith("openai/")
            if openai_model and effort != "none" and "temperature" in request:
                if request["temperature"] != 1:
                    raise UnsupportedCompletionOption("This reasoning route requires temperature=1 or an omitted temperature.")
                request.pop("temperature")
        request.update(model=route["model"], messages=deepcopy(messages), stream=False)
        if provider not in self._clients:
            factory = create_openai_client if provider == "openai" else create_openrouter_client
            self._clients[provider] = factory(timeout=settings.default_timeout)
        client = self._clients[provider]
        if client is None:
            raise ChatAPIError(f"The {provider} SDK is unavailable; install the openai package.")
        response = _dump(client.chat.completions.create(**request))
        choices = response.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ChatAPIError(f"{provider} returned no assistant message.")
        message = choices[0]["message"]
        message.setdefault("content", None)
        message.setdefault("role", "assistant")
        return {"message": message, "finish_reason": choices[0].get("finish_reason") or ("tool_calls" if message.get("tool_calls") else "stop"),
                "usage": response.get("usage"), "model": response.get("model")}

    def _claude(self, route: dict, messages: list[dict], options: dict) -> dict:
        _native_options(options, "Claude", {"seed", "frequency_penalty", "presence_penalty", "response_format"})
        system, native = [], []
        for message in messages:
            role = message["role"]
            if role in {"system", "developer"}:
                system.append(_text(message.get("content")))
                continue
            if role == "tool":
                parts = [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": _text(message.get("content"))}]
                _append_content(native, "user", parts)
                continue
            preserved = message.get("extra_content", {}).get("anthropic", {}).get("content")
            if role == "assistant" and preserved:
                parts = deepcopy(preserved)
            else:
                text = _text(message.get("content"))
                parts = [{"type": "text", "text": text}] if text else []
                for call in message.get("tool_calls") or []:
                    parts.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": _arguments(call)})
            _append_content(native, role, parts)
        request = {"model": route["model"], "messages": native, "system": "\n\n".join(system),
                   "max_tokens": options.get("max_completion_tokens", options.get("max_tokens", 4096))}
        for name in ("temperature", "top_p"):
            if name in options:
                request[name] = options[name]
        if "stop" in options:
            request["stop_sequences"] = [options["stop"]] if isinstance(options["stop"], str) else options["stop"]
        if route.get("effort") is not None:
            request["output_config"] = {"effort": route["effort"]}
        if options.get("tools"):
            request["tools"] = []
            for tool in options["tools"]:
                definition = tool["function"]
                converted = {"name": definition["name"], "input_schema": definition.get("parameters", {"type": "object", "properties": {}})}
                for key in ("description", "strict"):
                    if key in definition:
                        converted[key] = definition[key]
                request["tools"].append(converted)
            choice = options.get("tool_choice", "auto")
            if isinstance(choice, dict):
                request["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
            else:
                request["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
            if "parallel_tool_calls" in options and choice != "none":
                request["tool_choice"]["disable_parallel_tool_use"] = not options["parallel_tool_calls"]
        if "claude" not in self._clients:
            self._clients["claude"] = create_claude_client(timeout=settings.default_timeout)
        response = _dump(self._clients["claude"].messages.create(**request))
        blocks = response.get("content") or []
        calls = [_tool_call(block["name"], block["input"], block["id"]) for block in blocks if block.get("type") == "tool_use"]
        texts = [block["text"] for block in blocks if block.get("type") == "text"]
        message = {"role": "assistant", "content": "".join(texts) if texts else None}
        if calls:
            message["tool_calls"] = calls
        thinking = [block for block in blocks if block.get("type") in {"thinking", "redacted_thinking"}]
        if thinking:
            message["reasoning_content"] = "".join(block.get("thinking", "") for block in thinking)
            message["extra_content"] = {"anthropic": {"content": blocks}}
        usage = response.get("usage") or {}
        prompt = sum(usage.get(key, 0) or 0 for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) if "input_tokens" in usage else None
        reason = {"tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}.get(response.get("stop_reason"), "stop")
        return {"message": message, "finish_reason": reason, "model": response.get("model"),
                "usage": {"prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens")}}

    def _gemini(self, route: dict, messages: list[dict], options: dict) -> dict:
        _native_options(options, "Gemini", {"frequency_penalty", "presence_penalty"})
        if options.get("tools") and options.get("parallel_tool_calls") is False and options.get("tool_choice") != "none":
            raise UnsupportedCompletionOption("Gemini cannot guarantee parallel_tool_calls=false.")
        system, contents, call_names = [], [], {}
        for message in messages:
            role = message["role"]
            if role in {"system", "developer"}:
                system.append(_text(message.get("content")))
                continue
            if role == "tool":
                name, native_id = call_names[message["tool_call_id"]]
                text = _text(message.get("content"))
                try:
                    result = json.loads(text)
                except ValueError:
                    result = text
                response = {"name": name, "response": result if isinstance(result, dict) else {"result": result}}
                if native_id:
                    response["id"] = native_id
                parts = [{"functionResponse": response}]
            else:
                text = _text(message.get("content"))
                parts = [{"text": text}] if text else []
                for call in message.get("tool_calls") or []:
                    metadata = call.get("extra_content", {}).get("google", {})
                    native_id = metadata.get("id")
                    function = {"name": call["function"]["name"], "args": _arguments(call)}
                    if native_id:
                        function["id"] = native_id
                    part = {"functionCall": function}
                    if metadata.get("thought_signature"):
                        part["thoughtSignature"] = metadata["thought_signature"]
                    parts.append(part)
                    call_names[call["id"]] = (function["name"], native_id)
                preserved = message.get("extra_content", {}).get("google", {}).get("parts")
                if role == "assistant" and preserved:
                    parts = deepcopy(preserved)
            native_role = "model" if role == "assistant" else "user"
            if parts:
                if contents and contents[-1]["role"] == native_role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": native_role, "parts": parts})
        generation = {}
        for source, target in (("temperature", "temperature"), ("top_p", "topP"), ("seed", "seed")):
            if source in options:
                generation[target] = options[source]
        if "max_completion_tokens" in options or "max_tokens" in options:
            generation["maxOutputTokens"] = options.get("max_completion_tokens", options.get("max_tokens"))
        if "stop" in options:
            generation["stopSequences"] = [options["stop"]] if isinstance(options["stop"], str) else options["stop"]
        if route.get("effort") is not None:
            generation["thinkingConfig"] = gemini_thinking_config(route["model"], route["effort"], rest=True)
        output_format = options.get("response_format", {})
        if output_format.get("type") in {"json_object", "json_schema"}:
            generation["responseMimeType"] = "application/json"
            if output_format["type"] == "json_schema":
                generation["responseJsonSchema"] = output_format["json_schema"]["schema"]
        elif output_format.get("type") not in (None, "text"):
            raise UnsupportedCompletionOption("Gemini supports text, json_object, or json_schema response_format.")
        payload = {"contents": contents, "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if options.get("tools"):
            declarations = []
            for tool in options["tools"]:
                definition = tool["function"]
                if definition.get("strict"):
                    raise UnsupportedCompletionOption("Gemini does not support OpenAI strict function schemas.")
                declarations.append({"name": definition["name"], "description": definition.get("description", ""),
                                     "parametersJsonSchema": definition.get("parameters", {"type": "object", "properties": {}})})
            payload["tools"] = [{"functionDeclarations": declarations}]
            choice = options.get("tool_choice", "auto")
            calling = {"mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}.get(choice, "ANY")} if isinstance(choice, str) else {"mode": "ANY", "allowedFunctionNames": [choice["function"]["name"]]}
            payload["toolConfig"] = {"functionCallingConfig": calling}
        key = get_gemini_api_key()
        if not key:
            raise ChatAPIError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use Gemini.")
        response = self._post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{route['model']}:generateContent",
            payload, {"x-goog-api-key": key},
        )
        candidates = response.get("candidates") or []
        if not candidates:
            if response.get("promptFeedback", {}).get("blockReason"):
                return {"message": {"role": "assistant", "content": None}, "finish_reason": "content_filter"}
            raise ChatAPIError("Gemini returned no completion candidate.")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        calls = []
        for part in parts:
            function = part.get("functionCall")
            if function:
                call = _tool_call(function["name"], function.get("args", {}), function.get("id"))
                metadata = {}
                if part.get("thoughtSignature"):
                    metadata["thought_signature"] = part["thoughtSignature"]
                if function.get("id"):
                    metadata["id"] = function["id"]
                if metadata:
                    call["extra_content"] = {"google": metadata}
                calls.append(call)
        texts = [part["text"] for part in parts if "text" in part and not part.get("thought")]
        message = {"role": "assistant", "content": "".join(texts) if texts else None}
        if calls:
            message["tool_calls"] = calls
        if any(part.get("thoughtSignature") for part in parts):
            message["extra_content"] = {"google": {"parts": parts}}
        thoughts = "".join(part.get("text", "") for part in parts if part.get("thought"))
        if thoughts:
            message["reasoning_content"] = thoughts
        usage = response.get("usageMetadata") or {}
        completion = ((usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0)) if usage else None
        reason = "tool_calls" if calls else {"MAX_TOKENS": "length", "SAFETY": "content_filter", "RECITATION": "content_filter"}.get(candidate.get("finishReason"), "stop")
        return {"message": message, "finish_reason": reason, "model": response.get("modelVersion"),
                "usage": {"prompt_tokens": usage.get("promptTokenCount"), "completion_tokens": completion,
                          "total_tokens": usage.get("totalTokenCount")}}

    def _ollama(self, route: dict, messages: list[dict], options: dict) -> dict:
        choice = options.get("tool_choice", "auto")
        if isinstance(choice, dict) or choice == "required":
            raise UnsupportedCompletionOption("Ollama's native API cannot force a named or required tool call.")
        if options.get("tools") and options.get("parallel_tool_calls") is False and choice != "none":
            raise UnsupportedCompletionOption("Ollama's native API cannot guarantee parallel_tool_calls=false.")
        native, names = [], {}
        for message in messages:
            role = "system" if message["role"] == "developer" else message["role"]
            converted = {"role": role, "content": _text(message.get("content"))}
            if message.get("reasoning_content"):
                converted["thinking"] = message["reasoning_content"]
            if role == "tool":
                converted["tool_name"] = names[message["tool_call_id"]]
            if message.get("tool_calls"):
                converted["tool_calls"] = []
                for call in message["tool_calls"]:
                    names[call["id"]] = call["function"]["name"]
                    converted["tool_calls"].append({"function": {"name": call["function"]["name"], "arguments": _arguments(call)}})
            native.append(converted)
        controls = {}
        for key in ("temperature", "top_p", "seed", "frequency_penalty", "presence_penalty", "stop"):
            if key in options:
                controls[key] = options[key]
        if isinstance(controls.get("stop"), str):
            controls["stop"] = [controls["stop"]]
        if "max_completion_tokens" in options or "max_tokens" in options:
            controls["num_predict"] = options.get("max_completion_tokens", options.get("max_tokens"))
        payload = {"model": route["model"], "messages": native, "stream": False, "options": controls}
        if route.get("effort") is not None:
            payload["think"] = route["effort"]
        if options.get("tools") and choice != "none":
            for tool in options["tools"]:
                if tool["function"].get("strict"):
                    raise UnsupportedCompletionOption("Ollama does not support OpenAI strict function schemas.")
            payload["tools"] = deepcopy(options["tools"])
        output_format = options.get("response_format", {})
        if output_format.get("type") == "json_object":
            payload["format"] = "json"
        elif output_format.get("type") == "json_schema":
            payload["format"] = output_format["json_schema"]["schema"]
        elif output_format.get("type") not in (None, "text"):
            raise UnsupportedCompletionOption("Ollama supports text, json_object, or json_schema response_format.")
        response = self._post_json(ollama_base_url(settings.ollama_host) + "/api/chat", payload)
        native_message = response.get("message") or {}
        message = {"role": "assistant", "content": native_message.get("content")}
        calls = [_tool_call(call["function"]["name"], call["function"].get("arguments", {}), call.get("id")) for call in native_message.get("tool_calls") or []]
        if calls:
            message["tool_calls"] = calls
        if native_message.get("thinking"):
            message["reasoning_content"] = native_message["thinking"]
        return {"message": message, "finish_reason": "tool_calls" if calls else ("length" if response.get("done_reason") == "length" else "stop"),
                "model": response.get("model"), "usage": {"prompt_tokens": response.get("prompt_eval_count"), "completion_tokens": response.get("eval_count")}}

    def _mock(self, route: dict, messages: list[dict], options: dict) -> dict:
        tools = options.get("tools") or []
        choice = options.get("tool_choice", "auto")
        forced = isinstance(choice, dict) or choice == "required"
        last = messages[-1]
        if tools and choice != "none" and (forced or last["role"] != "tool"):
            tool = next(tool for tool in tools if tool["function"]["name"] == choice["function"]["name"]) if isinstance(choice, dict) else tools[0]
            definition = tool["function"]
            arguments = _schema_example(definition.get("parameters", {}))
            digest = hashlib.sha256(json.dumps([messages, definition], sort_keys=True).encode()).hexdigest()[:24]
            message = {"role": "assistant", "content": None,
                       "tool_calls": [_tool_call(definition["name"], arguments, "call_mock_" + digest)]}
            return {"message": message, "finish_reason": "tool_calls"}
        text = ("Mock tool result received: " if last["role"] == "tool" else "Mock completion: ") + _text(last.get("content"))
        output_format = options.get("response_format", {})
        if output_format.get("type") == "json_object":
            text = json.dumps({"mock": True, "message": text})
        elif output_format.get("type") == "json_schema":
            text = json.dumps(_schema_example(output_format["json_schema"]["schema"]))
        reason = "stop"
        stops = options.get("stop", [])
        for stop in ([stops] if isinstance(stops, str) else stops):
            if stop and stop in text:
                text = text.split(stop, 1)[0]
        limit = options.get("max_completion_tokens", options.get("max_tokens"))
        if limit is not None and len(text) > limit * 4:
            text, reason = text[:limit * 4], "length"
        return {"message": {"role": "assistant", "content": text}, "finish_reason": reason}


def _schema_example(schema: dict) -> Any:
    """Deterministic mock arguments; never infer or execute a real-world action."""
    if "default" in schema:
        return deepcopy(schema["default"])
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type", "object")
    if kind == "object":
        return {name: _schema_example(value) for name, value in schema.get("properties", {}).items() if name in schema.get("required", [])}
    return {"string": "", "integer": 0, "number": 0, "boolean": False, "array": [], "null": None}.get(kind)


completion_service = CompletionService()

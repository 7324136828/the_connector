"""OpenAI chat/model endpoints and discovery probes for external agent clients.

These requests are stateless: the external agent owns its conversation/tool loop.
Only active entries in the configuration library can be used as model IDs.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import settings, ROOT_DIR
from ..audit_context import set_audit_context
from ..services.configuration_manager import configuration_manager
from ..services.completion_service import completion_service, UnsupportedCompletionOption
from ..services.connectors.common import ChatAPIError
from ..services.session_manager import session_manager

router = APIRouter(tags=["Agent compatibility"])


class CompatibilityError(Exception):
    def __init__(self, status_code: int, message: str, code: str = "invalid_request_error", param: str | None = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        self.param = param


def error_response(exc: CompatibilityError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {
        "message": exc.message, "type": "server_error" if exc.status_code >= 500 else "invalid_request_error",
        "param": exc.param, "code": exc.code,
    }})


class CompletionMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def check_content(self):
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "assistant" and self.content is None:
            raise ValueError("content is required for non-assistant messages")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("assistant messages require content or tool_calls")
        return self


class CompletionRequest(BaseModel):
    # Accept only supported fields; quietly dropping client tools/options would
    # make an apparently successful agent response misleading.
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    messages: list[CompletionMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    reasoning_effort: str | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    n: Literal[1] = 1
    user: str | None = None
    metadata: dict[str, str] | None = None
    store: Literal[False] = False

    @field_validator("stream_options")
    @classmethod
    def check_stream_options(cls, value):
        if value is not None and (set(value) - {"include_usage"} or type(value.get("include_usage", False)) is not bool):
            raise ValueError("stream_options supports only include_usage: true/false")
        return value


def active_model(model_id: str) -> dict:
    record = configuration_manager.get_active_model(model_id)
    if record is None:
        raise CompatibilityError(404, f"Active configuration model '{model_id}' not found. "
                                 "Save and activate a configuration in /api/configs, then use its model_id.",
                                 "model_not_found", "model")
    return record


def config_context_length(record: dict) -> int | None:
    """Use declared capacity, or a conservative known limit across every route."""
    if record.get("context_length"):
        return record["context_length"]
    known = {
        ("openai", "gpt-4o-mini"): 128000, ("openai", "gpt-4o"): 128000,
        ("openai", "gpt-5"): 128000, ("openai", "gpt-5-mini"): 128000,
        ("openai", "gpt-5-nano"): 128000, ("mock", "mock-assistant"): 32000,
    }
    limits = []
    for step in record["config"]["sequences"]:
        for route in step.get("choices", [step]):
            provider, model = route["provider"], route["model"]
            if provider == "openrouter" and model.startswith("openai/"):
                provider, model = "openai", model.removeprefix("openai/")
            limit = known.get((provider, model))
            if limit is None:
                return None
            limits.append(limit)
    return min(limits) if limits else None


def model_object(record: dict) -> dict:
    result = {"id": record["model_id"], "object": "model",
              "created": int(datetime.fromisoformat(record["created_at"]).timestamp()),
              "owned_by": "the_connector", "name": record["name"],
              "description": record.get("description", ""), "configuration_id": record["id"]}
    context = config_context_length(record)
    if context is not None:
        result["context_length"] = context
    return result


@router.get("/v1/models")
@router.get("/api/v1/models")
@router.get("/api/models")
@router.get("/models")
def list_models():
    return {"object": "list", "data": [model_object(r) for r in configuration_manager.list_configs(active_only=True)]}


@router.get("/v1/models/{model_id}")
@router.get("/api/v1/models/{model_id}")
@router.get("/api/models/{model_id}")
@router.get("/models/{model_id}")
def retrieve_model(model_id: str):
    return model_object(active_model(model_id))


def completion_events(response: dict, include_usage: bool):
    """Emit valid buffered SSE, preserving tools, finish reason, and usage."""
    base = {key: response[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    def event(delta, finish_reason=None):
        chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        if include_usage:
            chunk["usage"] = None
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    yield event({"role": "assistant"})
    choice = response["choices"][0]
    message = choice["message"]
    for key in ("reasoning_content", "content"):
        value = message.get(key)
        if isinstance(value, str):
            for offset in range(0, len(value), 256):
                yield event({key: value[offset:offset + 256]})
    if message.get("tool_calls"):
        yield event({"tool_calls": [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]})
    # Provider metadata (e.g. Gemini thought signatures) is needed for replay.
    extra = {key: value for key, value in message.items() if key not in {"role", "content", "tool_calls", "reasoning_content"}}
    if extra:
        yield event(extra)
    yield event({}, choice["finish_reason"])
    if include_usage:
        yield "data: " + json.dumps({**base, "choices": [], "usage": response["usage"]}) + "\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
@router.post("/api/v1/chat/completions")
@router.post("/api/chat/completions")
@router.post("/chat/completions")
def chat_completions(req: CompletionRequest):
    set_audit_context(requested_model=req.model)
    record = active_model(req.model)
    set_audit_context(configuration_id=record["id"])
    # Copying via model_dump gives transports their own payload; session archive
    # is deliberately not mixed into an external agent's supplied conversation.
    messages = [m.model_dump(exclude_none=True) for m in req.messages]
    options = req.model_dump(exclude_none=True, exclude={
        "model", "messages", "stream", "stream_options", "n", "user", "metadata", "store",
    })
    try:
        config = session_manager.config_with_memory(record["config"])
        result = completion_service.complete(messages=messages, config=config, options=options)
    except UnsupportedCompletionOption as exc:
        raise CompatibilityError(400, str(exc), "unsupported_option") from exc
    except ValueError as exc:
        raise CompatibilityError(400, str(exc)) from exc
    except ChatAPIError as exc:
        raise CompatibilityError(502, str(exc), "upstream_error") from exc
    message = result["message"]
    if (message.get("content") is None and not message.get("tool_calls")
            and not message.get("refusal") and result["finish_reason"] != "content_filter"):
        raise CompatibilityError(502, "The configured providers returned neither text nor tool calls.", "empty_completion")
    response = {
        "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
        "created": int(time.time()), "model": req.model,
        "choices": [{"index": 0, "message": message, "finish_reason": result["finish_reason"]}],
        "usage": result["usage"],
    }
    session_manager.record_completion_response(response)
    if req.stream:
        return StreamingResponse(completion_events(response, bool((req.stream_options or {}).get("include_usage"))),
                                 media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return response


def ollama_model(record: dict) -> dict:
    serialized = json.dumps(record["config"], sort_keys=True).encode()
    return {"name": record["model_id"], "model": record["model_id"],
            "modified_at": record["updated_at"], "size": len(serialized),
            "digest": hashlib.sha256(serialized).hexdigest(),
            "details": {"format": "configuration", "family": "connector", "families": ["connector"],
                        "parameter_size": "", "quantization_level": ""}}


@router.get("/api/tags")
@router.get("/api/api/tags")
def tags():
    return {"models": [ollama_model(r) for r in configuration_manager.list_configs(active_only=True)]}


@router.post("/api/show")
@router.post("/api/api/show")
def show_model(body: dict[str, Any] = Body(...)):
    model_id = body.get("model") or body.get("name")
    if not isinstance(model_id, str) or not model_id:
        raise CompatibilityError(400, "Provide a model (or name) from the active configuration list.", param="model")
    record = active_model(model_id)
    context = config_context_length(record)
    info = {"general.architecture": "connector", "general.name": record["name"]}
    if context:
        info["connector.context_length"] = context
    return {"name": record["model_id"], "model": record["model_id"],
            "details": ollama_model(record)["details"], "model_info": info,
            "parameters": f"num_ctx {context}" if context else "",
            "capabilities": ["completion", "tools"], "modified_at": record["updated_at"],
            "template": "", "configuration_id": record["id"]}


@router.get("/props")
@router.get("/v1/props")
@router.get("/api/props")
@router.get("/api/v1/props")
def props(model: str | None = None):
    records = [active_model(model)] if model else configuration_manager.list_configs(active_only=True)
    contexts = [config_context_length(r) for r in records]
    context = min(contexts) if contexts and all(c is not None for c in contexts) else None
    generation = {"model": records[0]["model_id"] if len(records) == 1 else None}
    if context:
        generation["n_ctx"] = context
        generation["params"] = {"n_ctx": context}
    return {"service": settings.app_name, "version": settings.app_version,
            "models": [model_object(r) for r in records], "default_generation_settings": generation,
            "model_path": generation["model"], "total_slots": 1,
            "chat_template": "", "streaming": "buffered", "tool_calling": True}


@router.get("/api/version")
@router.get("/version")
def version():
    return {"version": settings.app_version, "service": settings.app_name, "api": "openai-compatible"}


@router.get("/")
def root():
    return {"service": settings.app_name, "version": settings.app_version, "status": "ok",
            "docs": "/docs", "configurations": "/api/configs", "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "active_configurations": len(configuration_manager.list_configs(active_only=True))}


@router.get("/favicon.ico", include_in_schema=False)
@router.get("/favicon.svg", include_in_schema=False)
def favicon():
    icon = ROOT_DIR / "frontend" / "public" / "favicon.svg"
    return Response(icon.read_bytes() if icon.is_file() else b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#10b981"/></svg>',
                    media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

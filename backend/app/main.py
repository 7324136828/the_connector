"""The Connector - FastAPI Gateway and Route Handlers."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import Body, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import settings
from .response_logging import LoggedFastAPI, ResponseLogWriter
from .audit_store import AuditStore
from .schemas.chat import (
    AgentRegisterToolRequest,
    AgentRunRequest,
    AgentRunResponse,
    AgentStepRequest,
    AgentStepResponse,
    AgentToolDefinition,
    ChatRequest,
    ChatResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    ModelInfo,
    NewSessionRequest,
    NewSessionResponse,
    SessionDetail,
    SessionSummary,
    TokenUsageInfo,
    UpdateSessionRequest,
)
from .schemas.configuration import normalize_config, example_config
from .model_capabilities import effort_levels, default_effort
from .services import (
    agent_service,
    router,
    session_manager,
    temp_manager,
)
from .services.connectors import list_ollama_models
from .services.configuration_manager import configuration_manager
from .services.configuration_history import configuration_history_manager
from .api.configurations import router as configurations_api
from .api.configuration_history import router as configuration_history_api
from .api.compatibility import router as compatibility_api, CompatibilityError, error_response

response_log_writer = ResponseLogWriter(
    settings.response_log_dir, enabled=settings.response_logging_enabled,
    max_bytes=settings.response_log_max_bytes, backup_count=settings.response_log_backup_count,
)
internal_audit_store = AuditStore(settings.db_path, enabled=settings.internal_audit_enabled)

app = LoggedFastAPI(
    response_log_writer=response_log_writer,
    response_log_max_body_bytes=settings.response_log_max_body_bytes,
    request_log_max_body_bytes=settings.request_log_max_body_bytes,
    internal_audit_store=internal_audit_store,
    title=settings.app_name,
    version=settings.app_version,
    description="Full-stack multi-provider LLM connector with ChatGPT-like interface and agentic plugin support.",
)

# CORS middleware for React Vite frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(configurations_api)
app.include_router(configuration_history_api)
app.include_router(compatibility_api)


@app.exception_handler(CompatibilityError)
async def compatibility_error_handler(request: Request, exc: CompatibilityError):
    return error_response(exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.endswith("/chat/completions"):
        descriptions = [".".join(str(v) for v in error["loc"][1:]) + ": " + error["msg"] for error in exc.errors()]
        return error_response(CompatibilityError(400, "; ".join(descriptions)))
    return await request_validation_exception_handler(request, exc)


@app.get("/api/health")
def health_check() -> Dict[str, Any]:
    """System health check and provider readiness."""
    return {
        "status": "ok",
        "version": settings.app_version,
        "providers_status": {
            "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "claude": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()),
            "openrouter": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
            "ollama_host": settings.ollama_host,
            "mock": True,
        },
    }


# ==========================================
# 1. Session Lifecycle Endpoints
# ==========================================

@app.post("/api/sessions", response_model=NewSessionResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/new", response_model=NewSessionResponse, status_code=status.HTTP_201_CREATED, deprecated=True)
def new_session(req: NewSessionRequest) -> NewSessionResponse:
    """Establish a new chat session with user-selected configuration."""
    if req.config_id:
        record = configuration_manager.get_config(req.config_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Saved configuration not found.")
        if not record["active"]:
            raise HTTPException(status_code=409, detail="Activate this saved configuration before creating a session.")
        # Sessions own immutable snapshots; library edits never rewrite a chat.
        req = NewSessionRequest(title=req.title, config=record["config"])
    elif req.history_id:
        record = configuration_history_manager.get_history(req.history_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Configuration history entry not found.")
        req = NewSessionRequest(title=req.title, config=record["config"])
    summary = session_manager.create_session(req)
    return NewSessionResponse(
        session_id=summary.session_id,
        title=summary.title,
        provider=summary.provider,
        model=summary.model,
        system_prompt=req.config["system_prompt"],
        past_memory=summary.past_memory,
        context_window=summary.context_window,
        created_at=summary.created_at,
        status=summary.status,
    )


@app.get("/api/sessions", response_model=List[SessionSummary])
def list_sessions() -> List[SessionSummary]:
    """List recent active chat sessions."""
    return session_manager.list_sessions()


@app.get("/api/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: str) -> SessionDetail:
    """Retrieve details and full message transcript for a session."""
    detail = session_manager.get_session(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return detail


def session_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=404 if "not found" in str(exc) else 409, detail=str(exc))


@app.patch("/api/sessions/{session_id}", response_model=SessionDetail)
def update_session(session_id: str, req: UpdateSessionRequest) -> SessionDetail:
    """Replace just this session's config snapshot."""
    try:
        return session_manager.update_session_config(session_id, req.config)
    except ValueError as exc:
        raise session_error(exc) from exc


@app.delete("/api/close", response_model=CloseSessionResponse)
def close_session_endpoint(
    req: Optional[CloseSessionRequest] = None,
    session_id: Optional[str] = Query(default=None),
) -> CloseSessionResponse:
    """End and deactivate a session (accepts JSON body or query param)."""
    target_id = (req.session_id if req else None) or session_id
    if not target_id:
        raise HTTPException(status_code=400, detail="session_id must be provided via query param or JSON body.")

    success = session_manager.close_session(target_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Session '{target_id}' not found.")
    temp_manager.purge_temp(target_id)

    return CloseSessionResponse(
        session_id=target_id,
        status="closed",
        detail="Session deactivated and temporary files purged.",
    )


@app.delete("/api/sessions/{session_id}", response_model=CloseSessionResponse)
def delete_session(session_id: str) -> CloseSessionResponse:
    """RESTful alias to end a session."""
    return close_session_endpoint(session_id=session_id)


# ==========================================
# 2. Chat Completion & Routing Endpoint
# ==========================================

@app.post("/api/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """
    Send a user prompt to a session via specific routing logic.
    Maintains a sliding context window unless past_memory is toggled off (stateless).
    """
    try:
        # Retrieve context window and session configs
        (
            history,
            system_prompt,
            session_provider,
            session_model,
            session_config,
        ) = session_manager.get_context_window(req.session_id)
    except ValueError as exc:
        raise session_error(exc) from exc

    # Prepare message payload for the LLM
    # If past_memory is True, history contains recent turns. If False, history is empty!
    messages_payload: List[Dict[str, str]] = list(history)
    messages_payload.append({"role": "user", "content": req.message})

    try:
        # Dispatch through the intelligent router
        route_res = router.route_chat(
            messages=messages_payload,
            system_prompt=system_prompt,
            config=session_config,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Routing failure: {exc}",
        ) from exc

    # Persist a complete exchange atomically, including if another client closes
    # the session while the provider is generating a reply.
    try:
        assistant_msg = session_manager.add_exchange(
            session_id=req.session_id, user_content=req.message,
            assistant_content=route_res.content, provider=route_res.provider,
            model=route_res.model, tokens=route_res.tokens, latency_ms=route_res.latency_ms,
        )
    except ValueError as exc:
        raise session_error(exc) from exc

    return ChatResponse(
        session_id=req.session_id,
        message_id=assistant_msg.id or "",
        role="assistant",
        content=route_res.content,
        provider=route_res.provider,
        model=route_res.model,
        tokens=TokenUsageInfo(
            input_tokens=route_res.tokens.get("input_tokens"),
            output_tokens=route_res.tokens.get("output_tokens"),
            total_tokens=route_res.tokens.get("total_tokens"),
        ),
        latency_ms=route_res.latency_ms,
        attempt_info=route_res.attempt_info,
        created_at=assistant_msg.created_at or "",
    )


# ==========================================
# 3. Agentic Support Endpoints (Plugin Support)
# ==========================================

@app.get("/api/agent/tools", response_model=List[AgentToolDefinition])
def list_agent_tools() -> List[AgentToolDefinition]:
    """List available agent tools and schemas for plugin integration."""
    return agent_service.list_tools()


@app.post("/api/agent/register-tool", status_code=status.HTTP_201_CREATED)
def register_tool(req: AgentRegisterToolRequest) -> Dict[str, Any]:
    """Dynamically register a tool or external plugin webhook."""
    agent_service.register_tool(
        name=req.name,
        description=req.description,
        parameters=req.parameters,
        endpoint=req.endpoint,
    )
    return {"status": "registered", "tool": req.name}


@app.post("/api/agent/run", response_model=AgentRunResponse)
def run_agent(req: AgentRunRequest) -> AgentRunResponse:
    """Execute an autonomous ReAct loop with tools and multi-step reasoning."""
    try:
        history, system_prompt, _, _, config = session_manager.get_context_window(req.session_id)
    except ValueError as exc:
        raise session_error(exc) from exc
    try:
        resp = agent_service.run_agent(req, history=history, system_prompt=system_prompt, config=config)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent routing failure: {exc}") from exc
    try:
        session_manager.add_exchange(
            session_id=req.session_id, user_content=req.prompt,
            assistant_content=resp.final_answer, provider=resp.provider, model=resp.model,
            tokens={"total_tokens": resp.total_tokens}, latency_ms=resp.latency_ms,
        )
    except ValueError as exc:
        raise session_error(exc) from exc
    return resp


@app.post("/api/agent/step", response_model=AgentStepResponse)
def step_agent(req: AgentStepRequest) -> AgentStepResponse:
    """Directly execute a single tool step."""
    return agent_service.execute_tool(req.tool, req.arguments)


# ==========================================
# 4. Configuration, Models & Export Endpoints
# ==========================================

@app.get("/api/providers/models", response_model=List[ModelInfo])
def get_supported_models() -> List[ModelInfo]:
    """List supported models across all providers, including live Ollama models."""
    models: List[ModelInfo] = [
        # OpenAI
        ModelInfo(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai", context_length=128000, description="Fast, cost-efficient OpenAI model"),
        ModelInfo(id="gpt-4o", name="GPT-4o", provider="openai", context_length=128000, description="Flagship omni model from OpenAI"),
        ModelInfo(id="gpt-5-nano", name="GPT-5 Nano", provider="openai", context_length=128000, description="Next-gen nano reasoning model"),
        # Claude
        ModelInfo(id="claude-3-5-haiku-20241022", name="Claude 3.5 Haiku", provider="claude", context_length=200000, description="Fastest Anthropic model"),
        ModelInfo(id="claude-3-5-sonnet-20241022", name="Claude 3.5 Sonnet", provider="claude", context_length=200000, description="Anthropic flagship intelligence"),
        # Gemini
        ModelInfo(id="gemini-2.5-flash", name="Gemini 2.5 Flash", provider="gemini", context_length=1000000, description="High-speed Google multimodal model"),
        ModelInfo(id="gemini-1.5-pro", name="Gemini 1.5 Pro", provider="gemini", context_length=2000000, description="Deep reasoning Google model"),
        # OpenRouter
        ModelInfo(id="openai/gpt-4o-mini", name="OpenRouter: GPT-4o Mini", provider="openrouter", context_length=128000, description="OpenRouter routed OpenAI"),
        ModelInfo(id="anthropic/claude-3.5-haiku", name="OpenRouter: Claude Haiku", provider="openrouter", context_length=200000, description="OpenRouter routed Claude"),
        # Mock / Simulation
        ModelInfo(id="mock-assistant", name="Mock Assistant (Offline / Demo)", provider="mock", context_length=32000, description="Zero-setup local mock for testing"),
    ]

    # Dynamically query Ollama local models
    try:
        ollama_names = list_ollama_models(settings.ollama_host)
        for oname in ollama_names:
            models.append(
                ModelInfo(
                    id=oname,
                    name=f"Ollama: {oname}",
                    provider="ollama",
                    context_length=8192,
                    description="Local Ollama instance model",
                )
            )
    except Exception:
        pass

    # Ensure default Ollama model if none detected
    if not any(m.provider == "ollama" for m in models):
        models.append(
            ModelInfo(
                id="llama3.2",
                name="Ollama: llama3.2",
                provider="ollama",
                context_length=8192,
                description="Default Ollama model",
            )
        )

    # Additional reasoning-capable choices; effort controls share validation with
    # uploaded routes rather than trusting frontend-provided capability lists.
    for provider, model, name in [
        ("openai", "gpt-5", "GPT-5"), ("openai", "gpt-5-mini", "GPT-5 Mini"),
        ("openai", "gpt-5.1", "GPT-5.1"), ("openai", "gpt-5.2", "GPT-5.2"),
        ("openai", "gpt-5.6-luna", "GPT-5.6 Luna"),
        ("openai", "o3", "o3"), ("openai", "o4-mini", "o4 Mini"),
        ("claude", "claude-opus-4-5", "Claude Opus 4.5"),
        ("claude", "claude-opus-4-6", "Claude Opus 4.6"),
        ("claude", "claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("openrouter", "openai/gpt-5-nano", "OpenRouter: GPT-5 Nano"),
    ]:
        models.append(ModelInfo(id=model, name=name, provider=provider))
    for model in models:
        model.effort_levels = effort_levels(model.provider, model.id)
        model.default_effort = default_effort(model.provider, model.id)
    return models


@app.get("/api/config/example")
def download_config_example():
    """Download an example; never read or expose the server's configuration."""
    return JSONResponse(example_config(), headers={"Content-Disposition": 'attachment; filename="config.json"'})


@app.post("/api/config/validate")
def validate_routing_config(config_data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return normalize_config(config_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/models/capabilities")
def model_capabilities(model: Dict[str, str] = Body(...)) -> Dict[str, Any]:
    return {"effort_levels": effort_levels(model.get("provider", ""), model.get("model", "")),
            "default_effort": default_effort(model.get("provider", ""), model.get("model", ""))}

@app.get("/api/sessions/{session_id}/export-zip")
def export_session_zip(session_id: str):
    """Package and download full chat transcript as a ZIP archive."""
    detail = session_manager.get_session(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")

    zip_path = temp_manager.package_session_export_zip(detail)
    if not zip_path.is_file():
        raise HTTPException(status_code=500, detail="Could not create export archive.")

    return FileResponse(
        path=zip_path,
        filename=f"session_{session_id[:8]}_export.zip",
        media_type="application/zip",
    )

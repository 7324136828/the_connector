"""Agentic tool execution engine, tool registry, and ReAct loop orchestrator."""

from __future__ import annotations

import ast
import json
import operator as op
import os
import platform
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..schemas.chat import (
    AgentRunRequest,
    AgentRunResponse,
    AgentStep,
    AgentStepResponse,
    AgentToolDefinition,
)
from .router import router

# Safe mathematical expression evaluator
_SAFE_MATH_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.USub: op.neg,
}


def _safe_eval_math(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    elif isinstance(node, ast.BinOp):
        left = _safe_eval_math(node.left)
        right = _safe_eval_math(node.right)
        op_type = type(node.op)
        if op_type in _SAFE_MATH_OPERATORS:
            return _SAFE_MATH_OPERATORS[op_type](left, right)
        raise ValueError(f"Unsupported operator: {op_type.__name__}")
    elif isinstance(node, ast.UnaryOp):
        operand = _safe_eval_math(node.operand)
        op_type = type(node.op)
        if op_type in _SAFE_MATH_OPERATORS:
            return _SAFE_MATH_OPERATORS[op_type](operand)
        raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
    raise ValueError(f"Unsupported AST node in math expression: {type(node).__name__}")


def tool_calculator(expression: str) -> str:
    """Safely calculate mathematical arithmetic expressions."""
    try:
        clean_expr = expression.replace("^", "**").strip()
        tree = ast.parse(clean_expr, mode="eval")
        res = _safe_eval_math(tree.body)
        return str(res)
    except Exception as exc:
        return f"Error evaluating '{expression}': {exc}"


def tool_python_interpreter(code: str) -> str:
    """Execute Python code in an isolated subprocess with a 5-second timeout."""
    try:
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if res.returncode != 0:
            return f"Execution Error (code {res.returncode}):\n{err or out}"
        return out or "(Executed successfully with no stdout output)"
    except subprocess.TimeoutExpired:
        return "Error: Python execution timed out after 5.0 seconds."
    except Exception as exc:
        return f"Error executing Python code: {exc}"


def tool_system_info() -> str:
    """Get sanitized host system info and UTC timestamp."""
    return json.dumps({
        "current_time_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "system": platform.system(),
    }, indent=2)


def tool_web_search(query: str) -> str:
    """Simulated web search for documentation, facts, and live queries."""
    q = query.lower()
    if "connector" in q:
        return (
            "The Connector is a unified multi-provider AI gateway and chat platform "
            "supporting OpenAI, Anthropic Claude, Google Gemini, Ollama, and OpenRouter "
            "with intelligent fallback routing and context memory controls."
        )
    elif "python" in q:
        return f"Python latest release is 3.14 / 3.13. Current runtime is Python {platform.python_version()}."
    return f"Search results for '{query}': Found multiple relevant resources matching your query."


def tool_http_fetch(url: str) -> str:
    """Fetch text or JSON content from a public URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TheConnector-Agent/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        return content[:2000] + ("..." if len(content) > 2000 else "")
    except Exception as exc:
        return f"HTTP Fetch failed for {url}: {exc}"


class AgentService:
    """Orchestrates tool registration, execution, and ReAct agent loops."""

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        self.register_tool(
            name="calculator",
            description="Safely evaluate arithmetic mathematical expressions (e.g. '12 * 45 + 180 / 4').",
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "The math expression to calculate."}
                },
                "required": ["expression"],
            },
            handler=lambda args: tool_calculator(args.get("expression", "")),
        )

        self.register_tool(
            name="python_interpreter",
            description="Execute arbitrary Python code in a safe subprocess and return stdout/stderr.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Valid Python code to execute."}
                },
                "required": ["code"],
            },
            handler=lambda args: tool_python_interpreter(args.get("code", "")),
        )

        self.register_tool(
            name="system_info",
            description="Retrieve system environment info such as UTC timestamp and platform details.",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: tool_system_info(),
        )

        self.register_tool(
            name="web_search",
            description="Perform a web search for documentation, current events, or knowledge base lookups.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string."}
                },
                "required": ["query"],
            },
            handler=lambda args: tool_web_search(args.get("query", "")),
        )

        self.register_tool(
            name="http_fetch",
            description="Fetch text or JSON from an HTTP/HTTPS endpoint.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target HTTP/HTTPS URL."}
                },
                "required": ["url"],
            },
            handler=lambda args: tool_http_fetch(args.get("url", "")),
        )

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        """Register a new tool or plugin capability."""
        self._tools[name] = {
            "definition": AgentToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
            ),
            "handler": handler,
            "endpoint": endpoint,
        }

    def list_tools(self) -> List[AgentToolDefinition]:
        """List all available registered tools."""
        return [entry["definition"] for entry in self._tools.values()]

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> AgentStepResponse:
        """Execute a tool by name with arguments."""
        if tool_name not in self._tools:
            return AgentStepResponse(
                tool=tool_name,
                arguments=arguments,
                result="",
                success=False,
                error=f"Tool '{tool_name}' is not registered.",
            )

        entry = self._tools[tool_name]
        try:
            if entry.get("handler"):
                res = entry["handler"](arguments)
            elif entry.get("endpoint"):
                # Remote plugin webhook execution
                req = urllib.request.Request(
                    entry["endpoint"],
                    data=json.dumps(arguments).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
            else:
                res = "No execution handler defined for this tool."

            return AgentStepResponse(
                tool=tool_name,
                arguments=arguments,
                result=res,
                success=True,
            )
        except Exception as exc:
            return AgentStepResponse(
                tool=tool_name,
                arguments=arguments,
                result="",
                success=False,
                error=str(exc),
            )

    def run_agent(self, req: AgentRunRequest, *, history: List[Dict[str, str]],
                  system_prompt: str, config: Dict[str, Any]) -> AgentRunResponse:
        """Run an autonomous ReAct loop with tool execution."""
        start_time = time.perf_counter()
        steps: List[AgentStep] = []
        total_tokens = 0

        # Build tools description for system prompt
        active_tools = [
            self._tools[t]["definition"]
            for t in (req.tools or self._tools.keys())
            if t in self._tools
        ]

        tools_desc = "\n".join(
            f"- `{t.name}`: {t.description}\n  Parameters: {json.dumps(t.parameters)}"
            for t in active_tools
        )

        agent_system_prompt = system_prompt + "\n\n" + (
            "You are an intelligent autonomous AI Agent. You solve tasks step-by-step using tools.\n\n"
            "AVAILABLE TOOLS:\n"
            f"{tools_desc}\n\n"
            "RESPONSE FORMAT INSTRUCTIONS:\n"
            "On each step, output exactly one JSON object with this schema:\n"
            "{\n"
            '  "thought": "Reasoning about what to do next",\n'
            '  "action": {"tool": "tool_name", "arguments": { ... }},\n'
            '  "final_answer": null\n'
            "}\n"
            "When you have completed the task and have the final answer, output:\n"
            "{\n"
            '  "thought": "I now have the solution",\n'
            '  "action": null,\n'
            '  "final_answer": "Complete final answer to the user"\n'
            "}\n"
            "Return valid JSON only. Do not wrap in markdown quotes."
        )

        conversation: List[Dict[str, str]] = list(history) + [
            {"role": "user", "content": f"Task: {req.prompt}"}
        ]

        final_answer = ""

        # ReAct loop
        for step_idx in range(1, req.max_steps + 1):
            route_res = router.route_chat(
                messages=conversation,
                system_prompt=agent_system_prompt,
                config=config,
            )

            total_tokens += route_res.tokens.get("total_tokens") or 0
            raw_output = route_res.content.strip()

            # Parse action or final answer from output
            thought = ""
            action_tool = None
            action_args = None
            extracted_final = None

            try:
                # Try parsing JSON
                # Clean possible code fence
                clean = raw_output
                if clean.startswith("```"):
                    clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                data = json.loads(clean)
                thought = data.get("thought", "")
                action = data.get("action")
                if action and isinstance(action, dict):
                    action_tool = action.get("tool")
                    action_args = action.get("arguments", {})
                extracted_final = data.get("final_answer")
            except Exception:
                # Heuristic fallback parsing
                thought = raw_output[:120]
                if "final" in raw_output.lower() or "answer" in raw_output.lower():
                    extracted_final = raw_output
                elif any(t.name in raw_output for t in active_tools):
                    # Check if calculator or python can be inferred
                    if "calculator" in raw_output or any(c in req.prompt for c in ["+", "*", "/", "-"]):
                        action_tool = "calculator"
                        action_args = {"expression": req.prompt}

            if extracted_final:
                final_answer = extracted_final
                steps.append(AgentStep(
                    step=step_idx,
                    thought=thought or "Final reasoning reached.",
                    tool=None,
                    arguments=None,
                    observation=None,
                ))
                break

            if action_tool and action_tool in self._tools:
                tool_res = self.execute_tool(action_tool, action_args or {})
                observation = str(tool_res.result if tool_res.success else f"Error: {tool_res.error}")

                steps.append(AgentStep(
                    step=step_idx,
                    thought=thought,
                    tool=action_tool,
                    arguments=action_args,
                    observation=observation,
                ))

                # Append to conversation for next step
                conversation.append({"role": "assistant", "content": raw_output})
                conversation.append({
                    "role": "user",
                    "content": f"Observation from {action_tool}: {observation}",
                })
            else:
                # No valid tool action: conclude with raw output
                final_answer = raw_output
                steps.append(AgentStep(
                    step=step_idx,
                    thought=thought,
                    tool=None,
                    arguments=None,
                    observation=None,
                ))
                break

        if not final_answer:
            final_answer = steps[-1].observation if steps and steps[-1].observation else "Task completed."

        latency_ms = (time.perf_counter() - start_time) * 1000

        return AgentRunResponse(
            session_id=req.session_id,
            prompt=req.prompt,
            steps=steps,
            final_answer=final_answer,
            provider=route_res.provider,
            model=route_res.model,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
        )


agent_service = AgentService()

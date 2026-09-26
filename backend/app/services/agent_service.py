"""Agentic tool execution engine, tool registry, and ReAct loop orchestrator."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..schemas.chat import (
    AgentRunRequest,
    AgentRunResponse,
    AgentStep,
    AgentStepResponse,
    AgentToolDefinition,
)
from .router import router
from .skill_manager import SkillManager, skill_manager as persistent_skill_manager

_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


def _agent_envelope(raw_output: str) -> Optional[Dict[str, Any]]:
    """Parse a model's agent envelope, tolerating common Markdown escapes."""
    clean = raw_output.strip()
    if clean.startswith("```"):
        first_newline = clean.find("\n")
        if first_newline >= 0:
            clean = clean[first_newline + 1:]
        if clean.rstrip().endswith("```"):
            clean = clean.rstrip()[:-3].rstrip()

    candidates = [clean]
    first_brace, last_brace = clean.find("{"), clean.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        enclosed = clean[first_brace:last_brace + 1]
        if enclosed != clean:
            candidates.append(enclosed)

    for candidate in candidates:
        # Models sometimes escape Markdown punctuation inside JSON (for example
        # u\_admin or admin\@localhost). Those are invalid JSON escapes, so
        # remove only the non-JSON backslash before parsing.
        repaired = _INVALID_JSON_ESCAPE.sub("", candidate)
        for parser in (json.loads, ast.literal_eval):
            try:
                data = parser(repaired)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
    return None


def _video_block_answer(value: Any) -> Optional[str]:
    """Normalize tool video wrappers to the fenced format used by the chat UI."""
    if isinstance(value, (dict, list)):
        payload = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str):
        payload = value.strip()
        match = re.fullmatch(r"(`{1,3})video\s*\r?\n([\s\S]*?)\r?\n\1", payload)
        if match:
            payload = match.group(2).strip()
    else:
        return None
    try:
        decoded = json.loads(_INVALID_JSON_ESCAPE.sub("", payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    return "```video\n" + json.dumps(decoded, ensure_ascii=False) + "\n```"


def tool_run_python_script(code: str) -> str:
    """Execute Python code in an isolated subprocess with a 5-second timeout."""
    try:
        res = subprocess.run(
            [sys.executable, "-I", "-c", code],
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


class AgentService:
    """Orchestrates tool registration, execution, and ReAct agent loops."""

    def __init__(self, skill_manager: SkillManager | None = None):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self.skill_manager = skill_manager or persistent_skill_manager
        self._register_default_tools()
        self.reload_persisted_skills()

    def _register_default_tools(self) -> None:
        self.register_tool(
            name="run_python_script",
            description="Run a Python script in a separate process and return stdout/stderr.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Valid Python code to execute."}
                },
                "required": ["code"],
            },
            handler=lambda args: tool_run_python_script(args.get("code", "")),
            kind="native",
        )

        self.register_tool(
            name="create_skill_from_conversation",
            description=(
                "Create and persist a reusable Python skill from a pasted conversation. "
                "Translate the conversation into python_code defining run(args), then call this tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Lowercase snake_case skill name."},
                    "description": {"type": "string", "description": "What the skill does."},
                    "parameters": {"type": "object", "description": "JSON Schema for run(args)."},
                    "python_code": {
                        "type": "string",
                        "description": "Python source defining a top-level run(args) function.",
                    },
                    "source_conversation": {
                        "type": "string",
                        "description": "The original pasted conversation used to derive the skill.",
                    },
                },
                "required": ["name", "description", "parameters", "python_code", "source_conversation"],
            },
            handler=self._create_persisted_skill,
            kind="native",
        )

    def _register_persisted_skill(self, record: Dict[str, Any]) -> None:
        name = record["name"]
        self.register_tool(
            name=name,
            description=record["description"],
            parameters=record["parameters"],
            handler=lambda args, skill_name=name: self.skill_manager.execute_skill(skill_name, args),
            kind="persisted",
        )

    def reload_persisted_skills(self) -> None:
        """Reload database-backed skills into the callable registry."""
        native_names = {"run_python_script", "create_skill_from_conversation"}
        for name in list(self._tools):
            if name not in native_names and self._tools[name].get("persisted"):
                del self._tools[name]
        for record in self.skill_manager.list_skills():
            self._register_persisted_skill(record)
            self._tools[record["name"]]["persisted"] = True

    def _create_persisted_skill(self, args: Dict[str, Any]) -> Dict[str, Any]:
        record = self.skill_manager.create_skill(
            name=args.get("name", ""),
            description=args.get("description", ""),
            parameters=args.get("parameters", {}),
            python_code=args.get("python_code", ""),
            source_conversation=args.get("source_conversation", ""),
        )
        self._register_persisted_skill(record)
        self._tools[record["name"]]["persisted"] = True
        return record

    def remove_persisted_skill(self, name: str) -> None:
        entry = self._tools.get(name)
        if entry and entry.get("persisted"):
            del self._tools[name]

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
        endpoint: Optional[str] = None,
        kind: str = "plugin",
    ) -> None:
        """Register a new tool or plugin capability."""
        self._tools[name] = {
            "definition": AgentToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
                kind=kind,
            ),
            "handler": handler,
            "endpoint": endpoint,
            "kind": kind,
        }

    def create_skill_from_conversation(
        self, conversation: str, config: Dict[str, Any], requested_name: str | None = None,
    ) -> Dict[str, Any]:
        """Use the configured model to translate a pasted conversation into a stored skill."""
        name_instruction = (
            f"Use exactly {json.dumps(requested_name)} as the name."
            if requested_name else "Choose a concise lowercase snake_case name."
        )
        system_prompt = (
            "Convert the supplied conversation into one reusable Python skill. "
            "Return exactly one JSON object and no markdown with keys: name, description, "
            "parameters, and python_code. parameters must be a JSON Schema object whose type is object. "
            "python_code must define a top-level synchronous function run(args) and return a JSON-serializable "
            "result. Do not add network access, subprocesses, dynamic code execution, or filesystem writes unless "
            "the pasted conversation explicitly requires them. " + name_instruction
        )
        route_res = router.route_chat(
            messages=[{"role": "user", "content": conversation}],
            system_prompt=system_prompt,
            config=config,
        )
        generated = _agent_envelope(route_res.content)
        if generated is None:
            raise ValueError("The model did not return a valid JSON skill definition.")
        if requested_name:
            generated["name"] = requested_name
        required = ("name", "description", "parameters", "python_code")
        missing = [key for key in required if key not in generated]
        if missing:
            raise ValueError("The generated skill is missing: " + ", ".join(missing) + ".")
        record = self.skill_manager.create_skill(
            name=generated["name"],
            description=generated["description"],
            parameters=generated["parameters"],
            python_code=generated["python_code"],
            source_conversation=conversation,
        )
        self._register_persisted_skill(record)
        self._tools[record["name"]]["persisted"] = True
        return record

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
            "Return valid JSON only. Do not wrap the JSON response in markdown quotes. "
            "If a tool observation contains a fenced `video` block, preserve that entire "
            "block unchanged inside final_answer so the chat UI can render its player. "
            "If it contains a video_block field, use that field's value as final_answer."
        )

        conversation: List[Dict[str, str]] = list(history) + [
            {"role": "user", "content": f"Task: {req.prompt}"}
        ]

        final_answer = ""
        final_retry_limit = config.get("agent_final_retries", 5)
        final_retries = 0
        regular_steps = 0
        step_idx = 0

        # ReAct loop
        while regular_steps < req.max_steps:
            step_idx += 1
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

            data = _agent_envelope(raw_output)
            if data is not None:
                thought = data.get("thought", "")
                action = data.get("action")
                if action and isinstance(action, dict):
                    action_tool = action.get("tool")
                    action_args = action.get("arguments", {})
                extracted_final = data.get("final_answer")
                if extracted_final is None and "video_block" in data:
                    extracted_final = _video_block_answer(data["video_block"])
                if extracted_final is not None and not isinstance(extracted_final, str):
                    extracted_final = json.dumps(extracted_final, ensure_ascii=False)
            else:
                # Heuristic fallback parsing
                thought = raw_output[:120]
                if "final" in raw_output.lower() or "answer" in raw_output.lower():
                    extracted_final = raw_output

            if extracted_final is not None:
                regular_steps += 1
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
                regular_steps += 1
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
            elif data is not None and final_retries < final_retry_limit:
                final_retries += 1
                steps.append(AgentStep(
                    step=step_idx,
                    thought=thought,
                    tool=None,
                    arguments=None,
                    observation=(
                        f"No final answer or registered tool was returned; requesting retry "
                        f"{final_retries} of {final_retry_limit}."
                    ),
                ))
                conversation.append({"role": "assistant", "content": raw_output})
                conversation.append({
                    "role": "user",
                    "content": (
                        "Your previous JSON response had final_answer set to null and did not "
                        "request a registered tool. Respond again with exactly one JSON object. "
                        "If the task is complete, set action to null and final_answer to a "
                        "non-null user-facing string. Otherwise request one available tool by "
                        "its exact name; do not use 'none' as a tool name."
                    ),
                })
            else:
                # Unstructured output, or the configured structured-response retry
                # budget was exhausted: preserve the last response for inspection.
                regular_steps += 1
                final_answer = (
                    "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
                    if data is not None else raw_output
                )
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

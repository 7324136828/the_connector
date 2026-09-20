"""Mock connector for local testing, CI/CD, and demonstration without API keys."""

from __future__ import annotations

import time
from typing import Any, Dict, List
from .common import record_token_usage, estimate_tokens


def mock_chat(
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str = "",
) -> str:
    """Generate a realistic mock response for testing."""
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    # Calculate token metrics
    input_text = f"{system_prompt}\n" + "\n".join(m.get("content", "") for m in messages)
    input_tokens = estimate_tokens(input_text)

    # Dynamic reply based on prompt
    query = last_user_msg.lower().strip()
    if "hello" in query or "hi" in query or "hey" in query:
        reply = (
            f"Hello! I'm running on The Connector using `{model}` (Mock Mode). "
            "All routing logic, session context window management, and agent tools are fully functional! "
            "How can I help you today?"
        )
    elif "who are you" in query:
        reply = (
            f"I am an AI assistant powered by The Connector architecture (`{model}`). "
            "You can configure real providers (OpenAI, Claude, Gemini, Ollama, OpenRouter) in `.env` "
            "or test custom probability sequences in `config.json`!"
        )
    elif "help" in query:
        reply = (
            "Here is what you can do with The Connector:\n\n"
            "- **Multi-Provider Support**: Switch between OpenAI, Claude, Gemini, Ollama, and OpenRouter.\n"
            "- **Intelligent Routing**: Use `config.json` for weighted probability and automatic fallbacks.\n"
            "- **Memory Control**: Toggle past conversation memory on/off.\n"
            "- **Agentic Tools**: Ask me to calculate math or run code in Agent Mode!"
        )
    elif any(op in query for op in ["+", "-", "*", "/", "calculate", "solve", "math"]):
        reply = (
            f"I noticed a computation request in your prompt: '{last_user_msg}'. "
            "In Agentic Mode, I can dispatch our built-in `calculator` or `python_interpreter` tools."
        )
    else:
        reply = (
            f"Received your message: \"{last_user_msg}\"\n\n"
            f"This response was routed through `{model}` via The Connector. "
            "Your conversation history and context window are actively managed according to your session settings."
        )

    output_tokens = estimate_tokens(reply)
    record_token_usage(input_tokens, output_tokens)
    return reply

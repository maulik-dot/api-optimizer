#!/usr/bin/env python3
"""
Thin client for the IndiaMart LiteLLM proxy at imllm.intermesh.net.
Fully OpenAI-compatible — uses requests, no extra SDK needed.

Set env vars:
  IMLLM_API_KEY   — your API key (required)
  IMLLM_BASE_URL  — override base URL (default: https://imllm.intermesh.net)
  IMLLM_MODEL     — default model to use when not specified
"""

import os
import json
import requests
from typing import Optional

BASE_URL  = os.getenv("IMLLM_BASE_URL", "https://imllm.intermesh.net").rstrip("/")
API_KEY   = os.getenv("IMLLM_API_KEY", "")
DEFAULT_MODEL = os.getenv("IMLLM_MODEL", "")


def _headers() -> dict:
    if not API_KEY:
        raise EnvironmentError(
            "IMLLM_API_KEY is not set.\n"
            "  export IMLLM_API_KEY=<your key>\n"
            "  or pass --api-key <key> on the command line."
        )
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type":  "application/json",
    }


def list_models() -> list[dict]:
    """Return all models available on the gateway."""
    resp = requests.get(f"{BASE_URL}/models", headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # OpenAI-style: {"object":"list","data":[{"id":"...", ...}]}
    return data.get("data", data) if isinstance(data, dict) else data


def chat(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    response_format: Optional[dict] = None,
    stream: bool = False,
) -> str:
    """
    Send a chat completion request and return the assistant's message content.
    Uses model param, then IMLLM_MODEL env var, then raises if neither is set.
    """
    chosen_model = model or DEFAULT_MODEL
    if not chosen_model:
        raise ValueError(
            "No model specified. Pass --model <name> or set IMLLM_MODEL env var.\n"
            "Run with --list-models to see available options."
        )

    payload: dict = {
        "model":       chosen_model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers=_headers(),
        json=payload,
        timeout=120,
        stream=stream,
    )

    if resp.status_code == 401:
        raise PermissionError("API key rejected (HTTP 401). Check IMLLM_API_KEY.")
    if resp.status_code == 404:
        raise ValueError(f"Model '{chosen_model}' not found (HTTP 404). Run --list-models.")
    resp.raise_for_status()

    return resp.json()["choices"][0]["message"]["content"]


def chat_tools(
    messages: list[dict],
    tools: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict:
    """Send chat with tool definitions. Returns the raw assistant message dict (may contain tool_calls)."""
    chosen_model = model or DEFAULT_MODEL
    if not chosen_model:
        raise ValueError(
            "No model specified. Pass --model <name> or set IMLLM_MODEL env var.\n"
            "Run with --list-models to see available options."
        )
    payload: dict = {
        "model":       chosen_model,
        "messages":    messages,
        "tools":       tools,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    resp = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers=_headers(),
        json=payload,
        timeout=120,
    )
    if resp.status_code == 401:
        raise PermissionError("API key rejected (HTTP 401). Check IMLLM_API_KEY.")
    if resp.status_code == 404:
        raise ValueError(f"Model '{chosen_model}' not found (HTTP 404). Run --list-models.")
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def prune_tool_results(messages: list[dict], keep_last: int = 6) -> list[dict]:
    """
    Trim old tool-call/tool-result pairs from a long agent conversation to stay
    within the context window while preserving the system prompt, the first user
    message, and the most recent `keep_last` tool-exchange pairs.

    Call this before every chat_tools() invocation in long agentic loops.
    """
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # First user message is the task description — always keep it
    first_user_idx = next((i for i, m in enumerate(non_system) if m.get("role") == "user"), None)
    if first_user_idx is None:
        return messages

    anchor = non_system[: first_user_idx + 1]
    tail   = non_system[first_user_idx + 1 :]

    # Identify tool exchange boundaries: assistant (with tool_calls) + tool result(s)
    pairs: list[list[dict]] = []
    buf: list[dict] = []
    for m in tail:
        buf.append(m)
        if m.get("role") == "tool":
            pairs.append(buf)
            buf = []
    leftover = buf  # partial exchange or plain assistant message at the end

    kept_pairs = pairs[-keep_last:] if len(pairs) > keep_last else pairs
    pruned     = [m for p in kept_pairs for m in p]
    return system + anchor + pruned + leftover


def chat_json(messages: list[dict], model: Optional[str] = None, **kwargs) -> dict:
    """Like chat() but parses and returns a JSON object. Retries once on parse failure."""
    raw = chat(messages, model=model, **kwargs)
    # Strip markdown code fences if the model wrapped the JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Second attempt: find the first { ... } block
        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end])
        raise ValueError(f"LLM did not return valid JSON.\nRaw output:\n{raw[:500]}")

"""Ollama streaming client with a tool round trip.

Ollama's /api/chat streams newline-delimited JSON, not SSE, and delivers tool
calls whole rather than as argument deltas -- simpler than the OpenAI wire
format, so this is hand-rolled rather than pulling in an SDK.

The loop: stream a turn; if it ends in tool calls, run them, append the results,
and stream again. Bounded by MAX_TOOL_ROUNDS so a model that keeps calling tools
cannot hang the request.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from api.config import get_settings

SETTINGS = get_settings()
log = logging.getLogger("llm")

# Two rounds covers get_available_slots -> book_meeting. More than that is a
# loop, not a plan.
MAX_TOOL_ROUNDS = 3


class Delta:
    """One streamed event: text, tool calls, or end of turn."""

    __slots__ = ("text", "tool_calls", "done")

    def __init__(
        self,
        text: str = "",
        tool_calls: list[dict] | None = None,
        done: bool = False,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.done = done


async def stream_turn(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
) -> AsyncIterator[Delta]:
    """Stream one assistant turn from Ollama."""
    payload: dict[str, Any] = {
        "model": model or SETTINGS.llm_model_large,
        "messages": messages,
        "stream": True,
        "options": {
            # Low but not zero: consistent sales language, still natural.
            "temperature": 0.4,
            "num_ctx": 65536,
        },
    }
    if tools:
        payload["tools"] = tools

    url = f"{SETTINGS.ollama_base_url.rstrip('/')}/api/chat"
    timeout = httpx.Timeout(SETTINGS.llm_timeout_seconds, connect=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")
                    log.error("ollama %s: %s", response.status_code, body[:300])
                    yield Delta(
                        text="I'm having trouble reaching my knowledge right now. "
                        "Could a specialist email you instead?",
                        done=True,
                    )
                    return

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = chunk.get("message") or {}
                    text = message.get("content") or ""
                    calls = message.get("tool_calls") or []

                    if text or calls:
                        yield Delta(text=text, tool_calls=calls)
                    if chunk.get("done"):
                        yield Delta(done=True)
                        return
    except httpx.TimeoutException:
        log.warning("ollama timed out after %ss", SETTINGS.llm_timeout_seconds)
        yield Delta(
            text="That's taking longer than it should. Shall I have someone follow up by email?",
            done=True,
        )
    except Exception as exc:
        log.exception("ollama stream failed")
        yield Delta(
            text="Something went wrong on my side. A specialist can pick this up -- "
            "what's the best email for you?",
            done=True,
        )


def normalise_tool_call(call: dict) -> tuple[str, dict]:
    """Pull (name, args) out of a tool call, tolerating shape differences.

    Ollama returns arguments as an object; some models emit a JSON string.
    """
    fn = call.get("function") or call
    name = fn.get("name") or ""
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return name, args if isinstance(args, dict) else {}


async def probe() -> dict[str, Any]:
    """Is the model reachable and tool-capable? Used by /api/health."""
    url = f"{SETTINGS.ollama_base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(url)
            models = [m["name"] for m in res.json().get("models", [])]
            wanted = SETTINGS.llm_model_large
            return {
                "reachable": True,
                "model": wanted,
                # Cloud models are served without a local pull, so absence from
                # the list is not an error.
                "pulled_locally": wanted in models,
            }
    except Exception as exc:
        return {"reachable": False, "model": SETTINGS.llm_model_large, "error": str(exc)}

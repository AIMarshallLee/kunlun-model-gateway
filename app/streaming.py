"""SSE forwarding helpers that retain usage counters, never generated content."""

from __future__ import annotations

import json
import math
import time
from typing import Any, AsyncIterator


class StreamProtocolError(RuntimeError):
    pass


class SSEUsageTracker:
    def __init__(self, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.output_characters = 0
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.done = False
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.max_bytes:
            raise StreamProtocolError("provider_stream_too_large")
        self._buffer += chunk
        if len(self._buffer) > 1024 * 1024 and b"\n" not in self._buffer:
            raise StreamProtocolError("provider_stream_line_too_large")
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._parse_line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buffer.strip():
            self._parse_line(self._buffer.rstrip(b"\r"))
        self._buffer = b""

    def _parse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.done = True
            return
        if not data:
            return
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StreamProtocolError("provider_stream_invalid_json") from exc
        if not isinstance(event, dict):
            raise StreamProtocolError("provider_stream_invalid_event")
        usage = event.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0:
                self.prompt_tokens = prompt
            if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
                self.completion_tokens = completion
        choices = event.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                self.output_characters += len(content)
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else None
                    arguments = function.get("arguments") if isinstance(function, dict) else None
                    if isinstance(arguments, str):
                        self.output_characters += len(arguments)

    def settlement_response(self, model: str, fallback_input_tokens: int) -> tuple[dict[str, Any], bool]:
        actual = self.prompt_tokens is not None and self.completion_tokens is not None
        prompt_tokens = self.prompt_tokens if self.prompt_tokens is not None else fallback_input_tokens
        completion_tokens = self.completion_tokens
        if completion_tokens is None:
            completion_tokens = math.ceil(self.output_characters / 4) if self.output_characters else 0
        return ({
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }, not actual)


async def synthesize_sse(response: dict[str, Any], request_id: str, model: str) -> AsyncIterator[bytes]:
    completion_id = str(response.get("id") or f"chatcmpl_{request_id.replace('-', '')}")
    created = int(response.get("created") or time.time())
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    for index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        delta: dict[str, Any] = {"role": message.get("role", "assistant")}
        if "content" in message:
            delta["content"] = message.get("content")
        if "tool_calls" in message:
            delta["tool_calls"] = message.get("tool_calls")
        event = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": response.get("model") or model,
            "choices": [{"index": choice.get("index", index), "delta": delta, "finish_reason": None}],
        }
        yield ("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
        finish = {
            **event,
            "choices": [{
                "index": choice.get("index", index),
                "delta": {},
                "finish_reason": choice.get("finish_reason", "stop"),
            }],
        }
        yield ("data: " + json.dumps(finish, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
    usage = response.get("usage")
    if isinstance(usage, dict):
        usage_event = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": response.get("model") or model,
            "choices": [],
            "usage": usage,
        }
        yield ("data: " + json.dumps(usage_event, separators=(",", ":")) + "\n\n").encode()
    yield b"data: [DONE]\n\n"

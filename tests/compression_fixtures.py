"""Synthetic observations and provider wire responses for compression checks."""

import copy
import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from headroom_kit.runtime import Json

FIXTURES = {
    "json": json.dumps(
        [
            {"id": i, "status": "ok", "region": "test", "description": "repeated observation"}
            for i in range(120)
        ],
        indent=2,
    ),
    "search": "\n".join(f"src/module_{i}.py:{i + 1}: result = process(value)" for i in range(150)),
    "build": "\n".join(
        ["INFO compile: module completed successfully"] * 200
        + ["ERROR test failed: expected 3, got 2"]
    ),
    "short": "ok",
    "read": "# Preserve these exact bytes for editing\n"
    + "\n".join(f"def function_{i}(value):\n    return value + {i}\n" for i in range(90)),
}
LOSSY_LOG = "\n".join(f"WARN worker_{i:04d}: unique observation {i:04d}" for i in range(180))
RETRIEVAL_MARKER = re.compile(r"(?:<<ccr:|Retrieve more: hash=)([a-f0-9]+)")
ROUTES = {"responses": "/v1/responses", "chat": "/v1/chat/completions", "anthropic": "/v1/messages"}


def request_body(route: str, text: str, fixture: str, stream: bool) -> dict:
    name = "Read" if fixture == "read" else "Bash"
    arguments = (
        {"file_path": "src/fixture.py"} if name == "Read" else {"command": "inspect fixture"}
    )
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in arguments},
        "required": list(arguments),
    }
    tool = {
        "name": name,
        "description": "Read a source file." if name == "Read" else "Run a shell command.",
        "parameters": schema,
    }
    prompt = {
        "role": "user",
        "content": f"Inspect the {fixture} fixture ({route}, stream={stream}). Report errors.",
    }
    body = {"model": "claude-sonnet-4-6" if route == "anthropic" else "gpt-4o", "stream": stream}
    if route == "responses":
        body.update(
            tools=[dict(type="function", **tool)],
            input=[
                prompt,
                {
                    "type": "function_call",
                    "call_id": "call_fixture",
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
                {"type": "function_call_output", "call_id": "call_fixture", "output": text},
            ],
        )
    elif route == "chat":
        body.update(
            tools=[{"type": "function", "function": tool}],
            messages=[
                prompt,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fixture",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_fixture", "content": text},
            ],
        )
    else:
        body.update(
            max_tokens=100,
            tools=[{"name": name, "description": tool["description"], "input_schema": schema}],
            messages=[
                prompt,
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_fixture", "name": name, "input": arguments}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_fixture", "content": text}
                    ],
                },
            ],
        )
    return body


def response_body(route: str, hash_key: str | None = None) -> dict:
    arguments = {"hash": hash_key, "max_tokens": 20000}
    if route == "responses":
        output = (
            [
                {
                    "type": "function_call",
                    "id": "fc_retrieve",
                    "call_id": "call_retrieve",
                    "name": "headroom_retrieve",
                    "arguments": json.dumps(arguments),
                }
            ]
            if hash_key
            else [
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "fixture complete", "annotations": []}
                    ],
                }
            ]
        )
        return {
            "id": "resp_fixture",
            "object": "response",
            "status": "completed",
            "model": "gpt-4o",
            "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 5},
        }
    if route == "chat":
        message = {"role": "assistant", "content": "fixture complete"}
        if hash_key:
            message.update(
                content=None,
                tool_calls=[
                    {
                        "id": "call_retrieve",
                        "type": "function",
                        "function": {
                            "name": "headroom_retrieve",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            )
        return {
            "id": "chat_fixture",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if hash_key else "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        }
    content = (
        [
            {
                "type": "tool_use",
                "id": "call_retrieve",
                "name": "headroom_retrieve",
                "input": arguments,
            }
        ]
        if hash_key
        else [{"type": "text", "text": "fixture complete"}]
    )
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": content,
        "stop_reason": "tool_use" if hash_key else "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 5},
    }


def stream_body(route: str) -> str:
    if route == "responses":
        return (
            "event: response.completed\ndata: "
            + json.dumps({"type": "response.completed", "response": response_body(route)})
            + "\n\n"
        )
    if route == "chat":
        chunk = {
            "id": "chat_fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "fixture complete"},
                    "finish_reason": None,
                }
            ],
        }
        stop = copy.deepcopy(chunk)
        stop["choices"][0].update(delta={}, finish_reason="stop")
        return f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(stop)}\n\ndata: [DONE]\n\n"
    events = [
        {"type": "message_start", "message": dict(response_body(route), content=[])},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "fixture complete"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


@dataclass
class Upstream:
    url: str = ""
    calls: list[dict[str, Json]] = field(default_factory=list)
    retrievals: list[str] = field(default_factory=list)
    received_at: float = 0

    def reset(self) -> None:
        self.calls.clear()
        self.retrievals.clear()
        self.received_at = 0

    def reply(self, route: str, body: dict[str, Json]) -> tuple[bytes, str]:
        self.calls.append(body)
        if len(self.calls) == 1:
            self.received_at = time.perf_counter()
            markers = RETRIEVAL_MARKER.findall(json.dumps(body))
            if markers and "headroom_retrieve" in json.dumps(body.get("tools", [])):
                self.retrievals.append(markers[0])
                return json.dumps(response_body(route, markers[0])).encode(), "application/json"
        if body.get("stream"):
            return stream_body(route).encode(), "text/event-stream"
        return json.dumps(response_body(route)).encode(), "application/json"


@contextmanager
def upstream() -> Iterator[Upstream]:
    recording = Upstream()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            route = next(name for name, path in ROUTES.items() if self.path.endswith(path))
            data, content_type = recording.reply(route, body)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: str | int) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    recording.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield recording
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

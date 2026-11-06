"""A fake OpenAI-compatible chat completions server, for Hermes profiles to use as their model.

It serves ``GET /v1/models`` and ``POST /v1/chat/completions``, both as one JSON reply and, for
``"stream": true``, as server-sent chunks ending with ``data: [DONE]``. Every completion answers
with plain text and no tool calls: ``replies[model]`` when the request's model has one, else
``"<model> says: done"``. Every completion request body is appended to ``record_path`` as one JSON
line, so a test in another process can see which model and ``reasoning_effort`` Hermes asked for.

Run it as a module; it prints ``listening on <base URL>`` once it accepts requests::

    python -m tests.fakes.fake_llm --port 0 --record calls.jsonl --reply "stock-picker-medium=portfolio up 2%"
"""

import argparse
import asyncio
import itertools
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiohttp import web


def reply_text(model: str, replies: Mapping[str, str]) -> str:
    """The text the fake answers ``model`` with."""
    return replies.get(model, f"{model} says: done")


def make_app(record_path: Path, replies: Mapping[str, str]) -> web.Application:
    """The fake's aiohttp application; it appends each completion request to ``record_path``."""
    completion_ids = (f"chatcmpl-fake{n}" for n in itertools.count(1))

    async def models(request: web.Request) -> web.Response:
        data = [{"id": model, "object": "model", "created": 0, "owned_by": "fake"} for model in replies]
        return web.json_response({"object": "list", "data": data})

    async def chat_completions(request: web.Request) -> web.StreamResponse:
        body: dict[str, Any] = await request.json()
        with record_path.open("a", encoding="utf-8") as record:
            record.write(json.dumps(body) + "\n")
        model = str(body.get("model", ""))
        text = reply_text(model, replies)
        base = {"id": next(completion_ids), "created": int(time.time()), "model": model}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            message = {"role": "assistant", "content": text}
            choice = {"index": 0, "message": message, "finish_reason": "stop"}
            return web.json_response(
                base | {"object": "chat.completion", "choices": [choice], "usage": usage}
            )

        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        chunk = base | {"object": "chat.completion.chunk"}
        deltas = [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": "stop"},
        ]
        for choice in deltas:
            await response.write(f"data: {json.dumps(chunk | {'choices': [choice]})}\n\n".encode())
        if body.get("stream_options", {}).get("include_usage"):
            await response.write(f"data: {json.dumps(chunk | {'choices': [], 'usage': usage})}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.add_routes([web.get("/v1/models", models), web.post("/v1/chat/completions", chat_completions)])
    return app


async def serve(port: int, record_path: Path, replies: Mapping[str, str]) -> None:
    """Serve on 127.0.0.1:``port`` (0 picks a free port) until cancelled."""
    runner = web.AppRunner(make_app(record_path, replies))
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
        bound_port = runner.addresses[0][1]
        print(f"listening on http://127.0.0.1:{bound_port}/v1", flush=True)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="A fake OpenAI-compatible chat completions server.")
    parser.add_argument("--port", type=int, default=0, help="port to listen on; 0 picks a free one")
    parser.add_argument("--record", type=Path, required=True, help="JSONL file to append requests to")
    parser.add_argument(
        "--reply", action="append", default=[], metavar="MODEL=TEXT", help="what to answer MODEL with"
    )
    args = parser.parse_args()
    replies = dict(reply.split("=", 1) for reply in args.reply)
    asyncio.run(serve(args.port, args.record, replies))


if __name__ == "__main__":
    main()

"""No-model check for middleware wrapper nesting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

request_trace: list[str] = []


class TraceMiddleware(AgentMiddleware):
    def __init__(self, name: str, events: list[str]) -> None:
        self.trace_name = name
        self.events = events

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        self.events.append(f"{self.trace_name}:before")
        response = await handler(request)
        self.events.append(f"{self.trace_name}:after")
        return response


async def fake_model(request: Any) -> str:
    del request
    trace = request_trace
    trace.append("model")
    return "ok"


async def main() -> None:
    request_trace.clear()
    outer = TraceMiddleware("outer", request_trace)
    inner = TraceMiddleware("inner", request_trace)

    async def composed(request: Any) -> str:
        return await outer.awrap_model_call(
            request,
            lambda request: inner.awrap_model_call(request, fake_model),
        )

    result = await composed(object())
    expected = ["outer:before", "inner:before", "model", "inner:after", "outer:after"]
    assert result == "ok"
    assert request_trace == expected, request_trace
    print(" -> ".join(request_trace))


if __name__ == "__main__":
    asyncio.run(main())

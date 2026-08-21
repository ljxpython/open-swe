"""第 4 章最小验证：fallback、单次模型 deadline 和 wrapup 指令。"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from agent.middleware.model_call_timeout import ModelCallTimeoutError, ModelCallTimeoutMiddleware
from agent.middleware.model_fallback import ModelFallbackMiddleware
from agent.middleware.timeout_wrapup import TimeoutWrapupMiddleware


async def main() -> None:
    fallback_model = MagicMock(name="fallback-model")
    fallback_request = MagicMock(name="fallback-request")
    request = MagicMock(name="primary-request")
    request.override.return_value = fallback_request
    calls: list[object] = []

    async def fallback_handler(current: ModelRequest) -> ModelResponse[Any]:
        calls.append(current)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary outage")
        return cast(ModelResponse[Any], MagicMock())

    await ModelFallbackMiddleware(fallback_model, backoff_schedule=(0.0,)).awrap_model_call(
        cast(ModelRequest, request), fallback_handler
    )
    assert calls == [request, fallback_request]
    print("fallback attempts: primary -> fallback")

    async def stalled_handler(_request: ModelRequest) -> ModelResponse[Any]:
        await asyncio.sleep(1)
        raise AssertionError("deadline should cancel this handler")

    try:
        await ModelCallTimeoutMiddleware(timeout_seconds=0.01).awrap_model_call(
            cast(ModelRequest, request), stalled_handler
        )
    except ModelCallTimeoutError:
        print("model deadline converted: True")
    else:
        raise AssertionError("expected ModelCallTimeoutError")

    wrapup = TimeoutWrapupMiddleware(timeout_seconds=1)
    wrapup._start = 0.0
    seen: dict[str, str] = {}
    model_request = ModelRequest(
        model=cast(Any, object()), messages=[], system_message=SystemMessage(content="base")
    )

    async def wrapup_handler(current: ModelRequest) -> ModelResponse[Any]:
        assert current.system_message is not None
        seen["system"] = str(current.system_message.content)
        return ModelResponse(result=[AIMessage(content="ok")])

    await wrapup.awrap_model_call(model_request, wrapup_handler)
    assert "time_limit_warning" in seen["system"]
    print("wrapup instruction added: True")


if __name__ == "__main__":
    asyncio.run(main())

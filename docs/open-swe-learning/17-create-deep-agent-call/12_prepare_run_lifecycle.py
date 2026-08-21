"""第 2 章最小验证：PrepareRun 的 fingerprint 闩锁和 prompt 注入。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from langchain.agents.middleware.types import AgentState, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from agent.middleware.prepare_run import BasePrepareRunMiddleware


class DemoPrepareMiddleware(BasePrepareRunMiddleware):
    def __init__(self) -> None:
        self.calls = 0

    async def _prepare(self, state: AgentState, runtime: Runtime) -> dict[str, Any]:
        self.calls += 1
        return {
            "work_dir": "/tmp/demo-work",
            "rendered_system_prompt": "prepared prompt",
        }


class DemoRequest:
    def __init__(self, state: AgentState, system_message: SystemMessage | None = None) -> None:
        self.state = state
        self.system_message = system_message

    def override(self, *, system_message: SystemMessage) -> DemoRequest:
        return DemoRequest(self.state, system_message)


async def main() -> None:
    middleware = DemoPrepareMiddleware()
    runtime = cast(Runtime, object())
    first_state = cast(AgentState, {"messages": [HumanMessage("first", id="turn-1")]})

    update = await middleware.abefore_agent(first_state, runtime)
    assert update is not None
    prepared_state = {**first_state, **update}
    print(f"first prepare: {middleware.calls}")

    skipped = await middleware.abefore_agent(cast(AgentState, prepared_state), runtime)
    assert skipped is None
    print(f"same fingerprint skipped: {middleware.calls}")

    second_state = cast(
        AgentState,
        {**prepared_state, "messages": [HumanMessage("second", id="turn-2")]},
    )
    await middleware.abefore_agent(second_state, runtime)
    print(f"new message prepared: {middleware.calls}")

    seen: dict[str, str] = {}

    async def handler(request: DemoRequest) -> ModelResponse:
        assert request.system_message is not None
        seen["prompt"] = request.system_message.text
        return cast(ModelResponse, object())

    request = DemoRequest(cast(AgentState, prepared_state))
    await middleware.awrap_model_call(request, handler)
    assert seen["prompt"] == "prepared prompt"
    print(f"injected system prompt: {seen['prompt']}")


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import logging
import operator
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, NotRequired

from deepagents import DeepAgentState, create_deep_agent
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

'''
uv run python docs/open-swe-learning/17-create-deep-agent-call/10_middleware_basics.py

同步模式：
MIDDLEWARE_MODE=sync uv run python docs/open-swe-learning/17-create-deep-agent-call/10_middleware_basics.py
'''

class LifecycleState(DeepAgentState):
    # operator.add 让每个钩子提交的列表追加到既有 state，而不是覆盖它。
    lifecycle_events: NotRequired[Annotated[list[str], operator.add]]


class LifecycleAuditMiddleware(AgentMiddleware[LifecycleState, None, Any]):
    state_schema = LifecycleState

    @staticmethod
    def _tool_names(request: ModelRequest[None]) -> list[str]:
        names: list[str] = []
        for item in request.tools:
            if isinstance(item, dict):
                name = item.get("name")
            else:
                name = getattr(item, "name", None)
            names.append(str(name or "anonymous"))
        return names

    @staticmethod
    def _state_summary(state: LifecycleState) -> str:
        return (
            f"messages={len(state.get('messages', []))} "
            f"events={len(state.get('lifecycle_events', []))}"
        )

    @staticmethod
    def _response_tool_names(response: Any) -> list[str]:
        result = getattr(response, "result", response)
        messages = result if isinstance(result, list) else [result]
        names: list[str] = []
        for message in messages:
            for tool_call in getattr(message, "tool_calls", []) or []:
                if isinstance(tool_call, dict) and tool_call.get("name"):
                    names.append(str(tool_call["name"]))
        return names

    @staticmethod
    def _tool_call_summary(request: ToolCallRequest) -> str:
        tool_call = request.tool_call
        args = tool_call.get("args")
        arg_keys = sorted(args) if isinstance(args, dict) else []
        return (
            f"name={tool_call.get('name')} id={tool_call.get('id')} "
            f"arg_keys={arg_keys}"
        )

    def before_agent(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        logger.info("[sync] before_agent: %s", self._state_summary(state))
        return {"lifecycle_events": ["before_agent: run started"]}

    async def abefore_agent(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        logger.info("[async] before_agent: %s", self._state_summary(state))
        return {"lifecycle_events": ["before_agent: run started"]}

    def before_model(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        logger.info("[sync] before_model: %s", self._state_summary(state))
        return {"lifecycle_events": ["before_model: next model turn"]}

    async def abefore_model(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        logger.info("[async] before_model: %s", self._state_summary(state))
        return {"lifecycle_events": ["before_model: next model turn"]}

    def wrap_model_call(
        self,
        request: ModelRequest[None],
        handler: Callable[[ModelRequest[None]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        tool_names = self._tool_names(request)
        model_name = getattr(request.model, "model_name", type(request.model).__name__)
        logger.info(
            "[sync] model_call_start: model=%s messages=%s visible_tools=%s",
            model_name,
            len(request.messages),
            tool_names,
        )
        started_at = time.monotonic()
        try:
            response = handler(request)
            logger.info(
                "[sync] model_call_response: type=%s tool_calls=%s",
                type(response).__name__,
                self._response_tool_names(response),
            )
            return response
        except Exception:
            logger.exception("[sync] model_call_error")
            raise
        finally:
            elapsed = time.monotonic() - started_at
            logger.info("[sync] model_call_end: elapsed=%.2fs", elapsed)

    async def awrap_model_call(
        self,
        request: ModelRequest[None],
        handler: Callable[[ModelRequest[None]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        tool_names = self._tool_names(request)
        model_name = getattr(request.model, "model_name", type(request.model).__name__)
        logger.info(
            "[async] model_call_start: model=%s messages=%s visible_tools=%s",
            model_name,
            len(request.messages),
            tool_names,
        )
        started_at = time.monotonic()
        try:
            response = await handler(request)
            logger.info(
                "[async] model_call_response: type=%s tool_calls=%s",
                type(response).__name__,
                self._response_tool_names(response),
            )
            return response
        except Exception:
            logger.exception("[async] model_call_error")
            raise
        finally:
            elapsed = time.monotonic() - started_at
            logger.info("[async] model_call_end: elapsed=%.2fs", elapsed)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call["name"]
        logger.info("[sync] tool_call_start: %s", self._tool_call_summary(request))
        if tool_name == "dangerous_delete":
            logger.warning("[sync] tool_call_blocked: name=%s", tool_name)
            return ToolMessage(
                content="dangerous_delete is disabled by LifecycleAuditMiddleware",
                tool_call_id=request.tool_call["id"],
                status="error",
            )

        started_at = time.monotonic()
        try:
            result = handler(request)
            logger.info(
                "[sync] tool_call_end: name=%s result_type=%s status=%s elapsed=%.2fs",
                tool_name,
                type(result).__name__,
                getattr(result, "status", "command"),
                time.monotonic() - started_at,
            )
            return result
        except Exception:
            logger.exception("[sync] tool_call_error: name=%s", tool_name)
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call["name"]
        logger.info("[async] tool_call_start: %s", self._tool_call_summary(request))
        if tool_name == "dangerous_delete":
            logger.warning("[async] tool_call_blocked: name=%s", tool_name)
            return ToolMessage(
                content="dangerous_delete is disabled by LifecycleAuditMiddleware",
                tool_call_id=request.tool_call["id"],
                status="error",
            )

        started_at = time.monotonic()
        try:
            result = await handler(request)
            logger.info(
                "[async] tool_call_end: name=%s result_type=%s status=%s elapsed=%.2fs",
                tool_name,
                type(result).__name__,
                getattr(result, "status", "command"),
                time.monotonic() - started_at,
            )
            return result
        except Exception:
            logger.exception("[async] tool_call_error: name=%s", tool_name)
            raise

    def after_agent(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        event_count = len(state.get("lifecycle_events", []))
        logger.info("[sync] after_agent: %s events_before_append=%s", self._state_summary(state), event_count)
        return {"lifecycle_events": ["after_agent: run finished"]}

    async def aafter_agent(
        self,
        state: LifecycleState,
        runtime: Runtime[None],
    ) -> dict[str, Any]:
        event_count = len(state.get("lifecycle_events", []))
        logger.info("[async] after_agent: %s events_before_append=%s", self._state_summary(state), event_count)
        return {"lifecycle_events": ["after_agent: run finished"]}


@tool
def get_status() -> str:
    """Return a harmless status value."""
    return "ok"


model_name = os.environ.get("MIDDLEWARE_MODEL_NAME", "DeepSeek-V4-Flash")
deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL")
deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY")
if not deepseek_base_url or not deepseek_api_key:
    raise RuntimeError("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required")

model = ChatOpenAI(
    model=model_name,
    base_url=deepseek_base_url.rstrip("/"),
    api_key=deepseek_api_key,
    use_responses_api=False,
    max_tokens=512,
    temperature=0,
)

agent = create_deep_agent(
    model=model,
    tools=[get_status],
    middleware=[LifecycleAuditMiddleware()],
    state_schema=LifecycleState,
)


async def main() -> None:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "调用 get_status，然后说明结果"}]}
    )
    print(result["lifecycle_events"])


def run_sync() -> None:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "调用 get_status，然后说明结果"}]}
    )
    print(result["lifecycle_events"])


if __name__ == "__main__":
    if os.environ.get("MIDDLEWARE_MODE", "async").lower() == "sync":
        run_sync()
    else:
        import asyncio

        asyncio.run(main())

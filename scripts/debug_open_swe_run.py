"""Start one local Open SWE Agent run and print its LangGraph stream events."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from langgraph_sdk import get_client

DEFAULT_URL = "http://localhost:2024"
DEFAULT_MODEL = "openai:DeepSeek-V4-Flash"
DEFAULT_EFFORT = "high"
DEFAULT_PROMPT = (
    "Use read_file to read agent/utils/tracing.py, then explain its purpose. "
    "Do not modify files, run shell commands, or call network tools."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("LANGGRAPH_URL", DEFAULT_URL))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--github-login",
        default=os.environ.get("OPEN_SWE_DEBUG_GITHUB_LOGIN"),
        help="GitHub login that has completed the local Dashboard OAuth flow",
    )
    args = parser.parse_args()
    if not args.github_login:
        parser.error("--github-login or OPEN_SWE_DEBUG_GITHUB_LOGIN is required")
    return args


def print_event(event: str, data: Any) -> None:
    print(f"\n=== {event} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def main() -> None:
    args = parse_args()
    client = get_client(url=args.url)
    thread = await client.threads.create(
        metadata={"source": "dashboard", "debug": True, "github_login": args.github_login}
    )
    thread_id = thread["thread_id"]
    print(f"thread_id={thread_id}")
    print(f"model={args.model} effort={args.effort}")

    config = {
        "configurable": {
            "__is_for_execution__": True,
            "source": "dashboard",
            "github_login": args.github_login,
            "agent_model_id": args.model,
            "agent_effort": args.effort,
        }
    }
    async for part in client.runs.stream(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": args.prompt}]},
        config=config,
        stream_mode=["messages", "updates", "events"],
        stream_resumable=True,
    ):
        print_event(part.event, part.data)


if __name__ == "__main__":
    asyncio.run(main())

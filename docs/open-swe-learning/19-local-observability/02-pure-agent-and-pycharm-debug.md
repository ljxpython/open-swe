# 第 2 章：纯 Agent、`langgraph dev` 与 PyCharm 调试

## 学习目标

本章给出两种本地调试方式，并回答该选哪一种：

| 目标 | 方式 | 是否需要 `langgraph dev` |
| --- | --- | --- |
| 看模型的每轮逻辑 prompt、工具 schema/参数和模型响应 | 单进程最小 Deep Agent 脚本，在 PyCharm Debug 运行 | 否 |
| 验证当前 Open SWE 的 thread、Store、sandbox、checkpoint、Run 与 SSE | `langgraph dev` 启动 Runtime，再用 SDK 或 Dashboard 发起 Run | 是 |

**推荐顺序**：学习本项目的真实运行链路时，先用第 3 节的 Runtime 路径；只想隔离模型或单个工具时，再用单进程最小 Agent。`get_agent()` 本身不是隔离实验入口，硬绕过它的前置条件只会制造与生产路径不同的假象。

## 1. 为什么不能把完整 `get_agent()` 当普通函数直接运行

`agent.server:get_agent()` 在执行模式要求：

```python
config["configurable"]["thread_id"]
config["configurable"]["__is_for_execution__"] is True
```

第二个条件由 [`agent/runtime/execution.py`](../../../agent/runtime/execution.py) 的 `graph_loaded_for_execution()` 判断。满足后，工厂还会读取 LangGraph Store 中的团队/Profile 设置，并按 thread 创建或连接 sandbox；这些不是一个孤立 Python 进程天然拥有的依赖。

因此：

- 直接 `await get_agent(config)` 但没有执行标记，会返回一个没有业务 tools、没有 sandbox 的空 Deep Agent；这只适合构图检查。
- 手动塞执行标记并不能消除 Store、模型凭据和 sandbox 依赖；它不是可靠的完整本地调试方法。
- 要调试真实 Open SWE Agent，使用 `langgraph dev` 提供 Runtime；要调试模型交互本身，使用下面的单进程最小 Agent。

本地调完整 Open SWE 时可设 `SANDBOX_TYPE=local`，但它会让 `execute` 等工具直接操作宿主机。只在受控目录运行，并先使用不要求工具调用的测试消息。

## 2. 单进程：最快看 prompt、工具参数和模型响应

此模式不使用 `get_agent()`，而是直接使用项目已有的 `make_model()` 与 Deep Agents。它保留模型和工具循环，但刻意不引入 thread、Store、sandbox 和 Dashboard，因此**不代表完整 Open SWE 的最终系统提示词或全量工具集**。

在 PyCharm 的 Scratch File 或未提交的本地文件中放入：

```python
import asyncio

from deepagents import create_deep_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from agent.utils.model import make_model


@tool
async def echo(text: str) -> str:
    """Return text unchanged."""
    return text


async def main() -> None:
    agent = create_deep_agent(
        model=make_model("openai:gpt-5.6-sol"),
        system_prompt="Use echo once, then state its result.",
        tools=[echo],
    )
    result = await agent.ainvoke({"messages": [HumanMessage("hello")]})
    print(result["messages"][-1].text)


asyncio.run(main())
```

在 PyCharm 中选择项目的 `.venv` 解释器，给 `echo()` 和 `result = await agent.ainvoke(...)` 打断点，按 Debug 运行。断点命中时可直接看：

- `text`：模型实际传给工具的参数；
- `result["messages"]`：每轮 `HumanMessage`、`AIMessage` 和 `ToolMessage`；
- 最后一条 `AIMessage`：模型最终响应。

此脚本会发起一次真实模型调用，可能产生模型费用；运行前需要配置对应模型的 API Key。若只验证 PyCharm 能跟进 async 调用，在 `await agent.ainvoke(...)` 前断下后即可停止调试；不要继续执行到模型调用。

常见误区：这个 Agent 的 system prompt 与工具仅是示例。它不能用于判断 Open SWE 生产 Agent 为什么多了某段仓库指令或少了某个工具；那应进入第 3 节的完整 Runtime 调试。

## 3. 完整 Open SWE：启动 Runtime，再用 SDK 发起一个 Run

在 PyCharm 中右键项目根目录的 [`run.py`](../../../run.py)，选择“调试”。它会加载根目录 `.env`，切换到项目根目录，再执行：

```bash
langgraph dev --server-log-level debug --no-browser
```

PyCharm 自动生成的配置只需使用项目 `.venv/bin/python`。不需要填写 `.env` 路径、工作目录、脚本参数，也不要添加 `--debug-port 5678 --wait-for-client`；当前 PyCharm 已经是启动 Runtime 的调试器。

`langgraph.json` 注册了 `agent`、`reviewer`、`analyzer`、`chat` 和 `scheduler`，所以这不是“只启动一个 Agent 函数”，而是完整本地 Runtime。默认项目代码把 `LANGGRAPH_URL` 未配置时视为 `http://localhost:2024`；若你改端口，必须同步设置 `LANGGRAPH_URL`。

完整 Run 在模型调用前还需要 GitHub 身份。Runtime 启动后，先在同一浏览器打开：

```text
http://localhost:2024/dashboard/api/auth/login
```

必须使用 `localhost`，不要改成 `127.0.0.1`，否则 OAuth state Cookie 不会随回调返回。GitHub 授权后，回调会把 token 保存到本地 Dashboard store；即使接着跳转的 `http://localhost:3000` 因未启动前端而无法访问，授权仍可能已成功。用同一浏览器访问 `http://localhost:2024/dashboard/api/me`，读取响应 JSON 中的 `login`。

仓库已提供 [`scripts/debug_open_swe_run.py`](../../../scripts/debug_open_swe_run.py)。它创建独立 thread，以项目支持的 `dashboard` source 运行，默认通过 `agent_model_id="openai:DeepSeek-V4-Flash"` 和 `agent_effort="high"` 覆盖 Runtime 默认模型，并打印每个 `messages`、`updates`、`events` 事件。工具参数包含在 `messages` 的 AI tool call 中，工具结果包含在 `updates` 中。用 `/me` 返回的 login 启动：

```bash
uv run --env-file .env python scripts/debug_open_swe_run.py --github-login <login>
```

可覆盖模型、effort 或任务内容：

```bash
uv run --env-file .env python scripts/debug_open_swe_run.py \
  --model openai:DeepSeek-V4-Flash \
  --effort high \
  --github-login <login> \
  --prompt "Use read_file to read agent/utils/tracing.py, then summarize it. Do not modify files."
```

预期现象：脚本先打印 `thread_id`，随后持续打印 `messages`、`updates`、`events` 事件；运行 `langgraph dev` 的终端同时输出 Runtime 与应用日志。这是最短的“本地 Runtime -> Agent -> 流事件”路径，不需要 Dashboard。

这仍是一次真实 Agent Run：可能调用模型；模型若不遵守“不要调用工具”，在 `SANDBOX_TYPE=local` 下可能访问宿主机文件。不要在含敏感文件的工作目录中使用宽泛任务。

## 4. 在完整 Agent 中看“每一轮实际发给模型的逻辑请求”

模型请求经过多层 middleware。当前 `server.py` 中 `SanitizeThinkingBlocksMiddleware` 后只剩 `ModelCallTimeoutMiddleware`，后者只加超时、不改请求。因此最接近 provider 调用前的业务断点是：

[`agent/middleware/sanitize_thinking_blocks.py`](../../../agent/middleware/sanitize_thinking_blocks.py) 的：

```python
return await handler(request)
```

断点命中后检查：

```python
request.system_message.text  # 当前轮最终逻辑 system prompt
request.messages             # 历史消息，不包含 system message
request.tools                # 当前轮实际可用的 tool schema
request.model                # 本轮模型；fallback 时可能不是主模型
request.model_settings       # 模型设置
```

这个位置比 [`prepare_run.py`](../../../agent/middleware/prepare_run.py:89) 更接近最终值：前者之后可能还有动态工具加载、plan mode 工具过滤、fallback 模型替换、Fireworks/Anthropic 消息清洗和超时 wrap-up 指令。

要看模型响应，在该调用处使用 PyCharm 的 Step Over 等待 `handler(request)` 返回，然后从调用栈上层检查返回的 `ModelResponse`；或在 SDK 输出中查看随后到达的 `messages` 事件。模型最终 HTTP JSON 仍不等于这里的 `ModelRequest`，它会由 provider adapter 在 [`agent/utils/model.py`](../../../agent/utils/model.py) 创建的 ChatModel 中序列化；当前项目没有保存原始 HTTP payload。

## 5. PyCharm 如何断到 `langgraph dev` worker

将断点设在 [`agent/middleware/sanitize_thinking_blocks.py`](../../../agent/middleware/sanitize_thinking_blocks.py) 的 `return await handler(request)` 前，再右键 `run.py` 选择“调试”。`langgraph dev` 使用热重载时可能创建子进程；若断点没有命中，在 PyCharm 的 `run.py` 调试配置中通过“修改选项”启用“调试时自动附加到子进程”，重启 Runtime 后再运行 SDK 脚本。

不要使用 PyCharm Python Debug Server 连接 `langgraph dev --debug-port`：该端口是 debugpy/DAP，而 Python Debug Server 是 pydevd 协议。当前方案由 PyCharm 直接启动目标 Python 进程，不存在二次 attach。

## 6. 固定排障顺序

1. 单进程最小 Agent：确认模型、消息格式和单个工具行为。
2. `langgraph dev` + SDK：确认 Agent 工厂、Runtime、事件流与 Run 状态。
3. 在最后模型 middleware 断点：检查完整逻辑 prompt、tools 和每轮响应。
4. 仅在上述信息不足时，添加后续章节的本地 JSONL recorder；不要默认把所有 prompt 和响应写入日志。

本章验证了 `get_agent()` 的执行模式分支、middleware 顺序、SDK 的 `threads.create()` / `runs.stream()` 签名，以及 PyCharm 直接调试 `run.py` 的完整 Runtime 路径。该路径已真实创建 Run 并完成 OAuth 前置验证；模型调用会使用配置的 provider，可能产生费用。

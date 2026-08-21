# 本地可观测性与调试

本专题先使用 Open SWE 已有的 LangGraph Run、SSE 和标准输出学习一次 Agent 运行；不依赖 LangSmith。完成后再实现一个本地 trace recorder，最后才考虑将它做成可查询的服务。

## 已验证的完整调试路径

本地学习完整 Open SWE 时，按下面四步执行：

1. 在 PyCharm 右键 [`run.py`](../../../run.py)，选择“调试”，启动 LangGraph Runtime。
2. 用浏览器访问 `http://localhost:2024/dashboard/api/auth/login`，完成 GitHub OAuth；用同一浏览器访问 `/dashboard/api/me` 确认保存的 `login`。
3. 运行 [`scripts/debug_open_swe_run.py`](../../../scripts/debug_open_swe_run.py)，传入该 `login`，创建独立 thread 并打印 SSE。
4. 在 Runtime 进程的 middleware 或工具实现中设置断点，检查完整逻辑 prompt、工具参数和模型响应。

这条路径保留 thread、sandbox、鉴权、middleware 和 SSE，适合学习真实 Open SWE。直接调用 `get_agent()` / `ainvoke()` 仍会遇到同一套鉴权与 sandbox 前置条件，适合局部实验，不是完整链路的简化替代。

## 调试指标与排障顺序

一次 Agent 调试的目标不是收集所有日志，而是定位“请求卡在哪一层”。按下面的指标由外到内检查：

| 层次 | 要看什么 | 当前查看位置 |
| --- | --- | --- |
| Run 身份 | `thread_id`、`run_id`、`source`、模型、effort | SDK 脚本开头与 SSE metadata |
| 生命周期 | Run 是否创建、当前 node/middleware、是否结束或报错、总耗时 | SDK 的 `events`、`updates` 和 PyCharm Debug 窗口 |
| 鉴权与 Sandbox | `github_login`、OAuth 是否完成、sandbox 是否创建或可用 | `/dashboard/api/me` 和 Runtime 日志；不打印 token |
| 模型调用 | 最终 system prompt、历史 messages、工具 schema、模型设置、超时/重试/fallback | `sanitize_thinking_blocks.py` 调用 `handler(request)` 前的断点 |
| 模型响应 | 可见文本、tool calls、finish reason、token usage、provider 错误 | SSE `messages` 与断点返回后的 `ModelResponse` |
| 工具执行 | 工具名、参数、耗时、输出、异常和副作用 | AIMessage 的 `tool_calls`、`updates`、ToolMessage、工具断点 |
| 状态与 SSE | 是否持续收到 `messages`、`updates`、`events`，checkpoint 是否推进 | SDK 脚本输出与浏览器 Network EventStream |

固定排障顺序：

1. `run.py` 是否已启动 Runtime 并监听 2024。
2. `/dashboard/api/me` 是否返回正确 `login`。
3. SDK 脚本是否打印 `thread_id` 和 `run_id`。
4. SSE 是否经过 `PrepareAgentRunMiddleware` 并继续进入模型节点。
5. 在最终模型 middleware 断点检查 prompt、tools 和模型响应。
6. 对每个工具调用核对参数、结果和副作用。

不要记录模型内部思维链。默认只观察或记录可见响应、工具调用、耗时、错误类型、token 用量和关联 ID；完整 prompt、工具参数、Cookie、OAuth/GitHub token 与私有代码都可能敏感，必须仅在本机受控环境查看并脱敏后再保存。

## 副作用：Agent 真正改变了什么

副作用是一次操作改变了持久状态或外部系统，而不只是读取数据、在内存中组装消息或生成文本。它决定调试时是否可以安全重试：工具返回异常并不总表示“什么也没发生”，网络超时尤其可能发生在外部系统已经完成操作之后。

| 类别 | Open SWE 中的例子 | 改变的状态 |
| --- | --- | --- |
| 只读 | `read_file`、`grep`、`ls`、模型生成文本 | 不应改变 sandbox 或外部系统 |
| 条件副作用 | `execute` | `pytest` 通常只读；shell 写文件、安装依赖、`git commit` 会修改工作区或 Git 历史 |
| sandbox 副作用 | `write_file`、`edit_file`、`delete` | 当前线程 sandbox 内的文件 |
| GitHub 副作用 | [`open_pull_request`](../../../agent/tools/open_pull_request.py) | 远端 PR、分支或评论状态 |
| 外部通知副作用 | [`slack_thread_reply`](../../../agent/tools/slack_thread_reply.py)、[`linear_comment`](../../../agent/tools/linear_comment.py) | Slack 或 Linear 中的新消息/评论 |
| 持久化调度副作用 | [`schedule_thread_wakeup`](../../../agent/tools/schedule_thread_wakeup.py) | 后续 Run 的唤醒任务 |
| HTTP 副作用 | [`http_request`](../../../agent/tools/http_request.py) 的 `POST`、`PUT`、`PATCH`、`DELETE` | 由远端 API 决定；`GET` 通常只读 |

### 最小安全验证

先用只读任务验证事件流，不让学习过程修改工作区：

```bash
uv run --env-file .env python scripts/debug_open_swe_run.py \
  --github-login <login> \
  --prompt "Use read_file to read agent/utils/tracing.py, then explain its purpose. Do not modify files, run shell commands, or call network tools."
```

预期是 SSE 中出现 `read_file` 的参数和工具结果，而没有写文件、shell 命令、PR 或消息通知。常见误区是把 `execute` 一律当成只读工具；它是否有副作用取决于模型实际传入的命令。

### 超时、失败与重试

以下情况需要把结果标为“未知”，然后先检查外部状态，不能直接重试：

```text
open_pull_request
  -> GitHub 已创建 PR
  -> 网络超时，工具向 Agent 报错
  -> Agent 若直接重试，可能创建重复 PR
```

对每个有副作用的工具调用，至少关联并审计：工具名、脱敏后的参数、目标资源、开始时间、耗时、成功/失败/未知状态、外部资源 ID（例如 PR URL 或 commit SHA）、错误类型，以及是否可安全重试。调试顺序是：先判断该调用是否有副作用，再确认执行到哪一步和外部状态，最后才决定重试、补偿或人工处理。

## 先区分三类数据

| 想看的内容 | 当前项目是否已有 | 现在去哪里看 | 边界 |
| --- | --- | --- | --- |
| Run 状态、节点/消息更新、工具生命周期 | 是 | Dashboard UI、浏览器 Network 的 SSE、LangGraph thread state/history | 这是 LangGraph 事件，不是 HTTP 请求日志 |
| 工具名称、参数、执行输出 | 是 | Agent 对话中的工具卡片；前端由 `stream.toolCalls` 投影 | 输出可能被工具自身截断或脱敏 |
| 模型最终回答与可见 reasoning 摘要 | 是 | Agent 对话、`messages` 事件和 thread history | 不是模型内部完整思维链 |
| 发给模型的完整逻辑请求 | 未持久化 | 断点查看 `ModelRequest`；后续添加本地 recorder | 包含用户内容、token、仓库上下文等敏感数据 |
| Provider 最终 HTTP JSON | 未持久化 | 后续在 provider transport 边界记录 | 它与 LangChain `ModelRequest` 不同，provider adapter 会二次序列化 |
| Python 服务诊断日志 | 是 | 运行 `langgraph dev` 的终端 stdout/stderr | 当前没有结构化日志收集或查询服务 |

## 学习路线

1. [本地 Run 与 SSE 事件流](01-local-run-and-sse.md)：用 PyCharm 启动 Runtime、完成 OAuth，再从浏览器和 SDK 观察事件流。
2. [纯 Agent、`langgraph dev` 与 PyCharm 调试](02-pure-agent-and-pycharm-debug.md)：区分单进程模型调试和完整 Open SWE Runtime 调试，并定位最终逻辑 prompt、工具与响应。
3. [免费自托管 Langfuse](03-self-hosted-langfuse.md)：部署、数据模型、LangChain callback 接入点，以及上线前的隐私与副作用审计边界。
4. 工具调用、错误与状态：关联 tool call、ToolMessage、SSE 和 UI 卡片。
5. 服务日志与断点：日志级别、`debug-port`、最短排障路径与敏感信息边界。
6. 本地 trace recorder：用一个 LangChain callback 记录 JSONL，不上传第三方。
7. 从本地文件到可观测服务：trace schema、关联 ID、存储、检索与保留策略，作为 LangSmith 类能力的最小版本。

## 当前源码地图

```text
Dashboard StreamProvider
  -> POST /dashboard/api/threads/{thread_id}/commands
  -> LangGraph /threads/{thread_id}/commands
  -> POST /dashboard/api/threads/{thread_id}/stream/events
  -> LangGraph /threads/{thread_id}/stream/events (SSE)
  -> streamMessagesToUi(messages, toolCalls, subagents)
```

关键源码：

- [`agent/dashboard/thread_api.py`](../../../agent/dashboard/thread_api.py)：启动 Run、声明 stream modes、代理 SSE。
- [`agent/dashboard/routes.py`](../../../agent/dashboard/routes.py)：Dashboard 的 SSE 路由。
- [`ui/src/features/agents/lib/AgentThreadStreamProvider.tsx`](../../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx)：浏览器端 StreamProvider。
- [`ui/src/features/agents/lib/streamMessagesToUi.ts`](../../../ui/src/features/agents/lib/streamMessagesToUi.ts)：把消息与工具参数/输出投影为 UI。
- [`agent/middleware/prepare_run.py`](../../../agent/middleware/prepare_run.py)：运行前生成并合并系统提示词的位置。

## 安全底线

完整 prompt、工具参数和模型响应可能含 GitHub token、OAuth cookie、仓库私有代码、用户消息和远端 URL。只在本机受控目录保存，文件权限设为仅当前用户可读；后续 recorder 必须默认脱敏并默认关闭。

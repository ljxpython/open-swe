# 第 1 章：本地 Run 与 SSE 事件流

## 学习目标

本章只回答一个问题：在不接入 LangSmith、也不改业务代码的前提下，如何观察一次 Dashboard Agent Run 从提交到 UI 更新的全过程。

它解决的是“这次 Run 到底有没有启动、卡在事件流还是 Agent、工具有没有执行”的定位问题。它不能保存完整模型 prompt 或 provider HTTP payload，那是后续章节的职责。

## 运行链路

```text
浏览器提交消息
  -> POST /dashboard/api/threads/{thread_id}/commands
  -> Dashboard 代理转发到 LangGraph /commands，创建 Run
  -> POST /dashboard/api/threads/{thread_id}/stream/events
  -> Dashboard 代理原样转发 LangGraph SSE 字节流
  -> @langchain/react 收到 events
  -> streamMessagesToUi 显示消息、工具参数、工具输出和子 Agent 状态
```

这个结论来自 [`agent/dashboard/thread_api.py`](../../../agent/dashboard/thread_api.py)：它为 Dashboard Run 声明 `values`、`updates`、`messages`、`messages-tuple`、`tools`、`checkpoints`、`events` 七类 stream mode，并将 SSE 代理到运行时的 `/threads/{thread_id}/stream/events`。运行时的默认地址由 [`agent/utils/thread_ops.py`](../../../agent/utils/thread_ops.py) 的 `LANGGRAPH_URL` 或 `LANGGRAPH_URL_PROD` 决定，未配置时是 `http://localhost:2024`。

## 最小真实观察

### 1. 在 PyCharm 启动 Runtime

项目根目录的 [`run.py`](../../../run.py) 已封装下面三件事：切换到项目根目录、加载根目录 `.env`、执行 `langgraph dev --server-log-level debug --no-browser`。

在 PyCharm 中确认解释器为项目 `.venv/bin/python`，右键 `run.py` 并选择“调试”。不需要配置脚本参数、工作目录、`.env` 文件路径，也不要添加 `--debug-port` 或 `--wait-for-client`。看到 Runtime 监听 `2024` 端口后再继续。

### 2. 完成本地 Dashboard GitHub OAuth

完整 Open SWE Run 会在调用模型前创建或恢复 sandbox，并解析 GitHub 身份；即使本次提示词只调用 `read_file` 也一样。先在浏览器打开：

```text
http://localhost:2024/dashboard/api/auth/login
```

完成 GitHub 授权。该地址会创建本地会话并保存 OAuth token；不要把 token 复制到脚本或 `.env`。OAuth 发起地址必须与 `DASHBOARD_API_BASE_URL` 的主机和端口完全一致；`localhost` 与 `127.0.0.1` 的 Cookie 不共享。

当前 `.env` 将前端地址设为 `http://localhost:3000`。因此授权回调保存 token 后会跳转到 `localhost:3000`；若 Vite 前端未启动，浏览器显示“拒绝连接”不代表授权失败。仍使用同一浏览器打开：

```text
http://localhost:2024/dashboard/api/me
```

响应 JSON 中的 `login` 就是下一步要传给 SDK 脚本的值。若需要 Dashboard 前端，再在另一个终端运行 `npm run dev`。

### 3. 用 SDK 脚本创建独立 Run

`--github-login` 是上一步 `/me` 返回的 GitHub 登录名；脚本只传 login，Runtime 从 Dashboard 的 token store 读取令牌：

```bash
uv run --env-file .env python scripts/debug_open_swe_run.py --github-login <你的 GitHub 登录名>
```

遗漏 login 时，脚本会在本地提示 `--github-login ... is required`；传入 login 但尚未完成 OAuth 时，Runtime 会报 `GitHub authentication required`。两者都表示尚未进入模型层，不是 DeepSeek 或 SSE 故障。

预期现象：

1. PyCharm Debug 窗口输出 HTTP 请求、异常堆栈和服务端调试日志。
2. Dashboard 中出现 Agent 文本、工具卡片或子 Agent 卡片。
3. 浏览器开发者工具的 Network 面板能看到 `stream/events` 请求；打开其 EventStream/Response 可查看持续到达的 SSE 帧。

常见误区：`make run` 只启动 FastAPI app，并不启动 LangGraph graph runtime；它仍会尝试把命令和事件代理到 `LANGGRAPH_URL`。学习完整 Run 时应使用 `langgraph dev`，或分别启动两个服务。

## 在浏览器中按层排查

### 1. Run 是否成功创建

在 Network 面板找到 `POST .../commands`：

- `2xx`：Dashboard 已请求 LangGraph 创建或控制 Run；响应中通常可定位 `run_id`。
- `4xx`：先检查 Dashboard 登录态、线程归属或请求 body。
- `5xx/502`：检查 Dashboard 后端是否能连接 `LANGGRAPH_URL` 指向的运行时。

对应的代理代码在 [`agent/dashboard/thread_api.py`](../../../agent/dashboard/thread_api.py)，它会补上 `assistant_id="agent"`、`stream_mode`、`stream_resumable=True` 与本次运行的 `configurable`。

### 2. 事件流是否连通

接着找到 `POST .../stream/events`：

- 状态保持 pending 且持续有 SSE 帧：连接正常，继续根据 event 内容判断 Agent 做了什么。
- 立即断开：看 `langgraph dev` 终端。Dashboard 代理在异常时会记录 `LangGraph stream/events proxy closed` 并带异常栈。
- 有 Run 却没有 UI 更新：检查事件是否包含 `messages`、`tools`；前端需要这些 stream mode 才能组装工具卡片。

代理不会重新解释 SSE；`_stream_thread_events()` 只是从 LangGraph 读取字节并 `yield` 给浏览器。因此同一事件可同时在浏览器 EventStream 和 LangGraph 服务端日志中交叉验证。

### 3. 工具参数和输出如何看

当前 UI 已经有这条链路：

```text
AIMessage.tool_calls[*].args        -> 工具卡片的 input
stream.toolCalls[*].output          -> 工具卡片的 output
ToolMessage                          -> 工具完成/错误的后备状态
```

实现位于 [`streamMessagesToUi.ts`](../../../ui/src/features/agents/lib/streamMessagesToUi.ts)。因此打开工具卡片可先看参数和输出；若 UI 显示异常，再对照浏览器 SSE 的 `messages`、`tools` 事件。不要把 UI 工具卡片误认为原始 HTTP 调用记录，它是 SDK 投影后的展示模型。

## 完整模型请求应在哪里断点看

模型的“完整”至少有两个层次：

| 层次 | 内容 | 当前最佳观察点 |
| --- | --- | --- |
| LangChain 逻辑请求 | 合并后的 system prompt、历史 messages、可调用 tools、模型设置 | [`agent/middleware/sanitize_thinking_blocks.py`](../../../agent/middleware/sanitize_thinking_blocks.py) 的 `return await handler(request)` 前 |
| Provider HTTP payload | 例如 OpenAI Responses 或 Anthropic Messages 的最终 JSON | 当前未记录；后续在 `make_model()` 创建的 provider transport 边界增加 recorder |

在前一个断点处，先单步执行完 `request = request.override(...)`，再检查：

```python
request.system_message.text  # 实际系统提示词
request.messages             # 不含 system message 的历史消息
request.tools                # 这次模型可调用的工具 schema
request.model_settings       # 本次模型参数
```

这比只查看 `rendered_system_prompt` 更准确，因为 middleware 会将它与 Deep Agents 原有的 `request.system_message` 合并；同时它位于动态工具处理和模型清洗之后。注意：这仍不是 provider 最终 HTTP JSON；[`agent/utils/model.py`](../../../agent/utils/model.py) 会按 OpenAI、Anthropic 等 provider 调整 `base_url`、reasoning、timeout 和 Responses API 参数。

## 日志与断点

当前调试方式是 PyCharm 直接 Debug `run.py`，不使用远程调试端口。需要断到 Runtime worker 时，在 `run.py` 自动生成的 PyCharm 配置中启用“调试时自动附加到子进程”；`langgraph dev` 的热重载可能将服务放到子进程中。不要把 PyCharm 的 Python Debug Server 连接到 `--debug-port`，前者使用 pydevd，后者使用 debugpy/DAP。

标准 `logging` 已遍布服务端模块：例如 Agent 图装配、sandbox、Dashboard 代理都以 `logger.info/debug/warning/exception` 输出到服务进程。排障顺序应固定为：浏览器 Network 确认请求和 SSE，`langgraph dev` 终端确认后端异常，再用断点检查模型逻辑请求。别一上来开全量模型 payload 日志，私有代码和凭据一锅端落盘，那才是真正的憨批操作。

## 验证与下一章

本章以 `run.py` + PyCharm Debug、`localhost` OAuth 和 SDK 脚本完成了真实 Runtime 验证：Run 能创建，SSE 能输出 `events`、`updates`，认证完成后会进入模型调用。并以当前源码核对了 Dashboard 的 `/commands` 与 `/stream/events` 代理、stream modes 和工具 UI 投影。

下一章会在不改生产默认行为的前提下，设计一个显式开关的本地 JSONL recorder，记录每次模型调用的逻辑请求、工具调用和响应，并先处理脱敏与文件权限。

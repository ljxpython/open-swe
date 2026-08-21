# Langfuse Cloud 可观测性

本章将 Open SWE 的 LangGraph 运行链路上报到 Langfuse Cloud。目标是看到一次 Agent Run 的完整提示词、模型响应、工具参数、工具结果、耗时、token 与错误。

完成接入后，按 [01-reading-traces.md](01-reading-traces.md) 学习如何阅读和排查 Cloud 中的数据。

## 1. 当前接入做了什么

接入点位于 `agent/utils/tracing.py` 的 `traced_graph_factory()`：每次 LangGraph Runtime 为一个 Run 创建 graph 后，`with_langfuse_tracing()` 给该 graph 合并 Langfuse 的 LangChain `CallbackHandler`。

LangChain 会通过 callback 自动创建层级观测，不需要在每个模型、工具或子 Agent 中手写埋点：

```text
一次 LangGraph Run = 一个 Langfuse Trace
同一 thread_id 的多次 Run = 一个 Langfuse Session
模型调用 = Generation（模型、token、延迟、提示词、响应）
工具调用 = Tool（参数、结果、错误）
Agent / Chain = 上层 Span
```

`thread_id` 被写为 `langfuse_session_id`，所以同一对话线程的多轮 Run 可在 Sessions 页按时间回放。`github_login` 被写为 `langfuse_user_id`，而 `open-swe-agent` / `open-swe-review` 与消息来源成为 tags。

`langsmith.tracing_context(...)` 仍然保留。它服务于原有的 LangSmith tracing 和默认 LangSmith sandbox；Langfuse 是额外的可观测性导出，二者不是互斥替换关系。

## 2. Cloud 配置

在项目根目录 `.env` 配置，值只能保存在本机或部署平台的密钥管理中，不能提交或贴到聊天中：

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
OPEN_SWE_LANGFUSE_ENABLED=true
```

`LANGFUSE_BASE_URL` 必须和创建项目时选择的 Cloud 区域一致。没有 `OPEN_SWE_LANGFUSE_ENABLED=true` 时，代码不会导入 Langfuse 或增加 callback；这让本地普通调试不产生上传。

项目依赖已加入 `langfuse>=4.14,<5`。安装或更新依赖后执行：

```bash
uv sync --extra dev
```

## 3. 启动并生成一条 Trace

先在 PyCharm 右键调试项目根目录的 `run.py`，它会启动 LangGraph Runtime。等待终端出现监听地址（通常是 `http://localhost:2024`）。不要用 `--wait-for-client`，否则 Runtime 会在真正的 Agent Run 前暂停。

另开一个终端，先在浏览器完成 GitHub OAuth。通过 `http://localhost:2024/dashboard/api/me` 确认登录名，再用实际值启动已有 SDK 调试脚本：

```bash
uv run --env-file .env python scripts/debug_open_swe_run.py \
  --github-login <dashboard-api-me 返回的 login> \
  --prompt "Use read_file to read agent/utils/tracing.py, then explain its purpose. Do not modify files, run shell commands, or call network tools."
```

这个命令会发送一次真实模型请求，可能产生模型费用；任务限定为只读，方便首次检查 trace。脚本输出的 `thread_id` 是随后在 Langfuse Sessions 页定位整段会话的依据。

`langgraph dev` 会监视项目文件。Agent Run 正在进行时修改源码或测试文件会触发 Runtime 重载，SSE 客户端可能报 `httpx.ConnectError`；这表示连接被重载中断，不是模型或 Langfuse 错误。等待 `/health` 恢复后重新发起一个新的 Run。若重启后 OAuth 登录态不可用，先重新完成 Dashboard OAuth。

## 4. 在 Langfuse Cloud 中检查

打开 Cloud 项目的 **Tracing** 页面，按最近时间查看最新 Trace。检查以下内容：

| 位置 | 应看到的内容 | 说明 |
| --- | --- | --- |
| Trace 表格 | `open-swe-agent` 或 `open-swe-review` | 稳定的图级名称，便于筛选 |
| Trace tree | Agent/Chain、Generation、Tool 的嵌套结构 | 一次 Run 中每一步为什么发生 |
| Generation | 模型名、输入/输出 token、延迟、完整 messages | 用来定位 prompt 或模型响应问题 |
| Tool | 工具名、输入参数、输出或异常 | 用来定位参数和副作用 |
| Session | 相同 `thread_id` 的多轮 Run | 用来回放整个对话 |
| 用户与 Tags | GitHub login、项目名、来源 | 用来筛选成本、问题和渠道 |

如果 Trace 没有出现，依次检查：Runtime 启动时确实加载了 `.env`、四个配置项均存在且开关为 `true`、Base URL 属于当前 Cloud 区域、该 Run 真的调用了模型或工具。这里 callback 运行在 `langgraph dev` 的服务进程，`debug_open_swe_run.py` 退出不会中断其导出；服务进程会持续批量上报。若以后改为直接运行一次性 `agent.ainvoke(...)` 脚本，则在进程结束前调用 `get_client().flush()`。

## 5. 数据边界与脱敏

开启后，完整业务 prompt、模型响应、工具参数和工具结果会上传到 Langfuse Cloud，才能实现本章的调试目标。这些内容可能含代码、仓库路径和用户输入，因此只能在允许上传这些数据的开发/测试项目中启用。

导出前会递归脱敏常见凭据字段与形式：`Authorization`、Cookie、token、API key、password、GitHub token 与 `sk-` / `pk-` 形式的 key。脱敏发生在服务进程内、数据离开进程之前；它不等于通用 DLP，不能保证识别业务文本里的所有敏感信息。

生产环境的推荐策略是：默认不开启完整内容采集，先确认数据分类、访问权限、保留期和删除流程；只保留调试必需字段，或再增加组织级 DLP / OpenTelemetry Collector 脱敏规则。

## 6. 从 Cloud 迁移到自托管

应用代码无需改变。部署自托管 Langfuse 后，仅替换部署环境的 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和 `LANGFUSE_BASE_URL`，重启 Runtime 即可。Cloud 中历史 Trace 不会自动迁移；是否迁移历史数据取决于之后的数据治理需求。

## 7. 验证清单

```bash
uv run ruff check agent/utils/langfuse.py agent/utils/tracing.py tests/utils/test_langfuse.py
uv run pytest -q tests/utils/test_langfuse.py
```

代码验证通过后，再执行第 3 节的一次最小真实 Run，并在 Cloud 检查：Trace 名称、Session、Generation 的模型/token、Tool 参数/结果、错误字段，以及任何不应上传的敏感内容。

本章接入已用一个零模型的最小 LangGraph 图做过 Cloud smoke test：Cloud 返回根 `CHAIN`，`traceName` 为 `open-swe-agent`，Session 与 tags 均正确。这只验证 callback 到 Cloud 的传输；第 3 节的真实 Run 才验证模型 Generation 和工具 Tool 的完整层级。

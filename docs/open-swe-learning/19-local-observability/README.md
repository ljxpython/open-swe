# 本地可观测性与调试

本专题先使用 Open SWE 已有的 LangGraph Run、SSE 和标准输出学习一次 Agent 运行；不依赖 LangSmith。完成后再实现一个本地 trace recorder，最后才考虑将它做成可查询的服务。

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

1. [本地 Run 与 SSE 事件流](01-local-run-and-sse.md)：从浏览器操作追到 LangGraph 的事件流。
2. 模型调用边界：在 `ModelRequest` 上区分完整逻辑请求和 provider HTTP payload。
3. 工具调用、错误与状态：关联 tool call、ToolMessage、SSE 和 UI 卡片。
4. 服务日志与断点：日志级别、`debug-port`、最短排障路径与敏感信息边界。
5. 本地 trace recorder：用一个 LangChain callback 记录 JSONL，不上传第三方。
6. 从本地文件到可观测服务：trace schema、关联 ID、存储、检索与保留策略，作为 LangSmith 类能力的最小版本。

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

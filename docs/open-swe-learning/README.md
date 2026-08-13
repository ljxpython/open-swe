# Open SWE 学习笔记

本目录是对当前 Open SWE 代码库的增量学习讲义。每章都以当前源码为准，区分已经验证的行为和后续待学内容。

## 学习范围

- 主体：`agent/`、`ui/`、`desktop/`、`langgraph.json` 和 `tests/`。
- 运行时：LangGraph、Deep Agents、FastAPI、可复用 sandbox。
- 当前前提：依赖已通过 `uv sync --extra dev` 安装；本地模型使用已配置的 OpenAI 兼容服务。

## 章节路线

0. [架构总览](00-architecture-overview.md)：用文字、C4、时序图、状态图和伪代码建立一次请求的全链路地图。
   - [架构图谱索引](architecture/README.md)：查看所有可编辑 Draw.io、PNG 和 HTML 资产。
1. [入口与本地运行](01-entry-and-runtime.md)：项目从哪里启动，哪些进程负责什么。
2. [线程、Run 与持久化](02-threads-runs-and-checkpoints.md)：一次消息如何成为可恢复的 Agent Run。
   - [2-1 主 Agent 工厂](02-1-main-agent-factory.md)：详细拆解 `agent.server:get_agent`。
   - [2-2 Dashboard 命令代理](02-2-dashboard-command-proxy.md)：请求如何被鉴权、补全并转发给 LangGraph。
3. [模型配置与 WawAPI](03-model-configuration-and-wawapi.md)：模型优先级、请求格式、超时与降级。
4. [Deep Agent 装配](04-deep-agent-assembly.md)：工具、提示词、中间件和子 Agent。
5. [Sandbox 生命周期](05-sandbox-lifecycle.md)：线程工作区、GitHub 凭据代理与故障恢复。
6. [Dashboard：认证、线程列表、流式交互和消息队列](06-dashboard-auth-streaming-and-queue.md)：从 OAuth 登录到 `run.start`、SSE 和 busy thread 追加消息。
7. [GitHub、Slack、Linear Webhook 与确定性 thread_id](07-webhooks-deterministic-threads-and-dispatch.md)：签名验证、事件门禁、稳定会话路由与 durable dispatch。
8. [Reviewer、Analyzer 与 CI 自动修复边界](08-reviewer-analyzer-and-ci-boundaries.md)：只读审查、仓库风格学习、GitHub Check 和当前 checkout 的 CI 能力边界。
9. [测试、类型检查、部署与安全边界](09-testing-deployment-and-security.md)：工程验收、凭据边界和课程总收束。
10. [LangGraph SDK 命令协议与 SSE 事件模型](10-langgraph-sdk-command-and-sse.md)：`run.start`、SSE channels、事件投影和忙碌线程队列的协议级链路。
11. [Dashboard UI 事件投影](11-dashboard-ui-event-projection.md)：SDK 流投影如何变成消息、工具调用、子 Agent 状态与发送/停止按钮。
12. [PR Chat：`traced_chat_agent`](../traced-chat-agent-guide-zh.md)：围绕单个 PR 的只读问答、上下文注入、权限和 SSE 链路。
13. [Deep Agents 主 Agent Loop](../deepagents-main-agent-loop-guide-zh.md)：从拿到用户任务开始，理解模型、工具、子 Agent 与验证如何形成闭环。
14. [Deep Agents 提示词与委派](../deepagents-prompts-and-delegation-guide-zh.md)：解释主 Agent 如何拿到动态工作规则、何时直接调用工具、何时才委派给 `general-purpose`，以及 `create_agent` 怎样被封装为 `create_deep_agent`。

## 覆盖矩阵

| 知识点 | 源码证据 | 课程章节 | 验证 | 项目外扩展 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 图入口与本地运行 | `langgraph.json`、`Makefile` | 01 | 命令已核对 | Docker/生产部署 | 讲义完成 |
| 全局技术架构与请求链路 | `langgraph.json`、`agent/webapp.py`、`agent/server.py` | 00 | 图结构校验；本地导入检查 | 部署拓扑、生产可观测性 | 讲义完成 |
| 线程、Run、checkpoint、FastAPI 路由 | `agent/dispatch.py`、`agent/server.py`、`agent/dashboard/routes.py`、`agent/dashboard/thread_api.py` | 02 | `test_dashboard_thread_api.py` + `test_dispatch.py` 共 81 项通过 | 长期记忆与外部 checkpoint | 讲义完成 |
| 主 Agent 工厂 | `agent/server.py:get_agent` | 02-1 | 导入检查；避免触发 sandbox 的构图说明 | 自定义 graph factory | 讲义完成 |
| Dashboard 命令代理 | `agent/dashboard/thread_api.py` | 02-2 | 4 项命令代理单元测试 | 自定义前端协议与审计 | 讲义完成 |
| 模型配置与提供商适配 | `agent/utils/model.py`、`agent/server.py`、`agent/utils/gateway.py` | 03 | 静态模型测试；WawAPI 两次最小真实调用返回 `OK` | Gateway、Anthropic fallback、工具 payload | 真实验证完成 |
| Deep Agents 工具和中间件 | `agent/server.py`、`agent/middleware/` | 04 | 装配测试 + 图结构校验 | MCP 工具治理 | 讲义完成 |
| Sandbox 生命周期 | `agent/utils/sandbox_state.py`、`agent/server.py`、`agent/integrations/langsmith.py` | 05 | sandbox 状态、恢复、reviewer 替换测试 | 自定义 sandbox provider | 讲义完成 |
| Dashboard 流式交互 | `agent/dashboard/`、`ui/src/features/agents/`、`agent/middleware/check_message_queue.py` | 06 | OAuth、thread API、SSE、queue 单测 | 多租户鉴权 | 讲义完成 |
| 外部事件入口 | `agent/webhooks/`、`agent/dispatch.py`、`agent/utils/thread_ids.py` | 07 | webhook、thread ID、dispatch 单测 | 其他 IM/Issue 系统 | 讲义完成 |
| Reviewer、Analyzer、CI | `agent/reviewer.py`、`agent/analyzer.py`、`agent/dashboard/review_style_jobs.py`、`agent/utils/github_ci.py` | 08 | Reviewer/Analyzer 装配与相关单测；CI 调度器当前缺失 | 评估体系、CI auto-fix | 受外部条件阻塞 |
| 测试和部署 | `tests/`、`Makefile`、`pyproject.toml`、`langgraph.json` | 09 | 单测、图校验、配置核对；完整 lint/typecheck/pytest 按本轮记录 | 集成测试与可观测性 | 讲义完成 |
| LangGraph SDK 命令、SSE 和 React 投影 | `agent/dashboard/routes.py`、`agent/dashboard/thread_api.py`、`ui/src/features/agents/lib/AgentThreadStreamProvider.tsx`、`ui/src/features/agents/lib/streamMessagesToUi.ts` | 10 | SDK 类型/实现静态核对；代理、CSRF、dispatch 单测 | WebSocket、custom channels、多 namespace interrupt UI | 讲义完成 |
| Dashboard UI 事件投影与运行控制 | `ui/src/features/agents/lib/streamMessagesToUi.ts`、`AgentThreadView.tsx`、`components/{messages,subagents,composer}/` | 11 | `streamMessagesToUi` 单测；图结构校验 | 输入编辑器、interrupt 卡片、真实 SSE 集成 | 讲义完成 |
| PR Chat 只读问答 | `agent/chat.py`、`agent/dashboard/review_chat_api.py`、`ui/src/features/reviews/ReviewChat.tsx` | 12 | 源码与 review chat 单测静态核对；图结构/预览检查 | 真实 GitHub、模型和远端 SSE | 讲义完成 |
| Deep Agents 主 Agent Loop | `agent/server.py`、`deepagents/graph.py`、`langchain/agents/factory.py` | 13 | 源码静态核对；图结构/预览检查 | 真实模型 trace | 讲义完成 |
| Deep Agents 提示词与子 Agent 委派 | `agent/prompt.py`、`agent/server.py`、`prepare_run.py`、`deepagents/{graph,subagents}.py` | 14 | 静态提示词构造；图结构/预览检查 | 真实模型 task trace | 讲义完成 |

## 架构图谱覆盖

| 图 | 用途 | 关键源码 |
| --- | --- | --- |
| `architecture/premium/01-c4-overview.drawio` | 系统上下文、容器、组件三层下钻 | `langgraph.json`、`agent/server.py`、`agent/dashboard/` |
| `architecture/premium/02-dashboard-run-sequence.drawio` | 首次 `run.start` 与后续 SSE | `agent/dashboard/thread_api.py`、`ui/.../AgentThreadStreamProvider.tsx` |
| `architecture/premium/03-webhook-sequence.drawio` | GitHub/Slack/Linear 事件入口 | `agent/webapp.py`、`agent/utils/{github_comments,slack,linear}.py` |
| `architecture/premium/04-agent-factory-sequence.drawio` | `get_agent(config)` 装配顺序 | `agent/server.py`、`agent/utils/model.py` |
| `architecture/premium/05-state-lifecycle.drawio` | Thread、Run、Checkpoint、sandbox、Git ref | `agent/utils/sandbox_state.py`、`utils/turn_checkpoint.py` |
| `architecture/premium/06-security-boundary.drawio` | OAuth、GitHub App、proxy、sandbox 权限边界 | `agent/dashboard/oauth.py`、`agent/utils/github_app.py`、`agent/integrations/langsmith.py` |
| `architecture/premium/07-reviewer-analyzer.drawio` | 主 Agent、Reviewer、Analyzer 职责关系 | `agent/reviewer.py`、`agent/analyzer.py` |
| `architecture/premium/08-dashboard-module-map.drawio` | Dashboard 35 个 Python 模块的导入关系 | `agent/dashboard/*.py` |
| `architecture/premium/09-model-config-sequence.drawio` | 模型选择、WawAPI Chat 与 fallback | `agent/server.py`、`agent/utils/model.py`、`agent/utils/gateway.py` |
| `architecture/premium/10-deep-agent-assembly-sequence.drawio` | Deep Agent 工厂、工具、提示词、middleware、subagent 与模型/工具循环 | `agent/server.py`、`agent/middleware/prepare_run.py`、`agent/tools/` |
| `architecture/premium/11-sandbox-lifecycle-sequence.drawio` | sandbox 三路生命周期、GitHub proxy 刷新、不可达保护与 reviewer 替换 | `agent/server.py`、`agent/utils/sandbox_state.py`、`agent/reviewer.py` |
| `architecture/premium/12-dashboard-auth-stream-queue-sequence.drawio` | OAuth state/session、线程权限、`run.start`、SSE 和 busy queue 的完整时序 | `agent/dashboard/routes.py`、`agent/dashboard/thread_api.py`、`ui/.../AgentThreadStreamProvider.tsx`、`agent/middleware/check_message_queue.py` |
| `architecture/premium/13-sdk-command-event-protocol-sequence.drawio` | 命令控制面、SSE 观察面、SDK 投影和忙碌线程队列的协议级时序 | `agent/dashboard/thread_api.py`、`ui/.../AgentThreadStreamProvider.tsx`、`ui/.../useSubmitAgentMessage.ts` |
| `architecture/premium/14-dashboard-ui-event-projection.drawio` | SDK 投影如何组装为消息、工具、子 Agent 与运行按钮 | `ui/.../streamMessagesToUi.ts`、`AgentThreadView.tsx`、`components/{messages,subagents,composer}/` |
| `architecture/premium/15-review-chat-sequence.drawio` | PR Chat 的命令控制面、PR 虚拟文件注入、只读 GitHub 查询和 SSE 观察面 | `agent/dashboard/review_chat_api.py`、`agent/chat.py`、`ui/.../ReviewChat.tsx` |
| `architecture/premium/16-main-agent-loop.drawio` | 主 Agent 的模型判断、工具执行、ToolMessage 回流、子 Agent 汇总与正常结束 | `agent/server.py`、`deepagents/graph.py`、`langchain/agents/factory.py` |
| `architecture/premium/17-deepagents-prompts-and-delegation.drawio` | 主 Agent 直接工具调用与 `task` 委派的分支、提示词和结果回流边界 | `agent/prompt.py`、`agent/server.py`、`deepagents/{graph,subagents}.py` |

所有图都有对应的 `png/` 预览和 `html/` 交互查看器；`.drawio` 与 JSON 是可编辑、可重复生成的源文件。

## 验证记录

- 第 2 章执行 `uv run pytest -vvv tests/agent/test_dispatch.py`，10 项通过。
- 第 2 章执行 `uv run pytest -vvv tests/middleware/test_check_message_queue.py`，6 项通过。
- 第 2-2 章执行 Dashboard 命令代理边界测试，4 项通过。
- 第 2 章 FastAPI 路由扩展验证：`uv run pytest -q tests/dashboard/test_dashboard_thread_api.py tests/agent/test_dispatch.py`，81 项通过。
- 第 3 章执行 WawAPI 两次 `max_tokens=16` 的最小真实调用，普通与 `medium` effort 路径均返回 `OK`；未验证 Gateway 与 Anthropic fallback。
- 第 4 章装配测试覆盖 backend、skills 路由、工具列表、middleware 顺序和 subagent 模型继承；图校验为 `0 error / 0 warning / 0 crossings`。
- 第 5 章执行 sandbox proxy、恢复、reviewer 替换、显式重建和 stale 状态测试；新增生命周期图校验为 `0 error / 0 warning / 0 crossings`。未执行真实 LangSmith sandbox 创建。
- 第 6 章新增 Dashboard 认证、线程、命令代理、SSE 和消息队列讲义；第 12 张 Dashboard 时序图校验为 `0 error / 0 warning / 0 crossings`。外部 OAuth、远端 LangGraph SSE 和真实模型未执行。
- 第 7 章复用入口时序图，新增 GitHub、Slack、Linear 的签名门禁、确定性 thread ID、metadata 和 durable dispatch 源码讲解；真实平台 webhook 未执行。
- 第 8 章完成 Reviewer、Analyzer 与 CI 边界讲义；Reviewer/Analyzer 图校验为 `0 error / 0 warning / 0 crossings`。当前 checkout 不含 `agent/ci_autofix.py`，CI 自动修复调度链未宣称完成。
- 第 9 章完成测试、类型检查、部署和安全边界总收束；复用 security boundary 图，外部 OAuth、真实 sandbox 和生产部署未执行。
- 第 10 章完成 LangGraph SDK 命令、SSE channel、事件投影和 busy queue 协议讲义；新增第 13 张时序图校验为 `0 error / 0 warning / 0 crossings`，SDK 版本和协议类型做了本地静态核对，未连接远端 SSE。
- 第 11 章完成 Dashboard UI 事件投影讲义；新增第 14 张组件图，使用 `streamMessagesToUi` 单测验证 turn 边界，未连接远端 SSE 或执行真实取消。
- 第 12 章完成 PR Chat 的独立讲解；新增第 15 张时序图，结构校验为 `0 error / 0 warning`，并完成 PNG 预览检查。未调用真实 GitHub、模型或远端 SSE。
- 第 13 章完成主 Agent Loop 独立讲解；新增第 16 张循环图，结构校验为 `0 error / 0 warning`，并完成 PNG 预览检查。未发起真实模型调用。
- 第 14 章完成主 Agent 动态提示词、general-purpose 子 Agent 与 `create_agent -> create_deep_agent` 装配讲解；新增第 17 张分支时序图，结构校验为 `0 error / 0 warning`，并完成 PNG 预览检查。执行本地静态提示词构造，未发起真实模型调用。
- 上述验证使用 fake LangGraph client 或本地 store，不调用模型、GitHub 或网络服务。

## 使用方式

按章节顺序阅读即可。每一章都包含源码路径、最小验证、常见误区和练习；新章节完成后会更新本文件的状态。

## 主 Agent 快速总览

如果想先用通俗语言把“外部消息 -> LangGraph Run -> `get_agent` -> 模型/工具循环 -> 回复”完整走一遍，先读 [`docs/main-agent-guide-zh.md`](../main-agent-guide-zh.md)。它是现有第 2-1 章和第 4 章的总串讲，不替代后续源码细节章节。

## 待深入主题

- 输入编辑器：`ChatComposer` 的 slash command、图片限制、`@` 文件引用、model/effort 选择与 `configurable`。
- interrupt/approval 卡片的事件兼容分支，以及更深层 subgraph 的 namespace 呈现。

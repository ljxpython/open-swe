---
type: Runtime Architecture
title: LangGraph 运行时与 Agent 组装
description: Open SWE 如何组合 LangGraph 图、FastAPI 路由、持久化调度、按线程隔离的沙箱，以及用于编码、评审、聊天和定时任务的 Deep Agents 工厂。
resource: /langgraph.json
tags: [open-swe, langgraph, deepagents, backend, runtime]
---
# LangGraph 运行时与 Agent 组装

## 已部署单元

`langgraph.json` 是运行时清单。它注册五个图和一个 HTTP 应用：

| ID | 入口点 | 作用 |
|---|---|---|
| `agent` | `agent.graphs.agent:traced_agent` | 主编码 Agent。 |
| `reviewer` | `agent.graphs.reviewer:traced_reviewer_agent` | 只读 PR 评审器。 |
| `analyzer` | `agent.graphs.analyzer:traced_analyzer` | 仓库评审风格分析。 |
| `chat` | `agent.graphs.chat:traced_chat_agent` | 仪表盘聊天助手。 |
| `scheduler` | `agent.graphs.scheduler:get_scheduler` | 定时工作。 |

同一清单挂载 `agent.webapp:app`，它重新导出在 `agent/api/app.py` 中组装的 FastAPI 应用。该应用挂载仪表盘、计划、工作流审批、GitHub、Linear、Slack 和健康检查路由。启动时它会校验沙箱和本地模型配置，并且只为明确配置的 `DASHBOARD_ALLOWED_ORIGINS` 启用带凭据 CORS；通配符来源会被拒绝。

## 从来源事件到持久化运行

`agent/webhooks/` 中按来源划分的路由会校验提供商输入，执行来源/仓库规则，构造上下文，并选择确定性线程标识。路由将任务交给 `agent/dispatch.py`，后者是共享的 LangGraph 运行契约。

`dispatch_agent_run` 以 `durability="sync"` 和 `multitask_strategy="interrupt"` 创建运行。同步持久化会为每一步建立检查点；收到后续消息时会中断当前运行，并带着历史和新消息恢复，而不依赖进程本地队列。如果配置了外部可访问的完成 URL 和密钥，调度器还会添加完成 Webhook。该调度机制让[工作流](workflows-zh.md)中的 Slack、Linear、GitHub 和仪表盘入口可以跨进程延续用户流程。

## 编码图组装

`agent/server.py:get_agent` 为一次运行构建新的 Deep Agent。准备中间件会解析运行身份和配置，获取或重连线程沙箱，写入相关元数据/用量，并渲染系统提示词。图包含 Deep Agents 内置的文件系统、Shell、待办和子 Agent 能力，以及 Open SWE 的 Web/研究、协作、规划、PR 工作和可选服务端集成工具。

真正的状态边界按线程划分，而不是按图对象划分：每次运行都会新建 Agent 工厂，但沙箱身份和运行元数据会随 LangGraph 线程持久化。`ensure_sandbox_for_thread` 会复用、ping、重连或重新创建后端；`agent/utils/sandbox_state.py` 维护进程缓存，持久化元数据保存沙箱 ID。该设计**依赖[集成与安全](integrations-security-zh.md)中的执行和身份控制**。

`get_agent` 中的中间件顺序是有意安排的，包含输入清理、模型调用限制、工具错误处理、子目录 `AGENTS.md` 上下文、任务重试/产物/PR/工作流保护、GitHub 代理刷新、消息/状态处理、超时完成行为、模型回退、计划模式限制和提供商消息清理。改变顺序应视为行为/安全变更，而不是格式调整。

## 评审器与分析器图

`agent/reviewer.py:get_reviewer_agent` 中的评审器工厂是另一张图，拥有评审专用提示词和工具集。准备阶段会获取 PR/diff 上下文、变更行、已有评审线程、仓库风格、目标仓库 `AGENTS.md`、组织指南和追踪上下文。发布前会校验发现是否落在变更行集合内。评审器明确排除编码 Agent 的提交/推送/创建 PR 路径；其生命周期见[工作流](workflows-zh.md)。

`agent/analyzer.py:get_analyzer` 运行启动或持续的评审风格分析。它通过 `CompositeBackend` 在沙箱之上叠加虚拟 `StateBackend` `/skills/` 路由，暴露操作手册但不将其写入任务仓库。它的窄工具集会保存评审风格提示词并读取评审结果，生成的风格会成为后续评审运行的上下文。

## 变更指南

- **新增图：**在 `agent/graphs/` 或适当的工厂中实现入口点，然后在 `langgraph.json` 注册；只有存在外部触发器时才添加路由/调度集成。
- **修改编码 Agent 策略：**同时检查 `get_agent`、`agent/prompt.py`、已注册工具和完整中间件顺序，并在 `tests/agent/`、`tests/middleware/` 或 `tests/tools/` 中添加针对性测试。
- **修改评审器行为：**结合 `agent/reviewer.py` 与 `agent/review/` 检查，并参考[运维与质量](operations-quality-zh.md)中的基准。保留评审器按 diff 范围、只读的边界。
- **修改线程语义：**从 `agent/dispatch.py` 和 Webhook 线程 ID 辅助函数开始；破坏确定性 ID 会改变后续消息是否恢复已有工作。

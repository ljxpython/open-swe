---
type: Project Guide
title: Open SWE 快速开始
description: Open SWE 入口文档：一个基于 LangGraph 和 Deep Agents、支持 Slack、Linear、GitHub 与仪表盘工作流的内部编码 Agent 框架。
resource: /README.md
tags: [open-swe, architecture, agent-platform, quickstart]
---
# Open SWE 快速开始

Open SWE 是面向组织内部编码 Agent 的框架。它结合 LangGraph 运行时、基于 Deep Agents 的编码和评审图、隔离沙箱，以及 Slack、Linear、GitHub 和 Web 仪表盘等协作界面。目标是让 Agent 能接收工程上下文，在仓库中工作，并报告或发布结果，同时不把凭据直接交给执行环境。

开始了解仓库时，先看这些文档：

- [运行时架构](runtime-architecture-zh.md)解释图、FastAPI 组合方式、持久化调度契约和按线程保存的执行状态。
- [工作流](workflows-zh.md)解释编码、评审、规划、审批和评审风格学习如何协同。
- [集成与安全](integrations-security-zh.md)解释沙箱隔离、身份、Webhook 门禁和可选的服务端工具。
- [仪表盘](dashboard-zh.md)解释认证 UI、FastAPI API 边界以及管理员/用户管理界面。
- [运维与质量](operations-quality-zh.md)解释开发命令、CI、E2E 验证、评审器评估和部署资源。

## 产品模型

主编码图在 `agent/server.py` 中通过 `deepagents.create_deep_agent` 组装。它从 Slack 线程、Linear issue、GitHub 交互或仪表盘聊天中接收源码上下文；针对与线程关联的沙箱工作；并使用经过刻意筛选的协作与研究工具。[运行时架构](runtime-architecture-zh.md)描述图工厂和持久化线程行为。

Open SWE 将代码评审与代码修改分离。只读评审图会准备 PR 专属检出目录和变更行上下文，然后发布发现；分析器图则学习仓库专属的评审风格。这些面向用户的流程记录在[工作流](workflows-zh.md)中，评估方式记录在[运维与质量](operations-quality-zh.md)中。

Web 仪表盘不是独立后端：它是通过 FastAPI 端点工作的 Vite/TanStack 客户端，而这些端点与 Webhook 位于同一个应用中。它提供 Agent 线程、计划、调度、评审管理、个人资料和工作区设置，详见[仪表盘](dashboard-zh.md)。

## 仓库地图

| 区域 | 源码锚点 | 重要原因 |
|---|---|---|
| Agent 运行时 | `agent/server.py`、`agent/graphs/`、`langgraph.json` | 组装编码图并注册已部署的助手。 |
| 入口与调度 | `agent/api/app.py`、`agent/webhooks/`、`agent/dispatch.py` | 校验传入事件并创建持久化 LangGraph 运行。 |
| 评审系统 | `agent/reviewer.py`、`agent/review/`、`agent/analyzer.py` | 实现按 diff 范围评审和按仓库维护评审风格。 |
| 仪表盘 | `agent/dashboard/`、`ui/src/` | 负责基于 OAuth/会话的管理 API 及客户端 UI。 |
| 执行边界 | `agent/utils/sandbox.py`、`agent/integrations/`、`Dockerfile` | 选择隔离执行提供商并配置沙箱镜像。 |
| 验证 | `tests/`、`tests/e2e/`、`evals/reviewer/`、`.github/workflows/ci.yml` | 覆盖 Python 行为、端到端流程和评审质量。 |

## 首次修改：从哪里开始

- **修改核心 Agent 行为：**从 `agent/server.py` 开始，然后阅读[运行时架构](runtime-architecture-zh.md)和[集成与安全](integrations-security-zh.md)。中间件顺序和工具列表是运行策略，不是偶然的连线。
- **新增或修改来源工作流：**从 `agent/webhooks/` 和 `agent/dispatch.py` 开始，然后沿着[工作流](workflows-zh.md)阅读。保留签名校验、来源/上下文构造、确定性线程身份和持久化调度。
- **修改评审行为：**从 `agent/reviewer.py` 和 `agent/review/` 开始；遵循[工作流](workflows-zh.md)，并运行[运维与质量](operations-quality-zh.md)中的针对性检查。
- **修改 UI 或仪表盘 API：**使用[仪表盘](dashboard-zh.md)将路由、类型化 UI 客户端和受保护的 FastAPI 端点作为一个整体定位。

## 本地开发基线

Python 依赖使用 `uv`，Python 测试使用 pytest，Lint/格式化使用 Ruff。核心命令如下：

```bash
make install
make dev                 # LangGraph 开发服务器：图 + FastAPI 应用
make lint
make format-check
make test
```

`make run` 只启动 `uvicorn agent.webapp:app`，适合仅处理 HTTP 的工作；Agent/仪表盘运行时通常需要 `make dev`。UI 在 `ui/package.json` 中有独立的 `pnpm` 脚本。实际的测试选择和 E2E 配置见[运维与质量](operations-quality-zh.md)。

## 文档说明

当前代码以及维护中的安装/定制指南是事实来源。`AGENTS.md`、`CLAUDE.md` 和 `docs/CUSTOMIZATION.md` 的部分内容在图注册、中间件和沙箱细节上与源码不一致；出现差异时，本 Wiki 以 `langgraph.json` 和当前实现路径为准。

## 待办

- **完整配置参考** —— `docs/INSTALLATION.md`；暂缓，因为它是涉及多种提供商和敏感凭据的较大安装指南，继续由现有安装文档维护更合适。
- **逐工具 API 参考** —— `agent/tools/`；暂缓，因为工具行为经过刻意筛选且变化频繁。需要修改某个工具时，从 `agent/server.py` 或 `agent/reviewer.py` 中注册的工具列表开始。

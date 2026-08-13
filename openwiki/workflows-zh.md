---
type: Workflow Guide
title: 编码、评审、规划与学习工作流
description: Open SWE 编码任务、PR 评审、计划审批、仓库评审风格学习和后续执行的端到端工作流。
resource: /agent/webhooks
tags: [open-swe, workflows, slack, linear, github, review]
---
# 编码、评审、规划与学习工作流

## 编码任务工作流

编码任务可以来自 Slack 提及/线程、Linear issue 交互、GitHub 评论或仪表盘聊天。按来源划分的 Webhook/路由会构造相关上下文，并解析确定性线程 ID，使同一来源的后续消息回到同一工作。所有来源都使用[运行时架构](runtime-architecture-zh.md)中的持久化调度器，它会中断并恢复活动任务以接收后续输入。

随后，编码图准备 Agent：解析身份/模型设置，创建或重新连接线程沙箱，并将任务/来源上下文纳入提示词。Agent 应端到端完成工作——检查并修改目标仓库、验证改动、提交/推送、在允许时创建或更新草稿 PR，并通过来源渠道回复。系统特意没有通用的运行后钩子替 Agent 创建 PR。

Slack 和 Linear 既是协作界面，也是入口。中间件和来源工具支持进度/状态回复，并接收 Agent 运行期间到达的消息。GitHub 流程还支持 PR 评论处理和仓库策略。[集成与安全](integrations-security-zh.md)解释了让这些工作流足以安全对外开放的身份和授权门禁。

## 规划与工作流审批

主 Agent 具备计划模式工具（`enter_plan_mode` 和 `save_plan`），FastAPI 通过 `agent/dashboard/plan_api.py` 挂载计划路由。仪表盘在 Agent 线程路由中呈现计划评审，因此用户可以检查并回应提议方案，而不是让每个请求都立即修改仓库。

工作流文件推送会走更严格的路径：Slack 路由包含明确的负责人审批流程，FastAPI 还挂载 `agent/dashboard/workflow_approval_api.py`。该审批流程**由[仪表盘](dashboard-zh.md)呈现**，但仍由后端路由/中间件强制执行；不能只用客户端可见性替代它。

## Pull Request 评审工作流

GitHub PR 事件可以调用 `reviewer` 图。评审路由会对事件执行门禁，并派生评审器专属的确定性线程。评审准备阶段收集 PR 元数据和 diff，计算有效变更行集合，读取既有评审线程和已配置的指南/风格，然后启动评审 Agent。

评审器通过评审专用工具（`add_finding`、`update_finding`、`list_findings`、`publish_review`）维护持续演进的发现，并且只发布有效且可渲染的发现。其提示词和预处理把 PR 文本及历史线程内容视为不可信数据。它不会获得编码图的写入/PR 编写工具链。这种分离**依赖[集成与安全](integrations-security-zh.md)中的服务端信任边界**，并通过[运维与质量](operations-quality-zh.md)中描述的基准评估。

## 评审风格学习

`analyzer` 图以两种模式构建仓库专属风格提示词：

- **启动：**检查历史评审信号，建立初始风格。
- **持续：**使用本评审器发现的结果持续改进风格。

`agent/skills/bootstrap-repo-analysis/` 和 `agent/skills/continual-learning/` 下的技能定义这两种模式。`agent/dashboard/review_style_jobs.py` 中的仪表盘任务会启动分析，`agent/dashboard/analyzer_cron.py` 支持启动后的定期优化。仪表盘允许管理员管理生成的仓库风格，把这个工作流连接到[仪表盘](dashboard-zh.md)。

## CI 自动修复与定时工作

检查失败或可操作的评审反馈等 GitHub 信号，可以在仓库、用户/个人资料和 PR 级别选择启用后进入项目的 CI 自动修复流程。源码位于 Webhook 处理及相关 Agent/仪表盘模块附近；修改时必须格外谨慎，因为它可能在 Agent 创建的 PR 上触发后续编码运行。

`scheduler` 图和仪表盘计划支持延迟或周期性工作。一次性唤醒任务的运维清理由 `scripts/purge_wakeup_crons.py` 负责；部署/验证指南见[运维与质量](operations-quality-zh.md)。

## 变更指南

- **新增调用来源：**实现经过校验的路由和处理器，创建确定性 ID，传递清晰的 `configurable` 上下文，并通过 `dispatch_agent_run` 调度。在 `tests/webhooks/`、`tests/slack/`、`tests/github/` 或对应目录添加来源专属测试。
- **修改提示词或输入上下文：**同时检查来源处理器、`agent/prompt.py` 和工具回复行为。外部系统输入必须保持为数据，不能成为覆盖系统策略的指令。
- **修改评审发布：**更新 `agent/reviewer.py`、`agent/review/findings.py` 和 `agent/review/publish.py`；运行针对性的评审器测试，质量变更还要运行评审器评估。

---
type: User Interface Architecture
title: 仪表盘与工作区管理
description: Open SWE 的 Vite/TanStack 仪表盘、FastAPI API 边界、经认证的 Agent 工作流和工作区管理功能。
resource: /ui/src
tags: [open-swe, dashboard, ui, fastapi, administration]
---
# 仪表盘与工作区管理

## 客户端与 API 边界

仪表盘是位于 `ui/` 的 Vite/TanStack Start 应用，文件路由位于 `ui/src/routes/`。它是经认证的客户端，不是独立的应用后端：`ui/src/lib/api.ts` 和 `ui/src/features/agents/lib/api.ts` 会携带凭据调用 FastAPI 的 `/dashboard/api/*` 端点。

对应后端是 `agent/dashboard/routes.py`，由 `agent/api/app.py` 与 Webhook、计划/审批路由一起挂载。因此会话、管理员权限和变更来源规则始终由服务端强制执行。此界面**依赖[运行时架构](runtime-architecture-zh.md)中的 FastAPI 组合方式**，修改时应将客户端与服务端作为一对一起处理。

Vercel 的 `ui/vercel.json` 会将 `/dashboard/api/*` 重写到托管的 LangGraph 应用，以便生产环境保持同源运行。本地开发可以让客户端指向独立的 API 基础 URL，但必须显式配置 CORS。安装细节仍以 `docs/INSTALLATION.md` 为准。

## 面向用户的工作流

`/` 会重定向到 `/agents`，这是主要的认证工作区。Agent 区域包括：

- 入口/新建任务页面（`ui/src/routes/agents/index.tsx`）；
- 流式线程、聊天和计划视图（`agents/$threadId.tsx`、`agents/$threadId_.plan.tsx`）；
- 可搜索的线程历史（`agents/threads.tsx`）；
- 自动化/计划视图（`agents/automations/`）；
- PR 评审历史与详情（`agents/reviews/`）。

Agent 客户端通过仪表盘端点流式处理并管理线程，从而**呈现[工作流](workflows-zh.md)中的持久化编码流程**。用户设置包括 GitHub 关联偏好、Slack 映射、通知和集成。仓库指令与快照视图位于 Agent 区域，把工作区配置连接到提示词和沙箱运行时。

## 评审与管理员管理

评审工作区用于配置启用的仓库、组织级指导、草稿评审策略、摘要、模型默认值和仓库专属评审风格。即使面向读取的页面通常可见，后端仍会用管理员授权保护所有变更操作。

`/admin` 明确要求管理员权限。它包括工作区模型/网关配置、触发评审、中断运行中的 Agent、可观测性凭据、PR 追踪处理和用户映射。`/admin/evals` 展示评估存储提供的最新评审器评估状态/日志。因此，UI 成为[工作流](workflows-zh.md)及[运维与质量](operations-quality-zh.md)中所述评审风格和评估生命周期的操作控制面板。

## 变更指南

对于同时涉及仪表盘和后端的功能：

1. 在 `ui/src/routes/` 找到相关文件路由，并定位 `ui/src/features/` 或 `ui/src/components/` 下的功能组件。
2. 在 `ui/src/lib/api.ts` 或 `ui/src/features/agents/lib/api.ts` 中新增或调整类型化客户端方法。
3. 在 `agent/dashboard/routes.py` 及 `agent/dashboard/` 下对应的专用模块中加入受保护端点和领域逻辑。
4. 将授权/同源检查保留在后端；不要依赖隐藏 UI 控件来提供安全保障。
5. 不要手动编辑 `ui/src/routeTree.gen.ts`，它是生成文件。

修改 UI 时运行 UI 单元测试、类型检查、Lint 和构建，并运行针对性的 Python 仪表盘测试。[运维与质量](operations-quality-zh.md)列出了这些命令及真实仪表盘 E2E 测试工具。

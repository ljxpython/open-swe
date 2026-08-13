---
type: Security Architecture
title: 集成、执行边界与安全控制
description: Open SWE 沙箱、GitHub 身份、Webhook 校验、仪表盘会话以及可选服务端 MCP/可观测性工具的安全与集成模型。
resource: /agent/integrations
tags: [open-swe, security, integrations, sandbox, authentication]
---
# 集成、执行边界与安全控制

## 隔离边界：按线程划分的沙箱

Open SWE 在由 `SANDBOX_TYPE` 选择的沙箱后端中运行仓库任务。`agent/utils/sandbox.py` 为 LangSmith（默认）、Daytona、Modal、Runloop、E2B 和本地执行注册工厂。本地提供商明确不提供隔离，只适合开发环境，不应视为生产等价配置。

编码图会让一个线程在后续消息中复用同一个沙箱。进程内存缓存后端对象，LangGraph 线程元数据保存持久化沙箱 ID。重连逻辑会按需 ping、复用、重连或重新创建后端。该执行生命周期**支撑[运行时架构](runtime-architecture-zh.md)中的编码与评审路径**，让对话式任务能够保留工作环境。

默认的 LangSmith 集成配置 GitHub 代理，而不是把真实 GitHub token 放进沙箱。针对 `github.com` 的 Git 操作和针对 `api.github.com` 的 API 操作，会使用新鲜的 GitHub App 安装 token 获得相应的代理认证。其他沙箱提供商不会自动继承这条代理路径，因此新增提供商必须明确说明凭据边界。

## GitHub 身份与仓库访问

Open SWE 以双模式解析 GitHub 身份：优先使用通过 OAuth/会话路径提供的用户 token，必要时回退到 GitHub App 安装 token。用户和凭据处理都在服务端完成；仪表盘提供 GitHub OAuth 端点，Agent 的 token 解析由 `agent/utils/auth.py` 及相关工具实现。

GitHub 事件路由会在调度前校验签名。Slack 和 Linear 路由同样校验提供商请求，Webhook 路由还会在创建任务前执行仓库/来源门禁。确定性线程 ID 随后把后续消息连接到正确的运行。这些检查**保护[工作流](workflows-zh.md)中的外部入口流程**；新增触发器必须保留这些检查，不能直接调用图。

## 仪表盘与 API 防护

FastAPI 应用只对已配置来源启用 CORS，启用凭据时会拒绝 `*`。仪表盘 API 使用会话和管理员依赖，变更路径还会执行同源保护。[仪表盘](dashboard-zh.md) UI 是 `/dashboard/api/*` 的带凭据客户端，不负责做授权决策。

生产环境的 Vercel 配置会将 `/dashboard/api/*` 重写到托管后端，使仪表盘 Cookie 和 API 调用保持同源。只有在前后端 URL 与来源都明确协调配置时，才支持直接跨源部署；设置步骤见 `docs/INSTALLATION.md`。

## 可选服务端工具

`agent/server.py` 可以加载 Datadog、LangSmith、Corridor、Notion、Currents 和浏览器工具等集成。它们是服务端能力，不是通用沙箱凭据：

- 可观测性工具只对获授权用户开放，因为日志/追踪可能受攻击者影响，并包含敏感的组织上下文。
- Datadog 和 LangSmith 凭据保留在服务端，不复制到任务沙箱。
- Corridor 会校验允许的 HTTPS 端点，并只暴露计划分析工具。
- Notion 访问按用户隔离，并在调用时刷新 OAuth token。

这些集成扩展了[运行时架构](runtime-architecture-zh.md)中描述的编码图，因此新增集成需要进行威胁建模审查：最小权限范围、明确的用户资格、不可信工具输出处理，以及被拒绝/无效路径测试。

## 安全扩展清单

1. **沙箱提供商：**在 `agent/integrations/` 下实现兼容 `SandboxBackendProtocol` 的工厂；在 `agent/utils/sandbox.py` 注册；测试创建/重连，并说明 Git 凭据如何隔离。
2. **Webhook 来源：**在路由边界校验签名；验证允许的仓库/用户；把远程文本视为不可信；构造确定性线程 ID；使用 `agent/dispatch.py`。
3. **MCP/外部能力：**凭据保留在服务端，只暴露必要的窄范围工具，限制可加载用户，并将外部内容按不可信数据清理/审查。
4. **仪表盘变更：**先实现后端授权和同源检查，再添加[仪表盘](dashboard-zh.md)中描述的类型化 UI 客户端和路由。

## 验证重点

安全敏感变更应配套 `tests/auth/`、`tests/github/`、`tests/slack/`、`tests/webhooks/`、`tests/sandbox/` 和 `tests/tools/` 下的针对性测试。更完整的命令矩阵、真实流程 E2E 测试和 CI 检查见[运维与质量](operations-quality-zh.md)。

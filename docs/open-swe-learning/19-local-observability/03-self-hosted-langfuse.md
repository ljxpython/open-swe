# 第 3 章：免费自托管 Langfuse

## 学习目标

本章选择 Langfuse 作为生产 Agent 可观测性的第一套 UI：查看一次 Run 的模型调用、prompt、token、工具调用、错误和耗时。目标不是在本章立刻改 Open SWE，而是先明确部署边界和接入位置，避免把本地 PyCharm 调试、LangSmith Sandbox 和生产 trace 系统混在一起。

## 1. 为什么选自托管 Langfuse

Langfuse 的软件是开源的，适合把 Agent trace 放在自己控制的基础设施中；它不是“零成本”，仍需要机器、磁盘、备份和运维。Langfuse Cloud 可能提供免费额度，但额度和套餐会变化；若要求数据不出自有环境且不依赖用量套餐，选择自托管。

| 方案 | 软件费用 | 运行成本 | 适合当前目标 |
| --- | --- | --- | --- |
| Langfuse Cloud | 以官网当前套餐为准 | 无需维护服务 | 快速试用 |
| Langfuse 自托管 | 开源软件免费 | 自己承担服务器、存储、备份 | 推荐：生产 Agent trace |
| OpenTelemetry + Grafana | 开源软件免费 | 自己搭多套后端和面板 | 后续统一平台时再考虑 |

Langfuse 的优势是已有 Agent 视图，不必先自行实现 trace 查询页面、模型 token 面板、prompt 对比和工具时间线。

## 2. 在 Open SWE 中的数据模型

将现有运行标识映射到 Langfuse：

```text
LangGraph thread_id  -> Langfuse Session：同一用户/任务的连续对话
LangGraph run_id     -> Trace metadata：本次 Runtime Run 的关联 ID
Agent graph 根调用   -> Langfuse Trace：一次 Agent 执行的时间线
每次模型调用         -> Generation：prompt、响应、模型、token、耗时、错误
工具调用             -> Tool / Span：参数摘要、输出摘要、耗时、错误
Sandbox / GitHub PR  -> 自定义 Span：资源标识和副作用状态
```

推荐标签和元数据：

```text
session_id  = thread_id
trace_name  = open-swe-agent / open-swe-review / open-swe-chat
user_id     = github_login（确认其可用于内部审计后）
tags        = source、graph_id、environment
metadata    = run_id、模型 ID、effort、sandbox 类型、代码版本
```

`thread_id` 用于聚合同一会话，`run_id` 用于定位一次执行，不能只保留其中一个。Token、Cookie、GitHub token、完整私有仓库内容不能作为 metadata；Langfuse metadata 是可查询数据，不等于私密临时变量。

## 3. 部署：先用官方 Docker Compose

自托管时优先使用 Langfuse 官方部署模板，不要把一大段第三方 Compose 文件复制进 Open SWE 仓库。当前官方文档将 PostgreSQL、ClickHouse、Redis 和 S3 兼容对象存储列为自托管依赖；开发可用本机 Docker，生产应使用持久卷、定期备份和仅内网可达的服务地址。

部署步骤：

1. 在单独的基础设施目录或服务器按官方 Docker Compose 文档启动 Langfuse。
2. 创建一个项目，例如 `open-swe-production`，生成 project public key 和 secret key。
3. 将 Langfuse UI/API 置于内网域名和 HTTPS 后，限制只有 Runtime 与获授权运维人员可访问。
4. 在 Open SWE 的部署密钥系统中保存 key，不提交到 `.env`、代码库或 sandbox。

官方参考：

- [Langfuse self-hosting](https://langfuse.com/docs/deployment/self-host)
- [Langfuse Python SDK](https://langfuse.com/docs/observability/sdk/python/overview)
- [Langfuse LangChain integration](https://langfuse.com/docs/integrations/frameworks/langchain)

## 4. Open SWE 的最小接入设计

Langfuse Python SDK 当前支持 `langfuse.langchain.CallbackHandler`。它可以由 LangChain 的 callback 机制自动捕获链、模型和工具调用；因此第一版不需要在每个 `read_file`、`edit_file` 工具中手写重复埋点。

接入必须在 **Runtime 服务端** 发生，不能把 callback 对象通过 `langgraph_sdk` 从 [`scripts/debug_open_swe_run.py`](../../../scripts/debug_open_swe_run.py) 发送过去。SDK 脚本只负责创建 Run；真正的 graph、模型和工具在 Runtime 进程中执行。

当前最小改动位置是 [`agent/utils/tracing.py`](../../../agent/utils/tracing.py) 的 `traced_graph_factory()`：它正好包住 `agent`、`reviewer`、`analyzer` 和 `chat` 的 graph 工厂。未来新增一个显式开关的 Langfuse helper，在这里为返回的 `Pregel` graph 注入 callback 与 metadata：

```python
from langfuse.langchain import CallbackHandler

callback = CallbackHandler()
graph = graph.with_config(
    {
        "callbacks": [callback],
        "metadata": {
            "langfuse_session_id": thread_id,
            "langfuse_user_id": github_login,
            "langfuse_trace_name": project_name,
            "langfuse_tags": [source, graph_id],
            "run_id": run_id,
        },
    }
)
```

这是设计示意，不应直接复制：实施时必须从实际 `RunnableConfig` 安全提取 `thread_id`、`github_login`、`source` 和 Runtime 的 `run_id`，并保留原有 callbacks、metadata，而不是覆盖它们。Langfuse SDK 4.14.4 已验证可识别 `langfuse_session_id`、`langfuse_user_id`、`langfuse_trace_name` 和 `langfuse_tags` metadata；实际接入时将版本固定在 `<5` 并添加聚焦测试。

第一版自动得到：模型调用、LangChain chain、已纳入 callback 的工具调用。第二版再为 sandbox 创建/恢复、PR 创建、Slack/Linear 发送、调度创建增加自定义 span，因为这些副作用不一定完整暴露为 LangChain tool 生命周期。

## 5. 配置与默认关闭

新增接入时采用“没有明确开关和完整 key 就完全不初始化”的规则：

```dotenv
OPEN_SWE_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=<project-public-key>
LANGFUSE_SECRET_KEY=<project-secret-key>
LANGFUSE_BASE_URL=https://langfuse.internal.example
LANGFUSE_TRACING_ENVIRONMENT=production
```

Langfuse SDK 使用 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和 `LANGFUSE_BASE_URL`；`LANGFUSE_TRACING_ENVIRONMENT` 用于区分 `development`、`staging`、`production`。不要将这些 key 放进客户端、浏览器或 sandbox；它们只应存在 Runtime 服务的密钥注入环境。

为避免私有代码默认外流，第一版策略应是：

| 数据 | 第一版策略 |
| --- | --- |
| run/thread ID、模型、token、耗时、错误类别 | 记录 |
| 工具名、参数摘要、结果摘要、副作用状态、PR URL/commit SHA | 记录并脱敏 |
| 完整 prompt、完整模型响应、完整工具输入/输出 | 默认关闭；经脱敏与访问控制后按环境打开 |
| OAuth/GitHub token、Cookie、Authorization header | 永不记录 |

## 6. 不要混淆 Langfuse tracing 与 LangSmith Sandbox

本项目的 [`agent/utils/tracing.py`](../../../agent/utils/tracing.py) 目前通过 `langsmith.tracing_context()` 设置 LangSmith project；而 `SANDBOX_TYPE=langsmith` 是另一件事，它使用 LangSmith Sandbox 提供执行环境。Langfuse 可以替代 trace 后端，但不会替代 LangSmith Sandbox。

本地当前使用 `SANDBOX_TYPE=local` 时，可以独立评估 Langfuse。若生产仍用 `SANDBOX_TYPE=langsmith`，仍需 LangSmith 凭据来创建 sandbox；此时可以逐步关闭或限制 LangSmith tracing，但不能因为接入 Langfuse 就删除 sandbox 所需的 LangSmith 配置。

## 7. 实施顺序与验收

1. 自托管 Langfuse，创建测试项目和 Runtime 专用 key。
2. 新增 `langfuse>=4.14,<5` 依赖及默认关闭的 helper，不改现有 LangSmith/Sandbox 行为。
3. 在 `traced_graph_factory()` 合并 callback 和 metadata，确保已有 callbacks 不丢失。
4. 用只读 `read_file` Run 验证：一个 Session 内按 `thread_id` 聚合，Trace 含模型 Generation 与工具 Span。
5. 补副作用自定义 span，并验证 PR/Slack 超时后能记录 `success`、`failed` 或 `unknown`，而非把未知状态误判为失败。
6. 再决定是否开放完整 prompt/输出，配置脱敏、保留期限、访问角色和删除流程。

验收不只看“Langfuse UI 有数据”。从一个 `thread_id` 必须能查到每次 `run_id`、模型调用、工具调用、错误和副作用资源 ID；关闭 `OPEN_SWE_LANGFUSE_ENABLED` 后，Runtime 必须不初始化 Langfuse，也不影响 Agent 正常运行。

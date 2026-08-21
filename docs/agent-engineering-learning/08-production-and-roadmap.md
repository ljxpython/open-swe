# 08. 生产化落地清单

## 学习目标

把前面几章组合成实际建设顺序：从一个可验证的 Agent，逐步走向可用的内部系统，再到能承载高权限工作的生产部署。

## 概念全解

“生产级”不是装上 Docker 就结束。对 Agent 来说，生产化的关键是：任务不丢、权限不乱、失败能解释、外部副作用可审计、成本能看见。

Open SWE 已经给出了很多正确方向：统一 durable dispatch、同步 checkpoint、每线程 Sandbox、Webhook 签名、OAuth/App token、工具守卫、模型超时/回退、Reviewer 的只读隔离、LangSmith tracing，以及生产部署文档。

但不要把源码存在等同于“你的部署自动安全”。项目文档明确要求生产 Runtime 配 PostgreSQL、Redis、密钥、网络边界，并提醒 `noop` 认证不可直接暴露公网。

## 架构图

![生产 Agent 设计蓝图](architecture/png/02-agent-design-blueprint.png)

[Draw.io](architecture/02-agent-design-blueprint.drawio) · [HTML](architecture/html/02-agent-design-blueprint.html)

图底部六步是上线时最值得逐项验证的链路：触发、验证、创建 durable run、准备上下文、Agent loop、安全执行。任一步没有可观测性，事故时就会变成“模型好像没反应”。

## 综合案例：把“修复 PR CI”做成系统

目标：用户在 GitHub PR 评论 `@open-swe fix CI`，Agent 诊断失败、修改代码、测试、创建/更新 PR，并报告结果。

```text
1. GitHub Webhook 到 FastAPI：校验 HMAC 签名、检查仓库 allowlist。
2. 由 PR/评论派生稳定 thread_id：同一 PR 的后续讨论回到同一任务上下文。
3. dispatch 创建 durable run：sync checkpoint，后续消息 interrupt 并续跑。
4. get_agent 按线程准备：解析触发用户、模型、仓库指令、Sandbox 与 prompt。
5. Agent loop：读 CI 日志 -> 读代码 -> 修改 -> 运行定向测试 -> 观察结果。
6. 外部副作用：只能用 open_pull_request / 受控 GitHub 工具；PR 守卫阻止 Shell 绕过。
7. UI/Slack/GitHub：订阅 stream，最终得到状态、变更、测试和 PR 链接。
```

对应源码：入口在 [agent/api/app.py](../../agent/api/app.py:32) 与 `agent/webhooks/`；派发在 [agent/dispatch.py](../../agent/dispatch.py:113)；主装配在 [agent/server.py](../../agent/server.py:951)；安全约束在 `agent/middleware/`；生产依赖在 [docs/INSTALLATION.md](../INSTALLATION.md:657)。

这个案例刻意省略了：真实 CI 日志权限、Webhook 重投去重策略、组织审批 UI、Sandbox 的网络 egress 策略。这些都要根据你的业务风险补上。

## 分阶段路线

1. 原型：具备单一任务、2-5 个工具、明确终止条件和工具日志。先不要做多 Agent、长记忆或自动写生产数据。
2. 内部可用：具备用户身份、任务 ID、持久化结果、失败通知和只读/写入分层。先不要做全自动高风险副作用。
3. 团队协作：具备 Thread/Run、可恢复执行、事件流、Sandbox 和仓库/租户隔离。先不要做复杂自主调度或无限重试。
4. 生产高权限：具备 HTTPS/网关、Secret Manager、最小权限、审计、监控告警、备份与演练。不要裸露 Runtime，也不要把长效密钥放进模型上下文。

## 上线验收清单

### 任务可靠性

- 每个外部事件有稳定任务标识和去重方案。
- Run 中断、服务重启、浏览器刷新后能说明任务状态并恢复或安全失败。
- 为模型超时、工具异常、Sandbox 不可达、授权失效分别设计用户可理解的结果。

### 权限与安全

- Webhook 校验签名；Dashboard/API 校验会话和资源所有权。
- 使用 Secret Manager，不在 `.env`、镜像、prompt 或 Sandbox 工作区中散落长期密钥。
- GitHub App/OAuth 使用最小仓库和权限范围；Sandbox 限制网络出口和资源。
- Runtime 在 HTTPS、私网或 API Gateway 后；不把 `LANGGRAPH_AUTH_TYPE=noop` 裸露公网。

### 可观测性与运维

- 记录 thread、run、调用者、模型、工具、错误类别、耗时与成本。
- 监控错误率、队列/运行堆积、checkpoint 失败、Sandbox 不可达、模型延迟与 token 成本。
- PostgreSQL/Redis 做备份、容量管理和恢复演练；不要 scale-to-zero 让后台 worker 消失。
- 有版本发布、回滚、依赖漏洞更新和真实 webhook/Sandbox 的集成测试。

## 与项目的差距

- 项目中已有明确证据：Dockerfile、生产安装说明、Runtime TTL、健康检查、LangSmith tracing、测试、ruff、basedpyright。
- 仍需由部署方完成：网关/WAF、TLS 证书、Secret Manager、HA、备份恢复、SLO、压测和网络策略。

`/health` 当前只是活性检查，不能证明 Postgres、Redis、模型或 Sandbox 已就绪。因此上线时应增加 readiness/依赖检查，而不是仅靠返回 `{"status": "healthy"}`。

## 检查题

1. 为什么“每线程一个 Sandbox”不能替代 checkpoint？
2. 为什么 PR 创建要走专用工具，而不是允许 `gh pr create`？
3. 如果用户在 Agent 跑测试时追加“先别改，给我解释原因”，你会选排队还是 interrupt？为什么？
4. 在你的业务中，哪一项外部副作用必须人工审批？请给出工具参数和最终服务端校验。

## 结束：该学什么，不该照抄什么

值得直接学习的是边界意识：统一入口、统一调度、状态外置、工具化副作用、中间件护栏、权限分层、按目标拆专用 Agent。

不应原样照抄的是规模：如果你现在只有一个只读问答 Agent，先做清晰工具契约、身份和日志；等到确实有长任务、写操作、多人协作或高权限时，再引入 Thread、Sandbox、专用图和更复杂的运维设施。

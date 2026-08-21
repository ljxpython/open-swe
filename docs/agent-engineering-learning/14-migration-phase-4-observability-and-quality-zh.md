# 迁移阶段 4：观测、质量反馈与发布门槛

## 本阶段目标

阶段 0-3 让 Run 能稳定创建、恢复，并且只暴露被允许的能力。阶段 4 解决另一个生产问题：运行失败、结果变差或版本变更后，团队能不能回答“发生了什么、影响了谁、是否应该回滚”。

目标不是把日志堆满，而是建立一条可关联的证据链：

```text
请求 -> thread -> run -> graph/assistant 版本
     -> model call / tool call / subagent
     -> checkpoint / SSE 事件 / 最终状态
     -> 用户反馈 / 业务结果 / 发布决策
```

Open SWE 可借鉴的是这条链路的边界和字段设计；它不是一个完整的指标平台，生产部署仍需要接入自己的日志、指标和告警系统。

## 1. 统一关联键

每条日志、Trace、Run 事件和业务反馈至少要能关联以下字段：

| 字段 | 作用 | 是否可暴露给前端 |
| --- | --- | --- |
| `request_id` | 一次 HTTP/Webhook 请求 | 可以 |
| `thread_id` | 对话或业务对象的长期身份 | 可以，按租户权限 |
| `run_id` | 一次具体执行 | 可以 |
| `assistant_id` | 选择哪个 Graph | 可以 |
| `assistant_version` | 解释当时运行的图、提示词和工具 | 可以显示摘要 |
| `project_id` / `actor_id` | 权限和租户隔离 | 仅授权查询 |
| `source` | Slack、Linear、GitHub、Dashboard、cron 等入口 | 可以 |
| `parent_run_id` | 关联父 Run 和子 Agent | 仅调试/审计 |

`thread_id` 不能代替 `run_id`。一个 Thread 可以有多次运行；否则无法区分用户的两次输入，也无法统计单次 Run 的耗时和重试。

## 2. Tracing：记录一次运行的完整上下文

Open SWE 的 `agent/utils/tracing.py` 通过 `langsmith.tracing_context(project_name=...)` 按 Graph 入口选择追踪项目：

[tracing.py:14](../../agent/utils/tracing.py:14)

迁移时保留“按 Graph 分项目”的思路，但不要把敏感输入原文全部写入 Trace：

```text
Trace
  ├─ Run span：thread_id、run_id、assistant、版本、source
  ├─ Model span：模型、attempt、耗时、输入/输出 token、错误类型
  ├─ Tool span：工具名、参数摘要、权限结果、副作用结果
  ├─ Subagent span：parent_run_id、子任务、子模型、终态
  └─ Finalizer span：最终状态、错误码、通知结果
```

建议保留：版本号、参数 hash、长度、错误码、耗时和结果摘要；默认脱敏：完整 Prompt、OAuth Token、GitHub Token、仓库机密、用户隐私字段。

不要只在模型调用处打 Trace。没有 Run、工具和最终状态的父子关系，排查“模型成功但 UI 没结果”仍然要靠猜。

## 3. Run metadata 与配置快照

`agent/dispatch.py:create_durable_run` 接受 `source`、`config` 和 `metadata`，并将 `stream_resumable`、`durability`、完成 webhook 等运行语义集中设置：

[dispatch.py:113](../../agent/dispatch.py:113)

阶段 4 要求 Run 创建时固定保存有效配置，而不是保存“用户当时提交了什么”：

```text
assistant_id / assistant_version
model_id / reasoning_effort
system_prompt_version
tool_profile_version
runtime_options_hash
project_id / actor_id / source
durability / multitask_strategy
```

三类数据分开保存：

| 数据 | 保存位置 | 生命周期 |
| --- | --- | --- |
| Thread metadata | Thread Store | 长期，供 UI 检索和恢复 |
| Run metadata | Run/审计表 | 不可变，解释一次运行 |
| Trace tags | Trace 系统 | 按保留策略采样或归档 |

配置快照不应被后续 Profile 更新覆盖。默认模型改了，历史 Run 仍必须显示原来的模型和策略版本。

## 4. 运行指标：先做能驱动决策的最小集合

不要一开始设计几十个指标。第一批只需要能回答可用性、成本、性能和质量四类问题：

| 类别 | 指标 | 用途 |
| --- | --- | --- |
| 可用性 | Run success / failed / timed_out / cancelled | 判断是否真正完成 |
| 可恢复性 | checkpoint resume、SSE reconnect、duplicate start | 验证阶段 0-2 的目标 |
| 性能 | queue latency、run duration、model latency、tool latency | 定位慢在排队、模型还是工具 |
| 成本 | input/output tokens、model attempts、工具调用次数 | 控制预算和异常循环 |
| 质量 | 用户采纳、重试提问、人工审批拒绝、业务结果 | 判断结果是否有用 |

所有计数都要按 `assistant_id`、版本、模型、项目和入口分组；跨租户聚合时要做权限和隐私隔离。

### 最小告警

```text
连续 Run 失败率超过阈值
单模型 timeout/429 突增
duplicate start 或重复外部写入出现
SSE 恢复失败率升高
某个 assistant 新版本质量显著下降
completion webhook 长时间未确认
```

告警只指向可执行动作：暂停灰度、切回上一版本、禁用某工具或扩大容量。只发“错误很多”的告警没有价值。

## 5. 子 Agent 必须可观测

Open SWE 使用 Deep Agents 的 `task` 委派子 Agent，并为子 Agent 单独设置模型超时和失败重试。父 Run 只看到最终摘要时，排查会缺少关键证据。

每个子 Agent 至少记录：

```text
parent_run_id
child_run_id
subagent_name / subagent_version
model_id / attempt
started_at / ended_at
tool_calls / token_usage
status / error_code
```

父 Run 的耗时、Token 和失败统计要能展开到子 Run，但不能把子 Run 的完整内部 Prompt 默认展示给最终用户。

如果子 Agent 失败，父 Agent 必须收到结构化结果：`status=failed`、`error_code`、是否可重试、是否已经产生外部副作用。不能只返回一段“子任务失败”的自然语言。

## 6. 质量反馈闭环

质量不能只看“模型输出了文本”。建议把反馈分成三层：

| 层级 | 例子 | 记录方式 |
| --- | --- | --- |
| 交互信号 | 用户追问、重新运行、停止、点赞/点踩 | 关联 `thread_id` + `run_id` |
| 人工决策 | 审批通过/拒绝、Reviewer finding 是否采纳 | 记录 actor、时间和对象 |
| 业务结果 | PR 合并、测试通过、工单解决、任务失败 | 由业务系统回写结果 |

反馈必须区分“模型质量差”和“工具/权限/上游失败”。否则把网络 504 算成模型差，会错误地回滚模型版本。

推荐为每次 Run 计算一组可解释标签，而不是一个神秘总分：

```text
completed_with_output
completed_with_external_effect
human_approved
user_retried
tool_error
upstream_error
side_effect_unknown
```

这些标签可以用于离线评估、灰度比较和问题聚类，不应直接作为模型训练数据而不经过脱敏和人工抽样。

## 7. 版本化与灰度发布

一次可审计的 Agent 版本至少由以下内容组成：

```text
graph code revision
system prompt revision
tool profile revision
middleware/config revision
model routing revision
```

发布流程建议：

1. 生成不可变版本 ID，并保存完整配置摘要。
2. 在离线样本上验证成功率、工具错误率、Token 和关键业务指标。
3. 只给一小部分项目或用户灰度。
4. 比较新旧版本的 Run 终态、恢复率、成本和质量反馈。
5. 达到门槛后扩大比例，否则切回上一版本。

回滚只切换“新 Run 使用的版本”，不要删除已有 Thread、Checkpoint、Store 数据。正在运行的 Run 应继续使用创建时的版本，或按明确的中断/恢复策略处理，不能半途换提示词和工具集合。

## 8. 发布验收门槛

阶段 4 完成前，至少要有下面这些可重复检查：

```text
每个 Run 都能从 request_id 找到 thread、run、trace 和最终状态
历史 Run 能显示当时的模型、工具和策略版本
模型调用、工具调用、子 Agent 和 finalizer 可按 run_id 串起来
断线恢复不会生成第二个 Run，重复 start 可证明幂等
失败、超时、取消、审批等待都有稳定错误码和指标
敏感字段不会出现在日志、Trace、SSE 或公开 metadata
灰度版本可以按指标停止，回滚不会删除状态
```

## 9. 推荐实施顺序

### 4.1 先统一字段

先在 Coordinator、middleware、SSE 和 completion webhook 中补齐 `request_id`、`thread_id`、`run_id`、版本和 `source`。字段不统一，后面接任何观测系统都会返工。

### 4.2 再接 Trace 和最小指标

按 Run、Model、Tool、Subagent、Finalizer 建立父子 span，同时记录成功率、延迟、Token 和恢复指标。

### 4.3 再做质量反馈

把用户行为、审批、Reviewer 结果和业务结果写回 Run 关联表，明确区分质量问题与基础设施问题。

### 4.4 最后做灰度门槛

版本 ID、离线样本、灰度比例、自动告警和回滚开关形成发布闭环。没有这些条件，不要把“线上看起来能跑”当成生产验收。

## 不要在阶段 4 做的事

- 不要把完整 Prompt、令牌和仓库机密写进日志或 Trace。
- 不要用一个总分掩盖模型、工具、网络和权限问题。
- 不要让版本回滚删除 Thread、Checkpoint 或 Store。
- 不要为了追求全量观测而记录不可控的高基数字段和完整事件正文。
- 不要在没有稳定关联键之前接入复杂的指标平台。

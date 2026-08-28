# 迁移阶段 1：Run 可靠性、Inbox 与失败收敛

## 本阶段目标

阶段 1 解决 Agent 运行中卡住、空结束、错误不清楚、追加消息丢失的问题：

```text
模型卡住 -> 超时
模型暂时不可用 -> Fallback 或明确失败
工具抛异常 -> 标准 ToolMessage
用户中途补充 -> 进入 Thread Inbox
Run 长期 pending -> Watchdog 回收
```

## 1. Middleware 顺序就是行为契约

Open SWE 在 `agent/server.py` 中按顺序组装 Middleware。迁移时不要只复制类名，要复制“谁包住谁”的语义：

```text
请求/输入校验
-> Run 准备
-> 模型调用限额
-> 工具错误转换
-> 消息队列消费
-> Fallback
-> provider 调用超时
-> Run 终态收尾
```

模型 timeout 必须包住真正的 provider call；Fallback 必须能捕获 timeout；工具 retry 不能包住不可重试的写操作。

## 2. 模型调用超时

Open SWE 的 `ModelCallTimeoutMiddleware` 使用 `asyncio.wait_for` 截断 provider 调用：

[model_call_timeout.py:44](../../agent/middleware/model_call_timeout.py:44)

`ai-agent-platform` 应同时设置三层 deadline：

| 层 | 控制对象 | 超时后的动作 |
| --- | --- | --- |
| HTTP timeout | Platform API 到 Runtime | 返回 upstream timeout |
| Model call timeout | 单次模型请求 | 触发 Fallback 或失败 |
| Run deadline | 整个 Agent Run | 取消 Run 并写入 timed_out |

不能只设置 HTTP timeout；工具循环也不能绕过 Run deadline。

## 3. 模型 Fallback 和限额

Open SWE 的 Fallback Middleware 会在暂时性错误时切换模型，并保留原始异常：

[model_fallback.py:131](../../agent/middleware/model_fallback.py:131)

适合 Fallback 的情况：

- provider 连接失败；
- 429 限流；
- 5xx 暂时性错误；
- 单次 provider timeout。

不应自动 Fallback：

- 参数校验错误；
- 权限错误；
- 工具 schema 错误；
- 用户请求不合法；
- 模型能力不支持当前输入。

同时设置 `max_model_calls`、`max_tokens`、`run_deadline` 和 `fallback_attempts`，每次尝试记录 model、attempt、异常类型和耗时。

## 4. 工具错误标准化

工具异常不要直接让整个 Graph 崩溃：

```text
工具抛异常
  -> 记录 tool_name、run_id、错误码
  -> 转成 ToolMessage
  -> 模型决定修正、重试或结束
```

工具错误至少分为：

```text
invalid_input
permission_denied
dependency_unavailable
timeout
side_effect_unknown
non_retryable_failure
```

## 5. 只对幂等工具重试

Open SWE 的外部请求重试矩阵明确区分幂等和非幂等方法：

[github_http.py:133](../../agent/utils/github_http.py:133)

迁移到 `ai-agent-platform` 时，工具注册表应携带：

```python
ToolPolicy(
    side_effect="read" | "write",
    idempotent=True | False,
    timeout_seconds=30,
    retryable_errors={"timeout", "429"},
    requires_approval=True | False,
)
```

网络超时不代表写操作没有成功；`side_effect_unknown` 状态下不能盲目重试。

## 6. 防止空消息和提前结束

Open SWE 的 `ensure_no_empty_msg` 会拦截空 AIMessage，必要时注入 `no_op` 或完成确认工具：

[ensure_no_empty_msg.py:78](../../agent/middleware/ensure_no_empty_msg.py:78)

平台化时定义通用规则：

```text
没有文本
没有工具调用
没有明确完成状态
  -> 不允许静默成功
  -> 写入 progress_guard 事件
  -> 继续、等待或失败
```

最终用户应该得到“回答完成、等待用户/审批、明确失败”三者之一。

## 7. Thread Inbox：运行中消息不再竞争 Run

Open SWE 在 Store 的 thread namespace 中保存待处理消息，Middleware 在下一次模型调用前读取：

[check_message_queue.py:149](../../agent/middleware/check_message_queue.py:149)

建议 Inbox 记录：

```text
message_id
thread_id
source
actor_id
content
created_at
dedupe_key
claim_status
```

消费顺序：

```text
按 created_at FIFO
读取后 claim
处理成功 ack
处理失败保留 retry 状态
```

不能简单“读完就删除”，否则 Worker 在模型调用前崩溃会导致消息丢失。

### 7.1 入站上下文在接收时固化，写操作前再实时复核

外部消息到达时，先把可审计的来源上下文和消息一起保存：

```text
source_event_id
source
actor_id
resource_ref
received_at
authorization_snapshot
```

同一个 `source_event_id` 必须去重；同一发送者在一个 Run 中的上下文也应合并，而不是每次模型调用都重复改写历史消息。这样 Webhook 重试、多入口转发和运行中补充消息都不会让模型混淆“这句话是谁、针对什么资源说的”。

`authorization_snapshot` 只用于解释消息进入系统时为什么被接受，不能替代当前授权。任何写操作、自动修复或外部通知执行前，仍要根据最新权限、资源状态和审批状态重新校验。

## 8. Stale Run Watchdog

Open SWE 的 `reconcile_stale_runs()` 会扫描 busy thread 上长期 pending 的 Run 并取消：

[reconcile.py:39](../../agent/reconcile.py:39)

平台应将它做成独立定时任务：

```text
扫描 queued/pending/running
  -> 计算 created_at / last_progress_at
  -> 超过 Run deadline
  -> cancel 或 timed_out
  -> 写入终态和告警
```

要区分正常等待外部系统、provider 断开、worker 丢失和人工审批等待。

## 9. 推荐可靠性栈

```text
RuntimeContext 校验
-> PrepareRun 幂等准备
-> ModelCallLimit
-> ToolPolicy
-> ToolError
-> Inbox consume
-> ModelFallback
-> ModelCallTimeout
-> ProgressGuard
-> RunFinalizer
```

每个 Middleware 都应该输出机器可读事件，不要只写日志。

## 10. 验收测试

```text
模型调用超过 deadline -> Run timed_out
主模型 429 -> 只切换允许的 fallback
工具参数错误 -> ToolMessage
写工具网络超时 -> 不自动重复执行
运行中追加两条消息 -> FIFO 注入一次
Inbox 消费前 Worker 崩溃 -> 消息仍可重新 claim
pending Run 超时 -> Watchdog 释放 thread
模型返回空消息 -> 不出现静默 success
```

## 不要在阶段 1 做的事

- 不要给所有工具默认开启重试。
- 不要让前端看到网络断开就重新提交 start 命令。
- 不要把所有异常都转换成成功的 AIMessage。
- 不要用日志文本代替稳定错误码。
- 不要先做复杂分布式队列，先把单 Thread 的 claim/ack 语义测通。

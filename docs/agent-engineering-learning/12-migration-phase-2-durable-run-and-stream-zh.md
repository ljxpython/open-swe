# 迁移阶段 2：Durable Run、统一 Run API 与可恢复 SSE

## 本阶段目标

阶段 2 直接建设新的生产运行协议，不兼容旧服务、旧前端和旧的 `runs.stream` 入口。目标是让 Run 脱离浏览器连接独立存在，SSE 只承担观察和恢复，不承担状态事实。

```text
Run 是持久任务
SSE 是观察通道
Thread / Run 状态是事实源
生产前端是授权观察者
runtime-web 是内部直连调试工具，不属于生产协议
```

## 1. 生产端只保留一套接口

建议由 Platform API 或 Runtime Gateway 提供下面的生产接口。具体 URL 可以按已有网关风格调整，但语义不要再出现第二套“启动并隐式流式返回”的 Run 状态机。

| 接口 | 作用 | 权限 |
| --- | --- | --- |
| `POST /threads/{thread_id}/runs` | 创建或按幂等键返回已有 Run，响应 `run_id` | thread 写权限 |
| `GET /threads/{thread_id}/runs/{run_id}` | 查询当前状态、终态和安全摘要 | thread 读权限 |
| `GET /threads/{thread_id}/runs/{run_id}/events` | SSE 订阅或从 `Last-Event-ID` 重放 | thread 读权限 |
| `POST /threads/{thread_id}/runs/{run_id}/cancel` | 取消运行 | thread 写权限 |
| `POST /threads/{thread_id}/runs/{run_id}/resume` | 处理审批或 interrupt 后继续 | thread 写权限和审批校验 |

创建请求只接受受控字段：

```json
{
  "assistant_id": "research_agent_v1",
  "input": {"messages": [{"role": "user", "content": "..." }]},
  "idempotency_key": "client-or-server-generated-key"
}
```

`thread_id`、`project_id`、身份、权限、模型策略和工具策略不能由浏览器自由决定。Platform API 认证后构建 `RuntimeContext` 和 `RuntimeOptions`，再交给 Runtime。

## 2. Run Coordinator 是唯一创建入口

Coordinator 负责运行生命周期，不负责 Agent Prompt、业务工具或具体 Graph 装配：

```text
验证 actor / project / assistant
  -> 创建或读取 thread
  -> 校验 idempotency_key
  -> 冻结 RuntimeContext、RuntimeOptions 和版本快照
  -> 调用 LangGraph 创建 Run
  -> 设置 durability="sync"、stream_resumable=True
  -> 写 queued / running 状态
  -> 返回 run_id
```

内部仍可以使用 `langgraph_sdk` 的 `client.runs.create(...)`。关键是所有生产入口都必须先经过 Coordinator；不能让页面、Webhook 或 cron 各自拼 `RunnableConfig`、各自决定幂等、再各自维护状态。

参考 Open SWE 的统一派发语义：

[agent/dispatch.py:113](../../agent/dispatch.py:113)

它值得借鉴 `durability="sync"`、`stream_resumable=True`、完成 webhook 和统一 metadata；不需要保留它为兼容旧调用方设计的包装方式。

## 3. 前端的正确调用顺序

```text
1. POST /threads/{thread_id}/runs
   -> 获取 run_id

2. GET /threads/{thread_id}/runs/{run_id}/events
   -> 消费 SSE 事件并保存最后 event id

3. 断线、刷新或切换页面
   -> 先 GET Run 状态
   -> 带 Last-Event-ID 重新订阅 events
   -> 不重新 POST 创建 Run

4. 用户停止或审批
   -> 调用 cancel / resume
```

这样 Run 是否继续由服务端状态和策略决定，而不是由浏览器连接是否存在决定。

## 4. SSE 传输与恢复

生产 SSE 建议透明代理 LangGraph 的事件 bytes：

```text
LangGraph Runtime
  -> httpx.stream()
  -> StreamingResponse
  -> Browser EventSource / SDK
```

代理层只做认证、权限、事件字段脱敏、连接管理和必要的观测字段补充，不重新定义业务事件协议。若上游支持恢复，前端以 `Last-Event-ID` 重连；若上游无法回放，前端仍先查询 Run 终态和最新 checkpoint，绝不依据“缺少最后一帧”重新创建 Run。

SSE 的状态规则：

| 现象 | 处理 |
| --- | --- |
| 浏览器断线 | Run 默认继续；客户端重新订阅 |
| 标签页刷新 | 查询 Run 后从最后事件恢复 |
| 用户主动停止 | 取消或 interrupt，并写入终态 |
| Worker 重启 | 从 checkpoint 恢复，或写入确定失败 |
| SSE 代理断开 | 返回连接错误；不能修改 Run 成功/失败 |
| completion webhook 重复 | 幂等投递；不能覆盖已持久化终态 |

## 5. 状态、checkpoint 与最终通知

状态写入顺序必须固定：

```text
业务/图状态完成
  -> 同步 checkpoint
  -> 写入 Run 终态
  -> 发出 completed / failed SSE 事件
  -> 发送 completion webhook
```

completion webhook 失败可以重试通知，但不能让成功的 Run 变成失败。SSE 最后一帧也不能被当成事务提交证明，Run 查询才是事实源。

## 6. runtime-web 的边界

`runtime-web` 是内部调试工具，可以直连 LangGraph、绕过 Platform API 权限，用于查看原始事件、验证 Graph、checkpoint 和 interrupt。它不发布到线上，因此：

- 不作为生产接口的兼容对象；
- 不作为生产鉴权和租户隔离的依据；
- 不要求和生产前端事件格式一致；
- 可以保留适合调试的 `AbortSignal` 或 disconnect-cancel 行为。

生产前端只能通过 Platform API / Runtime Gateway，得到服务端签发的 `RuntimeContext`。

## 7. 实施顺序

1. 定义 Run 状态、错误码、请求/响应 schema 和幂等键规则。
2. 实现 Coordinator，并让所有生产创建入口调用它。
3. 实现 Run 查询、取消、恢复和 SSE 订阅接口。
4. 开启 `durability="sync"`、`stream_resumable=True`、checkpoint 和 completion webhook。
5. 将生产前端直接接到新接口；不做旧页面适配。
6. 用断线、重试、Worker 重启、取消和审批场景验收。

## 8. 验收场景

```text
同一 idempotency_key 重试 -> 只创建一个 Run
创建 Run 后刷新页面 -> 查询状态并恢复事件，不重复执行
SSE 断线 -> Run 继续或按明确策略结束
Last-Event-ID 重连 -> 能回放未消费事件
取消 -> Run 进入 cancelled / interrupted
审批恢复 -> 只恢复目标 Run，且审批与参数匹配
Worker 在 checkpoint 后重启 -> 恢复或确定失败
completion webhook 重复 -> 外部通知和终态保持幂等
runtime-web 直连 -> 不影响生产接口权限模型
```

## 不要在阶段 2 做的事

- 不要保留或新增旧 `runs.stream` 兼容层。
- 不要让每个生产页面自己决定 Run 状态。
- 不要用重发创建请求代替 SSE 断线恢复。
- 不要把 SSE 最后一帧当作 Run 成功的唯一证据。
- 不要让 `runtime-web` 的直连调试方式进入线上发布路径。

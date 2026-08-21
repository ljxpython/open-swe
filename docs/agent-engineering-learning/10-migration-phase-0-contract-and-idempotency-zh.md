# 迁移阶段 0：运行契约、身份与幂等准备

## 本阶段目标

先把一次 Agent 运行的输入、身份和生命周期定义清楚，再迁移复杂 Middleware。阶段 0 不增加 Agent 功能，目标是证明：

```text
一个请求只能代表一个可信身份
一个业务对象能稳定找到同一个 thread
一次运行的有效配置可以被审计
恢复或重试不会重复执行准备动作
```

Open SWE 的可靠性能力建立在这些前提上。如果 thread、身份和运行配置不稳定，checkpoint、SSE 或 Sandbox 只会把错误保存得更久。

## 1. 先划分三类输入

`ai-agent-platform` 已经有 `RuntimeContext` 和 `RuntimeRequestMiddleware`，这部分应该保留，但要严格区分：

| 类别 | 内容 | 来源 | 是否允许客户端直接覆盖 |
| --- | --- | --- | --- |
| 可信身份 | user、tenant、role、permissions、project | Platform API 鉴权和 delegation token | 否 |
| 运行选项 | model、prompt、tool names、token limit、effort | 受控请求字段和项目默认值 | 只能在白名单内 |
| 运行资源 | thread、checkpoint、workspace、trace | Runtime 和资源管理器 | 否 |

`configurable` 是比 `RuntimeContext` 更通用的 LangGraph/LangChain 运行配置通道，适合 `thread_id`、checkpoint 路由键和已校验的运行选项。它的类型宽松不等于它是安全边界：身份、项目和权限仍必须由 Platform API 签发为 `RuntimeContext`；若调用链只能经由 `configurable` 传输，Runtime 也必须在入口校验并重建类型化 Context，不能信任浏览器原样提交的字典。

建议的请求形状：

```python
class RuntimeRequest:
    assistant_id: str
    thread_id: str | None
    input: dict[str, object] | None
    runtime_options: RuntimeOptions
    context: RuntimeContext
    idempotency_key: str | None
```

`context` 由平台签发，`runtime_options` 由服务端校验后生成，Graph 不直接信任原始 HTTP body。

## 2. 稳定的业务 Thread ID

Open SWE 对外部事件使用确定性 thread ID。例如 Slack 线程通过 channel 和 timestamp 生成稳定 ID：

[thread_ids.py:5](../../agent/utils/thread_ids.py:5)

```python
composite = f"{channel_id}:{thread_ts}"
md5_hex = hashlib.md5(composite.encode("utf-8")).hexdigest()
thread_id = str(uuid.UUID(hex=md5_hex))
```

`ai-agent-platform` 可以定义自己的业务键：

```text
project_id + assistant_id + object_type + object_id
```

例如：

```text
project-a:testcase-agent:requirement:REQ-1001
project-a:research-agent:ticket:INC-2002
```

需要防止：

1. 不能只用用户 ID，否则一个用户的所有任务会混在同一个 thread。
2. 不能只用前端随机 ID，否则 Webhook 重试会创建多个 thread。
3. 业务对象类型必须参与哈希，否则不同系统的相同编号会冲突。
4. thread key 算法需要版本号；规则变化不能让旧对象悄悄指向新 thread。

建议保留两层字段：

```text
thread_id       = LangGraph Runtime 的实际 ID
business_key    = 可查询、可审计的稳定业务索引
```

## 3. 保存有效配置快照

Open SWE 创建 Dashboard thread 时会解析团队默认模型、用户 Profile 和本次请求覆盖，并把解析结果写入 metadata：

[thread_api.py:1067](../../agent/dashboard/thread_api.py:1067)

其中 `resolved_model` 和 `resolved_effort` 记录真正生效的配置，而不是只保存用户提交的值。

`ai-agent-platform` 每次创建 Run 至少应保存：

```text
assistant_id
assistant_version
model_id
reasoning_effort
system_prompt_version
tool_profile_version
runtime_options_hash
project_id
actor_id
source
```

原因很简单：

```text
今天运行使用 gpt-4.1
明天管理员修改默认模型
历史 Run 仍然必须能解释当时为什么得到那个结果
```

快照分三层：

| 快照 | 写入时机 | 用途 |
| --- | --- | --- |
| Thread metadata | 创建或更新对话时 | UI 展示和检索 |
| Run metadata | Run 创建时 | 精确审计和重放 |
| Trace tags | 每次模型/工具调用 | 性能和错误分析 |

不要把完整 Prompt、Token 或密钥写入公开 metadata；保存版本号、摘要和必要的安全字段即可。

## 4. 让 PrepareRun 可恢复且幂等

Open SWE 的 `BasePrepareRunMiddleware` 使用 fingerprint 和记忆状态避免重复准备：

[prepare_run.py:36](../../agent/middleware/prepare_run.py:36)

关键语义：

```text
同一个 Run 恢复 -> 跳过已经成功的准备步骤
同一个 Thread 的新 Run -> 重新准备本次请求需要的资源
准备步骤在 checkpoint 前失败 -> 允许重试，但操作必须幂等
```

准备步骤可能包括：

- 读取项目策略和 Agent Profile；
- 解析模型和工具目录；
- 分配临时文件目录；
- 获取知识库或 MCP 会话；
- 创建审计上下文；
- 为 Coding Agent 连接 Workspace。

每个准备动作都要声明：

```text
是否有外部副作用
幂等键是什么
成功结果写在哪里
失败后是否可重试
恢复时如何判断已完成
```

建议使用：

```text
prepare_status = pending | ready | failed
prepare_fingerprint = hash(context + options + input)
```

不要在 `before_agent` 中无条件创建资源。

## 5. 统一 Run 状态和错误码

阶段 0 就应该定义平台状态：

```text
accepted
queued
running
waiting_approval
succeeded
failed
timed_out
cancelled
interrupted
```

同时定义稳定错误码：

```text
runtime_context_invalid
runtime_option_forbidden
assistant_not_allowed
run_conflict
run_deadline_exceeded
tool_permission_denied
upstream_unavailable
```

UI 可以把 `pending/running` 映射成“运行中”，但数据库和审计记录必须保留精确终态。

## 6. 实施顺序

1. 固化 `RuntimeContext` 的签发来源。
2. 定义 `RuntimeOptions` 白名单。
3. 增加业务 key 到 thread ID 的确定性映射。
4. 在 Run 创建时写入有效配置快照。
5. 为 PrepareRun 增加 fingerprint 和恢复测试。
6. 定义 Run 状态和错误码常量。

## 7. 最小验收测试

```text
同一个业务 key 重试两次 -> 得到同一个 thread
不同 project 或 assistant -> 不发生 thread 冲突
客户端伪造 user/tenant/project -> 在 Graph 前被拒绝
默认模型改变后查询旧 Run -> 仍显示原模型
PrepareRun 恢复 -> 不重复执行外部写操作
同一个 idempotency_key -> 不创建两个 Run
```

## 不要在阶段 0 做的事

- 不要复制 Open SWE 的 `get_agent()`；它绑定了 GitHub 和 Sandbox。
- 不要先接入完整 Coding Agent 工具集。
- 不要让客户端传任意 Python 工具、MCP URL 或身份字段。
- 不要用 prompt 代替 RuntimeContext 的权限校验。

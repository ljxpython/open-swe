# 02：middleware 列表与顺序逐项解读

当前列表位于 [agent/server.py:1192-1235](../../../agent/server.py:1192)。源码顺序如下：

```python
[
    PrepareAgentRunMiddleware(...),
    *([dynamic_tool_middleware] if dynamic_tool_middleware else []),
    SanitizeToolInputsMiddleware(),
    ModelCallLimitMiddleware(...),
    ToolErrorMiddleware(),
    SubdirAgentsReadMiddleware(),
    ToolRetryMiddleware(... tools=["task"] ...),
    PullRequestCreationGuardMiddleware(),
    refresh_github_proxy_before_model,
    check_message_queue_before_model,
    TimeoutWrapupMiddleware(),
    notify_step_limit_reached,
    *fallback_middleware,
    *plan_mode_middleware,
    SanitizeFireworksMessagesMiddleware(),
    SanitizeThinkingBlocksMiddleware(),
    ModelCallTimeoutMiddleware(),
]
```

先记住一个原则：这不是“随便排列的插件数组”。Middleware 会参与 before-agent、model-call、tool-call、after-agent 等生命周期钩子；同一个 middleware 放在不同位置，可能改变谁先看到输入、谁能捕获异常、谁能包住 provider 请求。

## 1. `PrepareAgentRunMiddleware`

源码位置：[agent/server.py:1195-1209](../../../agent/server.py:1195)。它把当前工厂上下文传入：

```python
PrepareAgentRunMiddleware(
    thread_id=thread_id,
    config=config,
    profile_login=profile_login,
    model_id=model_id,
    effort=profile_effort,
    source=source,
    user_email=user_email,
    linear_project_id=linear_project_id,
    linear_issue_number=linear_issue_number,
    create_prs=always_create_prs,
    draft_prs=draft_prs,
    plan_mode=plan_mode,
    corridor_enabled=bool(corridor_tools),
)
```

运行时它负责一次性准备本轮所需的状态，包括 sandbox/工作目录、token、turn checkpoint 和动态系统提示词。基类在 [prepare_run.py:54-67](../../../agent/middleware/prepare_run.py:54) 使用 fingerprint 避免同一恢复 Run 重复准备；在 [prepare_run.py:84-94](../../../agent/middleware/prepare_run.py:84) 把渲染出的 prompt 合并到模型请求。

它必须拿到 `thread_id` 和 `config`，否则无法知道本轮应该使用哪个线程资源。

## 2. 可选的 `DynamicToolMiddleware`

```python
*([dynamic_tool_middleware] if dynamic_tool_middleware else [])
```

没有可用集成工具时，展开为空，不产生占位 middleware。有工具时，它先暴露 `load_integration_tools`，模型明确加载后，下一次模型调用才附加真实工具 schema。

实现见 [agent/middleware/dynamic_tools.py:102-112](../../../agent/middleware/dynamic_tools.py:102)。它同时保护工具调用：未加载的集成工具会收到错误 `ToolMessage`，而不是直接执行。

## 3. `SanitizeToolInputsMiddleware`

位置：[agent/server.py:1211](../../../agent/server.py:1211)。它在业务工具真正收到参数前做统一清洗/规范化，减少模型产生的格式噪声对工具实现的影响。

它解决的是“输入形状”问题，不负责判断工具是否有权限，也不负责重试整个子 Agent。

## 4. `ModelCallLimitMiddleware`

```python
ModelCallLimitMiddleware(
    run_limit=MODEL_CALL_RECURSION_LIMIT,
    exit_behavior="end",
)
```

它限制一次 Run 的模型调用预算。`DEFAULT_RECURSION_LIMIT` 是图级上限，`MODEL_CALL_RECURSION_LIMIT` 是更聚焦于模型调用的上限；两者不是同一个计数器。

达到限制后使用 `exit_behavior="end"` 结束，而不是无限继续。项目另外通过 `notify_step_limit_reached` 给外部渠道发送可见通知。

## 5. `ToolErrorMiddleware`

位置：[agent/server.py:1213](../../../agent/server.py:1213)。工具抛出异常时，它把异常转换成模型可以理解的工具错误消息，避免一个普通工具异常直接炸掉整个图。

它不是“吞掉所有错误”：错误会回到 Agent 状态，模型仍然能看到失败原因并决定修正参数、换工具或结束。

## 6. `SubdirAgentsReadMiddleware`

它在读取文件结果时补充适用目录层级的 `AGENTS.md` 规则。这样模型先看到当前目录的局部约束，再决定是否编辑文件。

它是 Open SWE 的仓库规则传播层，不是 Deep Agents 默认的 `SkillsMiddleware`。二者区别：

| 机制 | 内容 | 触发方式 |
| --- | --- | --- |
| `SubdirAgentsReadMiddleware` | 仓库目录中的 `AGENTS.md` | `read_file` 结果处理 |
| `SkillsMiddleware` | Store 中的用户 Skill | Skills 元数据和 `read_file('/skills/...')` |

## 7. `ToolRetryMiddleware(tools=["task"])`

```python
ToolRetryMiddleware(
    max_retries=2,
    tools=["task"],
    retry_on=task_retry_on,
    on_failure=task_on_failure,
    initial_delay=1.0,
    max_delay=10.0,
)
```

它只重试 `task`，不重试所有业务工具。原因是子 Agent 的模型调用、sandbox 操作或远端 provider 可能出现暂态失败；盲目重试 `open_pull_request`、Linear 写操作等副作用工具，可能造成重复外部操作。

- `max_retries=2`：初次失败后最多再试两次。
- `retry_on=task_retry_on`：决定哪些异常值得重试。
- `on_failure=task_on_failure`：最终失败时生成统一的失败结果/通知。
- `initial_delay` 和 `max_delay`：控制退避时间。

## 8. `PullRequestCreationGuardMiddleware`

它只检查 `execute` 里的 shell command，阻止 `gh pr create`、`gh api .../pulls`、`curl POST .../pulls` 等新建 PR fallback 绕过专用 `open_pull_request` 工具的归属/审计流程；它不决定是否允许调用 `open_pull_request`，也不阻止已有 PR 的查看、编辑或评论。

这属于对特定副作用绕过路径的工具边界保护，不应只依赖 prompt 中一句“请不要创建 PR”。

## 9. `refresh_github_proxy_before_model`

这是模型调用前的函数式 middleware。它刷新 sandbox 使用的 GitHub proxy 凭据，降低长 Run 中 installation token 过期的概率。

它不负责创建 sandbox；sandbox 的创建/重连已经在 `PrepareAgentRunMiddleware` 和 backend 生命周期中完成。

## 10. `check_message_queue_before_model`

它在每次模型调用前读取线程队列，把用户在 Run 进行中追加的 Slack/Linear/Dashboard 消息注入当前状态。这样“运行中继续发消息”不会并行启动第二个主 Agent，而是进入下一轮模型上下文。

它必须位于模型调用链中，才能在下一次 LLM 请求前看到新消息。

## 11. `TimeoutWrapupMiddleware`

它处理 Run 接近或达到超时时的收尾逻辑，例如生成可见的超时说明、停止继续推进或执行项目定义的清理。它解决的是“整轮 Run 如何收尾”，与最后的 `ModelCallTimeoutMiddleware` 不同：后者只限制单次 provider 调用。

## 12. `notify_step_limit_reached`

当 Agent 因步骤/模型调用限制结束时，它向 Slack 等外部渠道发送明确通知。否则用户只会看到流突然停止，很难区分“正常完成”和“预算耗尽”。

## 13. `fallback_middleware`

这是一个可选展开：只有主模型和 fallback 模型都已配置且二者不同，列表才插入 `ModelFallbackMiddleware`。

它不是第二个 Agent，也不是并行调用；它只在符合条件的模型错误（例如超时、限流或 provider 暂态错误）冒泡时切换备用模型。

## 14. `plan_mode_middleware`

该列表通常包含 `PlanModeMiddleware`，根据 `plan_mode` 状态限制有副作用的工具。注意当前项目明确把 `task` 放进计划模式排除集合，因为子 Agent 是独立子图，不能假设它会自动继承父图的只读限制。

这不是一个纯 prompt 开关，而是工具可见性/调用边界的 middleware 约束。

## 15. 两个 provider 消息清洗 middleware

```python
SanitizeFireworksMessagesMiddleware(),
SanitizeThinkingBlocksMiddleware(),
```

它们在真正调用 provider 前修正特定消息格式：

- Fireworks 消息清洗：处理该 provider 不接受的消息形状。
- Thinking block 清洗：删除格式不合法或空的 Anthropic thinking block。

它们属于协议适配，不改变 Agent 的业务状态或工具权限。

## 16. 最内层 `ModelCallTimeoutMiddleware`

```python
# deadline covers the provider call itself
ModelCallTimeoutMiddleware(),
```

这是列表最后一项，源码注释已经说明意图：让 deadline 尽量包住真实 provider 请求本身。超时异常向外冒泡，外层的 fallback middleware 才能接住并尝试备用模型。

同时，通用子 Agent 在 `_general_purpose_subagent` 中另有一份自己的 timeout。原因是子 Agent 编译成独立图，父图的这份 middleware 不会自动包住子图内部的模型调用。

## 17. 为什么顺序不能随便换

### 例一：fallback 与 timeout

```text
provider 请求
  -> innermost ModelCallTimeoutMiddleware 超时
  -> 异常向外传播
  -> ModelFallbackMiddleware 判断是否切换备用模型
```

如果 timeout 放到 fallback 外面，fallback 可能看不到 provider 的超时异常。

### 例二：task retry 与错误处理

```text
task 调用失败
  -> ToolRetryMiddleware 判断是否重试
  -> 最终失败交给 ToolErrorMiddleware 形成可见 ToolMessage
```

如果所有错误先被粗暴转换，retry 可能失去判断异常类型的机会。

### 例三：队列消息与模型

```text
读取 pending_messages
  -> 注入 HumanMessage
  -> 发起下一次模型调用
```

队列检查放在模型调用边界之后，就会错过当前这次请求。

## 18. 主图与子图的 middleware 边界

父图这份列表不会完整复制给 `general-purpose` 子 Agent。Deep Agents 会给子 Agent 自动补上文件系统、摘要、Skills 等基础 middleware，而项目只显式传入：

```python
[
    dynamic_tool_middleware?,
    ModelCallTimeoutMiddleware(),
]
```

因此不要说“父图的 Plan Mode、消息队列、PR guard 会自动保护子 Agent”。当前代码正是因为这个边界，才把 `task` 纳入 `PLAN_MODE_EXCLUDED_TOOLS`。

## 本篇小结

middleware 列表是一条有顺序的运行保护链：先准备当前 Run，再处理工具和预算，接着治理副作用与交互消息，最后在 provider 边界做 fallback、协议清洗和单调用超时。顺序本身就是行为的一部分。

# 13：工具失败与副作用治理

这是 `09-middleware-learning-roadmap.md` 的第 3 章。工具调用不是一个“失败了就重试”的单点：Open SWE 分别处理参数格式、异常交付、子 Agent 暂态故障，以及不能绕过的 PR 创建路径，避免一个宽泛的 try/except 既掩盖错误又重复产生外部副作用。

源码入口：

- 装配顺序：[agent/server.py:1211-1223](../../../agent/server.py:1211)
- 参数清洗：[agent/middleware/sanitize_tool_inputs.py:23-84](../../../agent/middleware/sanitize_tool_inputs.py:23)
- 异常转换：[agent/middleware/tool_error_handler.py:147-180](../../../agent/middleware/tool_error_handler.py:147)
- `task` 重试策略：[agent/middleware/task_retry.py:5-86](../../../agent/middleware/task_retry.py:5)
- PR shell fallback 阻断：[agent/middleware/pr_creation_guard.py:231-288](../../../agent/middleware/pr_creation_guard.py:231)
- plan mode 工具过滤：[agent/middleware/plan_mode.py:40-81](../../../agent/middleware/plan_mode.py:40)

## 1. 一次工具调用经过哪些边界

主图中相关的 middleware 注册顺序如下：

```python
SanitizeToolInputsMiddleware(),
ToolErrorMiddleware(),
SubdirAgentsReadMiddleware(),
ToolRetryMiddleware(tools=["task"], ...),
PullRequestCreationGuardMiddleware(),
```

根据上一章的 wrapper 规则，靠前的是外层。省略不相关层后，调用和异常传播方向是：

```text
模型产生 ToolCall
  -> SanitizeToolInputs：修正 read_file 参数
  -> ToolError：最外层异常出口
  -> ToolRetry：仅 task 可在异常时重试
  -> PullRequestCreationGuard：可短路 execute
  -> 真实工具

真实工具抛异常
  -> ToolRetry 判断是否为 task 的暂态错误
  -> 不能重试或最终仍失败时，异常向外传播
  -> ToolError 生成 ToolMessage(status="error")
  -> 模型看见错误并决定下一步
```

这四层的输入、变化和出口不同：

| 机制 | 输入 | 变化 | 出口 |
| --- | --- | --- | --- |
| `SanitizeToolInputs` | `read_file` 的 args | 修正 `offset`、`limit` 的整数形状 | 放行 ToolCallRequest |
| `ToolRetry` | `task` 抛出的异常 | 选择退避重试或最终失败策略 | 重试、返回失败 ToolMessage，或重新抛出 |
| `ToolError` | 任意下游工具异常 | 转成标准 JSON 错误消息 | `ToolMessage(status="error")` |
| `PullRequestCreationGuard` | `execute(command=...)` | 检测 PR 创建 shell fallback | 直接返回阻断 ToolMessage，不调用 handler |

## 2. 参数清洗：只修复已知、低风险的格式噪声

模型有时会为 `read_file` 生成：

```python
{"offset": "1, 80", "limit": 80}
```

但文件工具要求 `offset` 是整数。`_coerce_int` 只接受整数，或从字符串的**开头**提取数字：

```text
"1, 80"              -> 1
"170, \"limit\": 60" -> 170
"  42, extra"        -> 42
"abc"                -> None，不猜测
```

`SanitizeToolInputsMiddleware` 只拦截工具名为 `read_file` 的调用，只处理 `offset` 和 `limit`，然后使用 `request.override(tool_call=...)` 把复制后的参数交给下游。它不修改原始 args 字典，也不会把无法解析的 `"abc"` 强行设成 `0`；后者应交给工具验证和错误处理。

这层解决的是“模型表达里有可确定的格式噪声”，不是“任何无效输入都替模型做决定”。

## 3. `ToolRetryMiddleware`：为什么只重试 `task`

项目装配为：

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

`max_retries=2` 表示“首次尝试之外最多再试两次”，即最多三次执行。LangChain 在每次失败后根据 `initial_delay * 2^attempt` 计算退避，并默认加入 jitter；本配置的 delay 上限是 10 秒。

### 3.1 哪些错误算暂态

`task_retry_on(exc)` 只返回以下类别：

- HTTP `408`、`409`、`425`、`429`、`500`、`502`、`503`、`504`、`529`，以及任何 `>= 500`；
- 已知连接/超时类名称，例如 `APIConnectionError`、`APITimeoutError`、`ReadTimeout`；
- `httpx.TransportError` 子类；
- `ModelCallTimeoutError`。

最后一项是子 Agent 专有的恢复路径：general-purpose 子 Agent 是独立图，没有父图的 fallback middleware；它自己的模型调用超时后，父 Agent 调用的 `task` 工具可以被再试一次。

`400`、普通业务异常、参数错误不会被盲目重试，因为重复调用没有改变其原因。

### 3.2 最终失败如何交给模型

重试耗尽时，LangChain 调用 `task_on_failure(exc)`：

- `invalid_prompt`、`context_length_exceeded`，或特定的 `400/422 invalid_request_error`，返回 JSON 字符串，让模型看见“子 Agent 失败”并调整任务；
- 其他异常重新抛出，交给外层 `ToolErrorMiddleware` 统一转为 `ToolMessage(status="error")`。

所以 `task_on_failure` 不是吞掉全部异常的兜底；它只把模型有可能修正的请求错误转换为可读结果。

### 3.3 为什么不能给所有工具加同样的重试

下面的重试可能产生重复副作用：

```text
open_pull_request  -> 可能创建重复 PR
linear_create_issue -> 可能创建重复 Issue
slack_thread_reply -> 可能发送重复消息
execute            -> 可能重复执行写文件、迁移、部署命令
```

`task` 也不是绝对无副作用，但它代表的是一次委派的子 Agent 执行，且项目只为可识别的暂态错误重试。把重试范围限制为 `tools=["task"]` 是风险控制，不是功能缺失。

## 4. `ToolErrorMiddleware`：让模型得到失败，而不是让图直接崩溃

它的主体很小：

```python
try:
    return await handler(request)
except Exception as exc:
    return ToolMessage(..., status="error")
```

普通异常会被标准化为 JSON：

```json
{
  "error": "...",
  "error_type": "ValueError",
  "status": "error",
  "name": "工具名"
}
```

这使错误以 ToolMessage 回到 messages，模型能改参数、改用别的工具或向用户说明阻塞，而不会因为一个工具异常直接中止整个图。

### 4.1 sandbox 异常是专门分支

捕获到 `SandboxClientError` 时，它还会：

1. 从 Runtime config 取得 `configurable.thread_id`；
2. 清掉进程内缓存的 sandbox backend；
3. 尝试发送 sandbox 不可达通知；
4. 返回包含 `recovery="sandbox_unreachable"` 的 ToolMessage。

它**不会**自动创建一个新 sandbox。新 sandbox 是空的，静默替换会让 Agent 以为原来的未提交工作仍在，实际却已经丢失。

### 4.2 为什么 `ToolError` 不能替代 `ToolRetry`

两者处理的时间点不同：

```text
Task 的下游异常
  -> ToolRetry 仍拿到 Exception，可按类型决定重试
  -> 最终未处理的 Exception 才到 ToolError
  -> ToolError 返回 ToolMessage，异常已经不存在
```

如果先把异常无差别转换成 ToolMessage，重试层只能看到一个正常返回值，无法判断它是否是 `429`、连接断开还是不可恢复的参数错误。当前注册顺序保证 `ToolRetry` 在 `ToolError` 的内侧，测试也断言了这一点。

## 5. `PullRequestCreationGuardMiddleware`：阻止 shell 绕过，不决定 PR 策略

这一层容易被误解。它不负责判断“本次 Run 是否允许调用 `open_pull_request`”，也不会拦截 `open_pull_request` 工具；它只检查 `execute` 工具里的 shell command，禁止模型在专用工具失败后绕过归属/审计流程。

会被阻断的例子：

```bash
gh pr create --draft
gh api repos/org/repo/pulls -X POST -f title=x
curl -X POST https://api.github.com/repos/org/repo/pulls -d '{}'
bash -c 'gh pr create --draft'
```

实现使用 `shlex` 拆分命令，并最多展开三层 `sh -c` / `bash -c` / `zsh -c`。达到展开深度上限仍有嵌套 shell 时也会保守阻断，避免解析不完整造成绕过。

命中后它**不调用 `handler`**，而是立即返回：

```json
{
  "status": "error",
  "code": "pr_creation_fallback_blocked",
  "recoverable_by_agent": false
}
```

这是真正的工具执行边界。与只写在 prompt 里的“请使用专用工具”相比，它不能被模型的下一句输出绕过。

## 6. plan mode：隐藏工具，不等于 sandbox 级权限控制

`PlanModeMiddleware` 是模型调用 wrapper。每一轮模型请求都会读取 `request.state["plan_mode"]`，当它为真时从本轮 `request.tools` 移除：

```text
task、http_request、open_pull_request、recreate_sandbox、request_pr_review、
用户 Skill 写/删、Slack 新线程、Linear 创建/更新/删除
```

关键点：

- `enter_plan_mode` 可以在 Run 中途通过 `Command` 把 state 改为 `True`，下一轮模型请求就会重新过滤工具；
- `task` 必须被排除，因为子 Agent 是独立图，不继承父图的 plan-mode 工具过滤；
- `execute`、`write_file`、`edit_file` 仍然可见，项目用 prompt 限制它们只服务于 `/workspace/plans/`，并没有把 plan mode 实现成 sandbox 强制只读权限。

因此应把它理解为“模型能力面收缩 + prompt 约束”，不是操作系统级的不可绕过权限边界。

## 7. 最小验证

运行配套脚本：

```bash
uv run python docs/open-swe-learning/17-create-deep-agent-call/13_tool_failure_and_side_effect_governance.py
```

预期输出：

```text
sanitized read_file offset: 170
retryable statuses: 503=True, 400=False
model-fixable task failure: invalid_prompt
blocked shell fallback: True
```

它只运行当前项目的纯函数，不会发起模型请求、工具调用、GitHub API 或 shell PR 创建。

针对实现的现有测试：

```bash
uv run pytest -q \
  tests/middleware/test_sanitize_tool_inputs.py \
  tests/agent/test_task_retry.py \
  tests/github/test_pr_creation_guard.py \
  tests/sandbox/test_sandbox_recovery.py
```

## 8. 常见误区

### 误区一：失败就让 `ToolErrorMiddleware` 处理

这会丢失重试时机。先区分“可恢复的暂态故障”和“应交给模型判断的错误”，最后才协议化异常。

### 误区二：`max_retries=2` 总共只会调用两次

不对。它是初次调用之后的最大重试次数，总共最多三次。

### 误区三：PR guard 禁止所有 PR 操作

不对。它只阻止 `execute` 中的新建 PR fallback；查看、评论、编辑已有 PR 以及专用 `open_pull_request` 工具不由它拦截。

### 误区四：plan mode 已经让仓库只读

不对。它会隐藏一批工具，但保留 `execute` 和文件编辑工具；真正的只读行为还依赖系统提示词约束。

## 9. 本章掌握标准

能回答下面五个问题，就可以进入第 4 章“模型可靠性与协议适配”：

1. `SanitizeToolInputsMiddleware` 为什么只处理确定的整数格式问题？
2. 为什么 `task` 的 `429` 可以重试，`open_pull_request` 不能套用同一策略？
3. `ToolRetry` 和 `ToolError` 为什么必须分层且保持当前相对顺序？
4. PR guard 阻断了哪个工具、哪些命令，以及它为何要短路 handler？
5. 为什么 plan mode 不是 sandbox 级权限隔离？

# `agent/middleware` 中文导读

## 先用一句话理解

Middleware 可以理解成 Agent 外面的一圈“保安、翻译和后勤”。它们不负责像模型一样思考任务，而是在模型调用、工具调用、Agent 开始或结束时插手：

```text
用户消息
   |
   v
准备运行环境、补充规则、检查权限
   |
   v
模型决定调用什么工具
   |
   v
清洗工具参数、拦截危险操作、执行工具
   |
   v
处理错误、重试、发送状态、保存收尾信息
```

## Middleware 怎么被使用

`server.py`、`reviewer.py`、`chat.py` 和 `analyzer.py` 创建 Deep Agent 时，会把 middleware 放进 `middleware=[...]` 列表。列表顺序很重要：外层 middleware 先进入，内层 middleware 更靠近模型调用。

主编码 Agent 的简化顺序是：

```text
PrepareAgentRunMiddleware
  -> DynamicToolMiddleware（有集成工具时）
  -> SanitizeToolInputsMiddleware
  -> ModelCallLimitMiddleware
  -> ToolErrorMiddleware
  -> SubdirAgentsReadMiddleware
  -> ToolRetryMiddleware（task 子 Agent）
  -> PullRequestCreationGuardMiddleware
  -> refresh_github_proxy_before_model
  -> check_message_queue_before_model
  -> SlackAssistantStatusMiddleware
  -> TimeoutWrapupMiddleware
  -> notify_step_limit_reached
  -> ModelFallbackMiddleware（需要时）
  -> PlanModeMiddleware
  -> provider 消息清洗
  -> ModelCallTimeoutMiddleware
  -> 模型
```

评审、PR Chat 和 Analyzer 会使用更精简的列表，因为它们的权限和目标不同。例如 PR Chat 是只读 Agent，不需要主编码 Agent 的 GitHub 推送保护和计划模式。

## 按执行时机分类

| 时机 | 典型 middleware | 通俗理解 |
| --- | --- | --- |
| Agent 开始前 | `prepare_run.py`、`dynamic_tools.py`、`plan_mode.py` | 把沙箱、提示词、工具和运行状态准备好 |
| 每次模型调用前 | `check_message_queue.py`、`refresh_github_proxy.py`、`timeout_wrapup.py` | 处理新消息、刷新凭据、提醒模型快到时间了 |
| 模型调用过程中 | `model_fallback.py`、`model_call_timeout.py`、消息清洗类 | 模型卡住或请求格式不对时兜底 |
| 工具调用前后 | `sanitize_tool_inputs.py`、`tool_error_handler.py`、`pr_creation_guard.py` | 修正参数、把错误变成消息、拦截危险命令 |
| Agent 结束后 | `notify_step_limit.py`、`settle_review_check.py`、Slack 状态 middleware | 告诉用户结果并清理外部状态 |

## 文件逐个说明

| 文件 | 通俗作用 | 主要使用情况 |
| --- | --- | --- |
| `__init__.py` | 统一导出 middleware，并且采用延迟导入。只有真正访问某个 middleware 时才加载对应文件，避免启动时一次性导入全部依赖。 | `server.py`、`reviewer.py`、`chat.py`、`analyzer.py` 从这里集中导入。 |
| `prepare_run.py` | 定义运行准备的公共基类。负责确保一次运行只准备一次，并把工作目录、渲染后的 system prompt 等结果写进 Agent 状态。 | `server.py` 的 `PrepareAgentRunMiddleware`、`reviewer.py` 的 `PrepareReviewerRunMiddleware`、`chat.py` 的 `PrepareChatRunMiddleware`、`analyzer.py` 的 `PrepareAnalyzerRunMiddleware` 都继承它。 |
| `check_message_queue.py` | 每次模型调用前检查线程队列，把用户在 Agent 工作期间追加的消息、Linear/Slack 消息或自动修复事件插入当前对话。 | 主 Agent 和 Reviewer 默认使用；这就是“Agent 忙着工作时还能继续发消息”的实现之一。 |
| `dynamic_tools.py` | 不把所有第三方工具一开始都塞给模型，而是先提供一个“加载集成工具”的入口，模型明确需要时再加载对应工具。 | 主 Agent 在存在 Currents、Notion、Datadog、LangSmith 等集成时使用。 |
| `ensure_no_empty_msg.py` | 防止模型这一轮什么都不做就退出。如果没有回复用户，也没有调用完成确认工具，就补一个 `no_op` 或完成确认调用，让流程继续。 | 作为主 Agent 的 after-model hook 使用，防止任务半路静默结束。 |
| `exclude_tools.py` | 从发给模型的工具列表中删除指定工具。 | PR Chat 使用它隐藏 `execute`、`write_file`、`edit_file`、`delete`，确保只读。 |
| `model_call_timeout.py` | 给一次模型请求设置硬性墙钟超时。模型连接卡死时强制抛错，不让整个线程永远挂住。默认由 `OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS` 控制。 | 主 Agent、Reviewer、PR Chat、Analyzer 都使用；它通常是最靠近模型的一层。 |
| `model_fallback.py` | 主模型遇到 429、5xx、连接错误或超时，就按退避时间切换到备用模型重试。全部失败时返回可见的故障消息或抛出最后错误。 | 主 Agent 在配置了 fallback 模型时使用。 |
| `notify_step_limit.py` | Agent 达到模型调用/步骤上限后，给 Slack 线程发一条提示，避免用户只看到 Agent 突然停了。 | 主 Agent 默认使用；它是 after-agent hook。 |
| `plan_mode.py` | 计划模式开启时，从模型可见工具中隐藏会改外部系统的工具，例如提交、开 PR 或启动子 Agent；每轮模型调用都会重新过滤。 | 主 Agent 使用，支持启动时进入计划模式，也支持运行中调用 `enter_plan_mode` 后生效。 |
| `pr_creation_guard.py` | 阻止模型绕过 `open_pull_request`，用 `gh pr create`、`gh api /pulls` 或 curl 直接开 PR，保证 PR 能正确归因和走统一流程。 | 已实现并从 `agent/middleware` 导出；当前默认 Agent middleware 列表中没有装入它，测试直接覆盖其行为。 |
| `refresh_github_proxy.py` | 每次模型调用前检查 LangSmith 沙箱里的 GitHub 代理 token 是否快过期，必要时刷新。 | 主 Agent 和 Reviewer 使用，避免长任务运行一小时后 `gh`/`git` 全部 401。 |
| `refresh_slack_status.py` | Agent 工作期间持续刷新 Slack 的“正在处理”状态，模型和工具运行完后清掉状态。 | 主 Agent 和 Reviewer 使用；避免 Slack 状态两分钟过期后看起来像 Agent 死了。 |
| `repair_orphaned_tool_calls.py` | 运行在工具调用中被取消或沙箱消失时，历史里可能只剩 AI 的 tool call，没有对应 ToolMessage。它补一条合成的工具错误消息，让下一次运行能继续。 | Reviewer 使用；重点修复 Anthropic/OpenAI 因工具结果缺失而拒绝整段历史的问题。 |
| `sandbox_circuit_breaker.py` | 统计连续的沙箱不可达错误，达到阈值后停止继续重试，并向用户说明沙箱坏了。 | 当前默认 `server.py`/`reviewer.py`/`chat.py`/`analyzer.py` middleware 列表没有直接装入这个类；它提供独立实现和通知函数，并有专门测试。不要把它和 `tool_error_handler.py` 的已启用处理混为一谈。 |
| `sanitize_fireworks_messages.py` | 调用 Fireworks 模型前删除旧格式的 `function_call` 字段，避免 Gateway 严格校验失败。 | 主 Agent、Reviewer、PR Chat 使用；只对 Fireworks 模型生效。 |
| `sanitize_thinking_blocks.py` | 调用 Anthropic 模型前删除空的 thinking block，避免 provider 因 malformed thinking 内容拒绝请求。 | 主 Agent、Reviewer、PR Chat 使用；只对 Anthropic 模型生效。 |
| `sanitize_tool_inputs.py` | 修正模型生成的工具参数，当前重点是把 `read_file` 的 `offset`、`limit` 从类似 `"1, 80"` 的字符串提取成整数。 | 主 Agent、Reviewer、Analyzer、PR Chat 使用，减少无意义的 Pydantic 校验失败和重试。 |
| `settle_review_check.py` | Reviewer 没有成功调用 `publish_review` 就结束时，把 GitHub 上一直显示“进行中”的 Review Check 收尾为 neutral；如果已有待确认结果则尽量恢复真实结论。 | Reviewer 使用；是 after-agent hook。 |
| `subdir_agents.py` | Agent 读取文件后，自动查找路径上层适用的 `AGENTS.md`，把相应规则追加到读取结果。 | 主 Agent 使用，保证模型在编辑子目录文件前能看到作用域内的项目规则。 |
| `task_retry.py` | 给 `task` 子 Agent 判断哪些错误值得重试，例如网络、超时、429、5xx；如果不值得重试，则把错误整理成模型能理解的失败说明。 | 由主 Agent 的 `ToolRetryMiddleware` 使用，专门保护子 Agent 委派调用。 |
| `timeout_wrapup.py` | 运行时间接近上限时，在下一次模型请求的 system message 里加“尽快收尾”的提醒，让 Agent 保存已有成果，不再开启大调查。 | 主 Agent、Reviewer、Analyzer 使用；它是软提醒，不是强制取消。 |
| `tool_error_handler.py` | 捕获工具异常，把异常变成 ToolMessage 返回给模型，而不是直接让整次 Agent run 崩掉；沙箱不可达时还会附加专门信息。 | 主 Agent、Reviewer、Analyzer、PR Chat 使用。 |
| `workflow_push_guard.py` | 检查 `git push` 是否会把 `.github/workflows/` 工作流文件推到远端；如果会，就生成变更摘要并要求人工审批。 | 已实现并有测试，但当前默认 Agent middleware 列表没有装入它；工作流审批主流程由 `workflow_approval.py`/API 配合。 |

## 主 Agent 中几个关键保护的配合

### 用户追问

```text
前端/Slack 追加消息
       ↓
线程消息队列
       ↓
check_message_queue_before_model
       ↓
下一次模型调用看到新消息
```

### 模型或网络故障

```text
模型请求卡死
   ↓
ModelCallTimeoutMiddleware 抛超时
   ↓
ModelFallbackMiddleware 尝试备用模型
   ↓
仍失败：返回可见错误，run-complete 再负责通知
```

### 工具参数写错

```text
模型生成 read_file(offset="1, 80")
   ↓
SanitizeToolInputsMiddleware
   ↓
offset=1
   ↓
工具正常执行
```

### 计划模式

```text
plan_mode=True
   ↓
PlanModeMiddleware 每轮过滤工具
   ↓
隐藏修改外部系统的工具
   ↓
Agent 只能研究和制定计划
```

### 代码规则加载

```text
模型调用 read_file
   ↓
SubdirAgentsReadMiddleware
   ↓
查找当前文件路径上层的 AGENTS.md
   ↓
把适用规则附在读取结果后
```

## 默认是否启用：不要只看文件名猜

目前可以按下面理解：

| 状态 | Middleware |
| --- | --- |
| 主 Agent 默认启用 | `prepare_run`、`dynamic_tools`（有集成时）、`sanitize_tool_inputs`、模型调用限制、`tool_error_handler`、`subdir_agents`、`task_retry`、`pr_creation_guard`、GitHub proxy 刷新、Slack 状态、`timeout_wrapup`、step-limit 通知、模型 fallback（配置时）、plan mode、Fireworks/Anthropic 清洗、模型超时、`ensure_no_empty_msg` 等 |
| Reviewer 默认启用 | `prepare_run`、工具输入清洗、模型调用限制、工具错误处理、GitHub proxy 刷新、消息队列、Slack 状态、超时收尾、模型消息清洗、孤儿工具修复、模型超时、Review Check 收尾 |
| PR Chat 默认启用 | `prepare_run`、工具输入清洗、模型调用限制、工具错误处理、工具排除、Fireworks/Anthropic 清洗、模型超时 |
| Analyzer 默认启用 | `prepare_run`、工具输入清洗、模型调用限制、工具错误处理、超时收尾 |
| 已实现但当前默认列表未装入 | `workflow_push_guard.py`、`sandbox_circuit_breaker.py` |

具体是否启用，以对应 Agent 工厂传给 `create_deep_agent(..., middleware=[...])` 的列表为准，而不是以文件是否存在为准。

## 一句话总结

`agent/middleware` 不是一堆独立业务功能，而是 Agent 的运行保护层：有的负责准备环境，有的负责控制工具权限，有的负责清洗模型请求，有的负责重试和超时，还有的负责把异常和运行结束状态告诉用户。它们让同一个 Agent 在长任务、网络波动、工具出错、用户追问和权限受限时仍然能稳定工作。

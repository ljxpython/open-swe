# 14：模型可靠性与协议适配

这是 `09-middleware-learning-roadmap.md` 的第 4 章。一个可持续运行的 Agent 不能只依赖“模型请求成功”：运行中会有新消息、sandbox GitHub token 会过期、provider 会限流或卡死，不同 provider 也可能拒绝历史消息的格式。本章把这些问题按发生位置拆开。

源码入口：

- before-model 钩子：[agent/middleware/refresh_github_proxy.py](../../../agent/middleware/refresh_github_proxy.py)、[agent/middleware/check_message_queue.py](../../../agent/middleware/check_message_queue.py)
- 总 Run 收尾提醒：[agent/middleware/timeout_wrapup.py](../../../agent/middleware/timeout_wrapup.py)
- provider fallback：[agent/middleware/model_fallback.py](../../../agent/middleware/model_fallback.py)
- 单次调用 deadline：[agent/middleware/model_call_timeout.py](../../../agent/middleware/model_call_timeout.py)
- provider 消息清洗：[agent/middleware/sanitize_fireworks_messages.py](../../../agent/middleware/sanitize_fireworks_messages.py)、[agent/middleware/sanitize_thinking_blocks.py](../../../agent/middleware/sanitize_thinking_blocks.py)

## 1. 先建立两条时间线

每一轮模型调用前，两个 `before_model` 钩子按注册顺序运行：

```text
refresh_github_proxy_before_model
  -> check_message_queue_before_model
  -> 开始模型 wrapper 链
```

随后模型 wrapper 的相关嵌套关系是：

```text
TimeoutWrapupMiddleware
  -> ModelFallbackMiddleware（只有配置了不同 fallback 时才存在）
    -> PlanModeMiddleware
      -> SanitizeFireworksMessagesMiddleware
        -> SanitizeThinkingBlocksMiddleware
          -> ModelCallTimeoutMiddleware
            -> 真实 provider 调用
```

因此两类“超时”完全不同：

| 机制 | 默认值 | 做什么 |
| --- | --- | --- |
| `TimeoutWrapupMiddleware` | 45 分钟 | 后续模型请求加“立即收尾”的指令 |
| `ModelCallTimeoutMiddleware` | 900 秒 | 用 `asyncio.wait_for` 取消卡死的**单次** provider 调用并抛异常 |

## 2. 每轮模型请求前：刷新 GitHub proxy 与接收新消息

### 2.1 `refresh_github_proxy_before_model`

LangSmith sandbox 中 GitHub proxy 使用 GitHub App installation token，通常约一小时过期。该钩子从运行时 config 取 `configurable.thread_id`，在每轮模型请求前调用 `maybe_refresh_proxy_token(thread_id)`。

它是 best effort：刷新失败只记录 warning，返回 `None`，不会阻止本次模型请求。因为它解决的是“长 Run 的未来 git/gh 工具调用仍可认证”，不是模型本身的前置条件。

### 2.2 `check_message_queue_before_model`

当一个 thread 已经在运行，新的 Dashboard、Slack 或 Linear 消息不能随意并发启动第二个主 Agent。入口会先把消息放到 LangGraph Store：

```text
namespace = ("queue", thread_id)
key       = "pending_messages"
```

这个 before-model 钩子在下一轮模型调用前读取并清空该键，将队列内容拼成一个新的 user message state update：

```python
{
    "messages": [{"role": "user", "content": content_blocks}],
    # Dashboard handoff 时可选：
    "plan_approval_blocked": True | False,
}
```

`content_blocks` 按队列 FIFO 顺序展开。它可以是文本、已提交的多模态 blocks，或由 image URL 下载后转换出的 image blocks。若当前 model 不支持图片，会保留文本并附加不支持图片的提示。

此外它还消费：

```text
("autofix", thread_id) / "pending_event"
```

把在运行中到达的 CI/review feedback 变为“结束前重新检查 PR”的用户指令。

### 2.3 为什么先删除队列项

读取 `pending_messages` 后，代码在构建 message blocks 前就调用 `store.adelete(...)`。设计目标是：该钩子在下一轮再次执行时不要重复注入同一条消息。

这不是通用消息队列的 exactly-once 事务协议。若删除成功后，进程在 state update 被 checkpoint 前失效，理论上可能丢失该批消息；当前实现优先避免重复指导 Agent。需要严格投递语义时，应该引入带 message id、ack 和 checkpoint 协调的专用队列，不能误以为这个 Store 键已提供它。

## 3. 接近 Run 时限：要求模型主动收尾

`TimeoutWrapupMiddleware` 第一次进入模型 wrapper 时才记录 `time.monotonic()`，而不是在 `get_agent()` 构图时开始计时。经过 `OPEN_SWE_WRAPUP_TIMEOUT_SECONDS`（默认 45 分钟）后，它不取消运行，而是在 system message 末尾附加：

```text
<time_limit_warning>
Wrap up immediately: finish the current step, save or report useful state...
</time_limit_warning>
```

这意味着它依赖模型配合，只影响**到达下一次模型调用**的 Run。若工具本身无限挂起，它无能为力；工具 deadline 和 Run 级调度需要由其他层负责。

它保留结构化 `SystemMessage.content`：列表内容会追加一个 text block，而不是粗暴转成字符串，避免破坏 provider 的缓存控制等 metadata。

## 4. fallback：暂态故障时交替尝试主模型与备用模型

`get_agent()` 只有在 `LLM_FALLBACK_MODEL_ID` 或模型默认 fallback 存在，且不等于主模型时，才安装 `ModelFallbackMiddleware`。

它认定为暂态的情况包括：

- provider 的连接、超时、限流、内部服务错误；
- `httpx.TransportError`；
- HTTP `408`、`409`、`425`、`429`、`500`、`502`、`503`、`504`、`529`；
- 最内层 timeout 转出来的 `ModelCallTimeoutError`（它是 `TimeoutError` 子类）。

默认 backoff schedule 是：

```python
(0.0, 5.0, 15.0, 30.0, 45.0)
```

它表示五次重试前的等待时间，因此总共最多六次尝试，顺序为：

```text
primary -> fallback -> primary -> fallback -> primary -> fallback
         立即       5 秒        15 秒       30 秒       45 秒（带最多 25% jitter）
```

第一次 failover 不等待，后续重试会逐渐越过 gateway 建议的短暂恢复窗口。若所有尝试都失败，默认返回用户可见的 `AIMessage`，说明这是 provider/gateway 暂态故障且可重新触发 Run；如果 `surface_outage_message=False` 才重新抛出最后一个异常。

以下错误不会无意义 fallback：

- 普通 `ValueError`、不符合暂态规则的 `400`；
- 已被识别的模型不可用/未配置错误，会直接返回“更换模型或更新 workspace 权限”的具体消息。

## 5. 单次模型调用 deadline：把“永远不返回”变成可恢复异常

provider 客户端通常有 HTTP timeout，但某些 streaming/websocket 卡死并不会触发 read timeout。`ModelCallTimeoutMiddleware` 是最内层 wrapper：

```python
await asyncio.wait_for(handler(request), timeout=self._timeout_seconds)
```

超过 `OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS`（默认 900 秒）时，它取消下游 awaitable，转换为 `ModelCallTimeoutError`。异常随后向外冒泡：

```text
provider hang
  -> ModelCallTimeoutError
  -> ModelFallbackMiddleware 识别为暂态错误
  -> 用备用/主模型的下一次尝试继续
```

它必须在 fallback 内侧。若把 timeout 放到 fallback 外侧，fallback 的 retry loop 收不到单次调用的 timeout 异常，整个 retry 链会一起被截断。

父图的 timeout 不覆盖子 Agent 内部的 provider 调用，因为子 Agent 是独立图；当前实现为 general-purpose、browser 和 reviewer 子 Agent 都单独配置了 `ModelCallTimeoutMiddleware`。子 Agent 没有该 fallback wrapper，因而其 timeout 还会成为上章 `task` 重试的暂态错误。

## 6. Provider 协议适配：只在匹配的 provider 前修复消息

### 6.1 Fireworks

`SanitizeFireworksMessagesMiddleware` 仅在当前 model（或其 `.bound` 链）是 `ChatFireworks` 时运行。它原地删除历史 `AIMessage.additional_kwargs["function_call"]` 的 legacy 字段。

现代 tool call 数据仍在 `tool_calls` 中。删除旧字段是因为 gateway 拒绝携带 `messages[N].function_call` 的请求；这不是删除 Agent 的工具调用能力。

### 6.2 Anthropic thinking blocks

`SanitizeThinkingBlocksMiddleware` 仅在 model 是 `ChatAnthropic` 时运行。对历史 `AIMessage.content` 列表，它移除：

```python
{"type": "thinking", "thinking": ""}
```

其他 content block 原样保留。这个清洗发生在发送 provider 前，防止空 thinking block 触发 Anthropic 格式校验失败。

两个 sanitizer 都限定 provider，避免让某个 provider 的兼容补丁意外改写另一个 provider 的 messages。

## 7. 放回一次完整调用

```text
第 N 次模型循环
  -> refresh GitHub proxy（失败只记录）
  -> 读取并清空本 thread 的 pending messages
  -> 有新内容则合并为 HumanMessage state update
  -> wrapup 超时？追加收尾指令
  -> primary 发起 provider 调用
     -> 过滤 plan-mode tools
     -> 清洗 Fireworks / Anthropic 格式
     -> 单次调用 deadline
  -> 暂态失败？fallback 交替重试
  -> 成功响应或最终用户可见 outage message
```

注意：队列内容是本轮模型调用**前**写进 state；如果这轮模型请求已经发出，用户的新消息仍要等到下一次模型循环才会被读取。若模型正常结束且没有下一轮调用，消息入口需要以新的 Run/队列策略再次唤醒，而不是让此 middleware “插队”进已经在飞行中的 HTTP 请求。

## 8. 最小验证

运行配套脚本：

```bash
uv run python docs/open-swe-learning/17-create-deep-agent-call/14_model_reliability_and_protocol_adaptation.py
```

预期输出：

```text
fallback attempts: primary -> fallback
model deadline converted: True
wrapup instruction added: True
```

它只使用本地 fake handler，不请求模型、LangGraph Server、sandbox 或外网。

现有测试覆盖各条边界：

```bash
uv run pytest -q \
  tests/middleware/test_check_message_queue.py \
  tests/middleware/test_model_fallback_middleware.py \
  tests/middleware/test_model_call_timeout.py \
  tests/middleware/test_sanitize_fireworks_messages.py \
  tests/middleware/test_sanitize_thinking_blocks.py \
  tests/agent/test_timeout_wrapup.py \
  tests/github/test_github_proxy_refresh.py
```

## 9. 常见误区

### 误区一：fallback 是一次性的“主失败就换备用”

不对。当前实现交替尝试主/备模型，并带多次退避，默认最多六次。

### 误区二：wrapup timeout 会终止 Run

不对。它只注入 prompt，真正取消单次卡死请求的是 `ModelCallTimeoutMiddleware`。

### 误区三：消息队列能实时打断当前模型请求

不对。它只在下一轮 `before_model` 读取；已经发出的 provider 请求不会被它修改。

### 误区四：消息 sanitizer 对所有模型都会改写 history

不对。Fireworks 与 Anthropic 的 sanitizer 都先检测 provider 类型。

## 10. 本章掌握标准

能回答下面五个问题，就完成本路线：

1. 为什么 queued message 只能在下一轮模型调用前进入 state？
2. 为什么 wrapup 与单次 model deadline 必须是两个 middleware？
3. 为什么 fallback 要在 model deadline 外层？
4. 主图的 fallback 为什么不会自动保护子 Agent 的模型调用？
5. 为什么 Fireworks/Anthropic 的 sanitizer 必须先识别 provider？

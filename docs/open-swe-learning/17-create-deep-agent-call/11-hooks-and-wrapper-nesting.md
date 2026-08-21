# 11：生命周期钩子与 wrapper 嵌套

这是 `09-middleware-learning-roadmap.md` 的第 1 章。本章只解决一个问题：多个 middleware 放进 `create_deep_agent(middleware=[...])` 后，调用顺序到底是什么。

不要把所有钩子当成同一种“前后拦截器”：`awrap_model_call` / `awrap_tool_call` 是嵌套 wrapper；`before_agent`、`before_model`、`after_agent` 是图中的独立节点。

## 1. 一次 Agent Run 的位置

```text
START
  -> before_agent（一次）
  -> before_model（每轮一次）
  -> awrap_model_call -> model -> awrap_model_call 返回
  -> awrap_tool_call -> tool -> awrap_tool_call 返回
  -> 回到 before_model
  -> 模型不再产生 tool call
  -> after_agent（一次）
  -> END
```

没有工具调用时，`awrap_tool_call` 不会执行；有两个工具调用时，它会分别执行两次。`before_agent` 和 `after_agent` 则分别只属于 Run 的开始和结束。

## 2. wrapper：第一个 middleware 是最外层

假设：

```python
middleware = [OuterMiddleware(), InnerMiddleware()]
```

LangChain 会把模型 wrapper 组合成：

```text
OuterMiddleware.awrap_model_call(
    request,
    handler=InnerMiddleware.awrap_model_call(
        request,
        handler=real_model,
    ),
)
```

所以执行顺序是：

```text
Outer.before
  -> Inner.before
    -> real_model
  <- Inner.after
<- Outer.after
```

这就是“列表第一个是 outermost”。Open SWE 的 `ModelCallTimeoutMiddleware` 放在列表末尾，目的是让它贴近真实 provider 调用；fallback 在它外层，才能捕获 timeout 异常。

工具 wrapper 完全一样：

```text
第一个工具 middleware before
  -> 第二个工具 middleware before
    -> real tool
  <- 第二个工具 middleware after
<- 第一个工具 middleware after
```

当前安装版本的实现证据：

- 模型 wrapper：`.venv/lib/python3.11/site-packages/langchain/agents/factory.py` 的 `_chain_async_model_call_handlers()`；
- 工具 wrapper：同一文件的 `_chain_async_tool_call_wrappers()`。

## 3. `handler` 到底是什么

在 wrapper 中，`handler` 不是“某个固定的模型函数名”，而是下游剩余链路：

| 当前 wrapper | `handler` 代表 |
| --- | --- |
| 最外层 middleware | 下一个 middleware wrapper |
| 中间层 middleware | 更内层 middleware wrapper |
| 最内层 middleware | 真实模型或真实工具执行器 |

因此三种行为分别是：

```python
# 正常放行
return await handler(request)

# 修改请求后放行
return await handler(request.override(tools=filtered_tools))

# 短路，不执行下游
return ToolMessage(content="blocked", tool_call_id=call_id, status="error")
```

如果忘了调用 `handler`，后面的 middleware、模型或工具都不会执行。这正是权限拦截和缓存短路能工作的原因，也正是误写 wrapper 时最容易造成“Agent 没反应”的原因。

## 4. `before_*` 不是 wrapper

`before_agent` 和 `before_model` 不接收 `handler`，因为它们不是包裹调用，而是图节点：

```python
async def abefore_model(self, state, runtime):
    return {"messages": [...]}  # 返回 state update
```

它们的顺序由图边连接决定。当前 LangChain 工厂会：

```text
middleware 列表中的 before_agent：按注册顺序执行
middleware 列表中的 before_model：按注册顺序执行
```

每个节点返回的字典会合并进 state；返回 `None` 表示不更新 state。它们不能像 `awrap_model_call` 那样直接拿到本次 `ModelRequest.tools`，也不能通过返回 `ModelResponse` 来替换模型结果。

## 5. `after_agent` 的顺序是反过来的

这是最容易忽略的区别。多个 `after_agent` 节点在结束时按注册顺序的反方向执行：

```python
middleware = [First(), Second(), Third()]
```

结束顺序是：

```text
Third.after_agent
  -> Second.after_agent
    -> First.after_agent
      -> END
```

原因是结束处理需要先收拢内层/后注册的收尾逻辑，再回到外层/先注册的收尾逻辑。当前工厂源码在构图时从 `middleware_w_after_agent[-1]` 反向连接到第一个节点。

不要把 `after_agent` 的顺序套用到 `before_agent`；两者方向相反。

## 6. 最小验证：不调用真实模型

运行配套脚本：

```bash
uv run python docs/open-swe-learning/17-create-deep-agent-call/11_hooks_and_wrapper_nesting.py
```

预期输出：

```text
outer:before -> inner:before -> model -> inner:after -> outer:after
```

这个验证手工模拟了 LangChain 的 wrapper 组合，不需要 DeepSeek、OpenAI、sandbox 或 LangGraph Store。它只验证“第一个是外层、返回时反向展开”这一条规则。

## 7. 放回 Open SWE 的列表

把本章规则映射回 `agent/server.py:get_agent()`：

```text
PrepareAgentRunMiddleware      -> before_agent：先准备 Run
check_message_queue_before_model -> before_model：每轮注入队列消息
ModelFallbackMiddleware        -> 外层模型 wrapper
SanitizeThinkingBlocksMiddleware -> provider 前消息清洗
ModelCallTimeoutMiddleware     -> 内层模型 wrapper：包住真实调用
ToolRetryMiddleware            -> task 工具 wrapper
ToolErrorMiddleware            -> 工具异常 wrapper
notify_step_limit_reached      -> after_agent：最后通知用户
```

注意：列表中靠前并不意味着所有钩子都绝对先执行。准确规则是：

| 钩子类型 | 顺序规则 |
| --- | --- |
| `before_agent` | 注册顺序 |
| `before_model` | 注册顺序，每轮重复 |
| `awrap_model_call` | 注册顺序进入，逆序返回 |
| `awrap_tool_call` | 注册顺序进入，逆序返回 |
| `after_agent` | 注册顺序逆序执行 |

## 8. 本章掌握标准

能回答下面五个问题，就可以进入路线的第 2 章：

1. 为什么列表第一个模型 wrapper 是最外层？
2. `handler` 不调用会发生什么？
3. 为什么 `before_model` 没有 `handler` 参数？
4. 为什么两个 tool call 会触发两次 `awrap_tool_call`？
5. 为什么 `after_agent` 和 `before_agent` 的多个 middleware 顺序相反？

下一章学习 [PrepareAgentRunMiddleware](../../../agent/middleware/prepare_run.py)：把“before_agent 只运行一次”进一步拆成 fingerprint、checkpoint、幂等准备和动态 system prompt。

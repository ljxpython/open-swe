# 第 15 章：Open SWE 如何装配 `create_deep_agent`

## 学习目标

本章只看当前项目的使用方式，不展开 Deep Agents 内部实现。读完后，你应该能沿着 `get_agent(config)` 回答：

- 用户和团队配置怎样变成主模型与 subagent 模型？
- Sandbox backend、静态工具、动态工具和 skills 怎样进入 Deep Agent？
- 为什么 Open SWE 把 `system_prompt` 传成空字符串？
- Middleware 为什么必须按当前顺序传入？

## 1. 调用链

入口不是浏览器直接调用 `get_agent`，而是 LangGraph Runtime 加载注册的图：

```text
langgraph.json
  -> agent.server:traced_agent
  -> traced_graph_factory(get_agent, ...)
  -> await get_agent(config)
  -> create_deep_agent(...)
  -> 返回编译后的 Pregel/CompiledStateGraph
```

`agent/server.py:32` 导入第三方工厂：

```python
from deepagents import create_deep_agent
```

真正的生产装配在 `agent/server.py:953-1237` 的 `get_agent`。

## 2. 第一条分支：是否真的在执行

```python
configurable = config.get("configurable") or {}
thread_id = configurable.get("thread_id")

if thread_id is None or not graph_loaded_for_execution(config):
    return create_deep_agent(
        system_prompt="",
        tools=[],
    ).with_config(config)
```

这条分支返回的是轻量空图，供图加载、检查或元数据探测使用。它没有线程 Sandbox，也没有 Open SWE 业务工具。

所以不能看到 `create_deep_agent` 被调用，就断言 Agent 已经可以修改仓库。只有有 `thread_id` 且配置标记为执行阶段，才会进入完整装配。

## 3. 第二步：解析可信配置

执行分支先并行读取：

```python
team_defaults, use_gateway, profile, fable_enabled = await asyncio.gather(
    _cached_team_default_model_pair("agent"),
    _cached_gateway_enabled(),
    _cached_profile(profile_login),
    _cached_fable_enabled(),
)
```

模型选择优先级是：

```text
thread configurable override
    > 用户 Profile override
    > 团队默认模型
```

随后还要校验模型 ID、effort 能力，并根据 Gateway/Fable 开关做最终修正。`create_deep_agent` 不负责这些业务策略；它只接收已经决定好的 `BaseChatModel`。

## 4. 第三步：准备 Sandbox backend 和 skills

`get_agent` 通过 thread_id 取得线程对应的 backend：

```text
thread_id
  -> _get_cached_sandbox_backend(...)
  -> SandboxBackendProtocol
  -> CompositeBackend
  -> create_deep_agent(backend=agent_backend)
```

这个 backend 让内置工具可以操作正确的线程工作区。登录用户的 skills 还会通过只读 Store backend 暴露在 `/skills/` 路由下。

因此 `get_agent` 传入的不是：

```python
backend="/some/local/path"
```

而是已经封装了 Sandbox、权限、GitHub proxy 和重连逻辑的 backend 对象。

## 5. 第四步：组装三类工具

### 5.1 Deep Agents 内置工具

不放在 `static_tools` 中，由 `create_deep_agent` 根据 backend 自动加入：

```text
read_file / write_file / edit_file
ls / glob / grep / delete
execute / task
```

### 5.2 Open SWE 静态工具

在 `agent/server.py:1119-1146` 注册，例如：

```text
web_search、http_request、approve_plan
linear_*、slack_*、open_pull_request
schedule_thread_wakeup、report_platform_issue
```

这些是产品固定能力，每次完整 Agent 构图时都会传入。

### 5.3 动态集成工具

Corridor、Notion、Currents、LangSmith、Browser 等工具根据凭据、权限和环境动态加载。失败时通常降级为空列表，避免可选集成拖垮主 Agent。

`DynamicToolMiddleware` 还维护保留名称，防止外部工具覆盖 `execute`、`task` 或业务核心工具。

## 6. 第五步：构造主模型和 subagent 模型

Open SWE 允许主 Agent 和 subagent 使用不同的模型/effort：

```text
team defaults
  -> profile main/subagent overrides
  -> thread override for main model
  -> provider_model_kwargs
  -> make_model(main)
  -> make_model(subagent)
```

主模型传给 `model=main_model`；subagent 模型放进 `_general_purpose_subagent(subagent_model, ...)` 描述中。

这解释了一个常见误区：`create_deep_agent(model=main_model)` 的 `model` 只直接指定主 Agent 模型。子 Agent 是否继承或覆盖模型，由 `SubAgent` 规格决定。

## 7. 第六步：为什么 `system_prompt=""`？

主工厂的调用是：

```python
return create_deep_agent(
    model=main_model,
    system_prompt="",
    tools=static_tools,
    subagents=[...],
    skills=skill_sources,
    backend=agent_backend,
    middleware=[...],
).with_config(config)
```

空字符串不代表没有系统提示词。Open SWE 把仓库指令、用户指令、工作目录、计划模式、来源渠道和 Sandbox 状态放到 `PrepareAgentRunMiddleware` 中，在每次新 Run/新回合动态渲染。

如果在工厂创建时把提示词固定死：

- thread 切换时无法更新仓库上下文
- 用户指令变化后不能及时生效
- 恢复 Run 时容易重复或使用过期上下文

所以这里是“构图时准备能力，运行前准备本轮上下文”。

## 8. 第七步：Middleware 顺序

Open SWE 在 `agent/server.py:1191-1233` 传入的顺序包含：

```text
PrepareAgentRunMiddleware
DynamicToolMiddleware
SanitizeToolInputsMiddleware
ModelCallLimitMiddleware
ToolErrorMiddleware
SubdirAgentsReadMiddleware
ToolRetryMiddleware(task)
PullRequestCreationGuardMiddleware
消息队列与 Slack 状态 middleware
Fallback / Plan mode
SanitizeThinkingBlocksMiddleware
ModelCallTimeoutMiddleware
```

顺序表达的是嵌套关系：

- 工具错误要在 retry 后统一收敛
- fallback 要能收到最内层模型超时
- timeout 要尽量只包住 provider call
- 工具清洗要发生在工具真正执行前

因此不能把这些对象当成普通配置项随便排序。

## 9. 一个请求在 `get_agent` 中的快照

```text
config
  -> thread_id / profile_login
  -> team + profile + thread model
  -> Sandbox backend
  -> static tools + dynamic tools
  -> main model + subagent model
  -> skills route
  -> ordered middleware
  -> create_deep_agent(...)
  -> with_config(config)
```

这个过程仍然没有调用 LLM。真正的模型请求发生在之后的图执行阶段。

### 9.1 `.with_config(config)` 到底做什么？

`with_config` 是 LangChain `Runnable` 的配置绑定方法。它不会执行图，也不会启动模型，而是返回一个包装后的 `RunnableBinding`，把配置作为后续执行的默认配置：

```python
graph = create_deep_agent(
    model=main_model,
    tools=static_tools,
    backend=agent_backend,
    middleware=middleware,
)

bound_graph = graph.with_config(config)
return bound_graph
```

当前 `get_agent` 中的 `config` 有两个时刻的用途：

```text
构图前：get_agent 读取 config，决定 thread、模型、Sandbox、工具和 middleware
构图后：with_config(config)，让执行期的图和 middleware 继续拿到同一份配置
```

一个典型的 `RunnableConfig` 可能是：

```python
config = {
    "configurable": {
        "thread_id": "thread-123",
        "agent_model_id": "openai:gpt-5.5",
        "agent_effort": "medium",
    },
    "recursion_limit": 500,
    "metadata": {"source": "dashboard"},
    "tags": ["agent"],
}
```

这些字段大致负责：

| 字段 | 作用 |
| --- | --- |
| `configurable` | thread_id、模型选择等业务/运行配置 |
| `recursion_limit` | 限制图的递归或执行步数 |
| `metadata` | tracing、日志和运行标记 |
| `tags` | LangSmith 等观测系统的标签 |

绑定后执行时，配置会传给图的节点、middleware、模型和工具：

```python
await bound_graph.ainvoke(input_data)
async for chunk in bound_graph.astream(input_data):
    ...
```

也可以在单次调用时覆盖部分绑定配置：

```python
bound_graph = graph.with_config({
    "configurable": {"thread_id": "thread-a"},
    "recursion_limit": 100,
})

await bound_graph.ainvoke(
    input_data,
    config={"configurable": {"thread_id": "thread-b"}},
)
```

此时 `thread_id` 使用 `thread-b`，未覆盖的 `recursion_limit` 仍使用 `100`。因此可以这样区分：

```text
with_config(config)       = 给 Runnable 绑定默认配置
invoke(..., config=...)   = 给本次调用传临时配置
ainvoke / astream         = 真正执行 Agent
```

`with_config` 也不等于持久化：它不会创建 thread、写入 Store 或保存 checkpoint。`thread_id` 只是被传给 LangGraph Runtime，真正的 checkpoint 持久化由 checkpointer/runtime 完成。

最小验证：

```python
from langchain_core.runnables import RunnableLambda

runnable = RunnableLambda(
    lambda value, config: {
        "value": value,
        "thread_id": config["configurable"]["thread_id"],
    }
)
bound = runnable.with_config({"configurable": {"thread_id": "thread-1"}})
assert bound.invoke("hello")["thread_id"] == "thread-1"
```

这段代码只验证配置传播，不调用模型、不创建 Run。

## 10. 最小源码验证

只检查导入和符号来源，不触发真实模型：

```bash
uv run python -c \
  'import inspect; from deepagents import create_deep_agent; print(inspect.getsourcefile(create_deep_agent))'
```

预期路径包含：

```text
.venv/lib/python3.11/site-packages/deepagents/graph.py
```

再运行 Open SWE 的装配测试：

```bash
uv run pytest -q tests/agent/test_agent_assembly_context.py tests/models/test_agent_subagent_models.py
```

这些测试主要验证 backend、工具、middleware 顺序和主/子模型配置，不会要求你真的创建远程 Sandbox。

## 常见误区

1. `get_agent` 是 `create_deep_agent` 的实现。错，前者是应用装配器，后者是第三方图工厂。
2. `create_deep_agent` 会自动知道当前用户和仓库。错，这些由 `get_agent` 和运行前 middleware 注入。
3. `create_deep_agent` 调用返回就开始干活。错，它只构图；Run 执行时才调用模型。
4. `tools=[]` 代表没有任何工具。错，Deep Agents 仍可能根据 backend 加入内置工具；是否有文件能力取决于 backend 和 middleware。

## 小结

学习 Open SWE 的关键不是背下一个很长的函数调用，而是看清边界：`get_agent` 决定“这次运行允许拥有什么能力”，`create_deep_agent` 把这些能力编译成图，LangGraph Runtime 再负责执行和恢复。

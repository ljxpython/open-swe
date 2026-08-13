# 第 2-1 章：主 Agent 工厂 `agent.server:get_agent`

## 学习目标

本章只研究主 Agent 的装配工厂。读完后，你应能：

1. 解释为什么 `get_agent` 是异步工厂，而不是一个全局 Agent 单例。
2. 沿着 `config -> backend -> model -> tools -> middleware -> create_deep_agent` 读懂源码。
3. 判断一次请求最终使用了哪个模型、哪些工具，以及是否拥有 sandbox。
4. 修改工具或 middleware 时，知道应该改哪里、哪些边界不能破坏。

## 先看全貌

`langgraph.json` 注册的是 `agent.graphs.agent:traced_agent`。这个符号不是直接等于 `get_agent`，而是：

```text
traced_agent
  -> traced_graph_factory(get_agent, "open-swe-agent")
  -> await get_agent(config)
  -> Pregel graph
  -> LangGraph 执行该 graph
```

`traced_graph_factory` 做两件事：调用工厂拿到图，并在 `langsmith.tracing_context` 中暴露 `open-swe-agent` 项目上下文。真正决定 Agent 能力的是 `get_agent`，真正把能力编译成图的是 Deep Agents 的 `create_deep_agent`。

源码：

- [图注册](../../langgraph.json)
- [追踪包装器](../../agent/utils/tracing.py)
- [主工厂](../../agent/server.py)

![主 Agent 工厂装配时序](architecture/premium/png/04-agent-factory-sequence.png)

读图时从左向右追踪依赖，从上向下追踪装配顺序：先决定是否是执行模式，再解析身份和 profile，接着准备 sandbox 与模型，最后把工具和 middleware 交给 `create_deep_agent`。这张图是本章后面每个小节的索引。

## 一、工厂为什么是异步的

函数签名是：

```python
async def get_agent(config: RunnableConfig) -> Pregel:
```

它不是简单地 `return agent`，因为每个执行线程都要解析运行时数据：

- 读取团队默认模型和 effort。
- 读取用户 profile 覆盖项。
- 判断 Gateway、Fable、LangSmith/Datadog 等能力是否开启。
- 从线程 metadata 找到或重新连接 sandbox。
- 加载可选的 MCP/外部工具。
- 读取仓库和用户自定义指令。

这些操作大多是异步 I/O，所以工厂本身必须是 async。它仍然不是“每次都从零创建所有东西”：团队设置、profile 和动态工具使用 TTL cache；sandbox backend 也按 `thread_id` 缓存。换句话说，**图实例按执行上下文装配，昂贵资源按线程或 TTL 复用**。

## 二、第一道分支：构图检查模式与执行模式

函数开头先读取：

```python
configurable = config.get("configurable") or {}
thread_id = configurable.get("thread_id")
config["recursion_limit"] = DEFAULT_RECURSION_LIMIT
```

随后判断：

```python
if thread_id is None or not graph_loaded_for_execution(config):
    return create_deep_agent(system_prompt="", tools=[]).with_config(config)
```

`graph_loaded_for_execution` 只检查 `configurable["__is_for_execution__"]`。这形成两个明确模式：

| 模式 | 条件 | 返回内容 | 为什么这样做 |
| --- | --- | --- | --- |
| 加载/检查图 | 没有 `thread_id`，或没有执行标记 | 空提示词、空工具的最小 Deep Agent | LangGraph 启动、检查或展示图结构时不连接用户 sandbox |
| 真正执行 | 有 `thread_id` 且有执行标记 | 完整 Agent | 只有真正运行时才允许访问线程资源和外部能力 |

这里的 `config` 会被工厂补上 `recursion_limit`，但 `configurable` 主要来自调用方。不要把“图加载成功”误解成“已经创建 sandbox”；sandbox 只在执行分支出现。

## 三、准备线程上下文与 sandbox backend

通过第一道分支后，工厂解析触发用户：

```python
profile_login = resolve_github_login(as_json_object(config))
```

然后并发取得四项数据：

```python
team_defaults, use_gateway, profile, fable_enabled = await asyncio.gather(...)
```

这四项分别是：

1. 主 Agent 和子 Agent 的团队默认模型对。
2. 是否使用 LangSmith LLM Gateway。
3. 当前 GitHub 用户的 Dashboard profile。
4. 是否允许 Fable 模型。

工厂再创建一个 `reconnect_backend` 闭包，把当前 `thread_id` 和 `configurable` 固定下来：如果 backend 缓存失效，Deep Agents 重新请求 backend 时仍能回到同一条线程。

```python
backend = _get_cached_sandbox_backend(
    thread_id,
    reconnect=reconnect_backend,
)
```

`ensure_sandbox_for_thread` 的核心策略是：

```text
内存有 backend -> ping -> 刷新 GitHub proxy
只有 metadata 有 sandbox_id -> reconnect -> ping -> 刷新 proxy
两者都没有 -> 创建 sandbox -> 写回 thread metadata
```

默认情况下，已有 sandbox 失联不会悄悄换成空 sandbox，因为这样会把未提交代码直接丢掉。这个保护是主 Agent 和本章工厂的重要安全边界。

## 四、模型解析的三层覆盖

工厂首先拿团队默认模型：

```python
(model_id, profile_effort), (subagent_model_id, subagent_effort) = team_defaults
```

随后按优先级覆盖：

```text
团队默认
  -> 用户 profile 的主模型覆盖
  -> 用户 profile 的子 Agent 模型覆盖
  -> configurable.agent_model_id / agent_effort 的线程覆盖
  -> gate_fable_model 最终校正
```

线程覆盖只有在 `model_id` 属于 `SUPPORTED_MODEL_IDS` 且 effort 对该模型有效时才生效；`canonical_model_pair` 会把已经改名或废弃的模型 ID 规范化。

最后调用 `provider_model_kwargs` 生成提供商参数，并调用 `_make_model_or_defer`：

```python
main_model = _make_model_or_defer(model_id, ...)
subagent_model = _make_model_or_defer(subagent_model_id, ...)
```

`_make_model_or_defer` 的意义是：模型初始化失败时先构造延迟错误模型，让图仍能加载；真正运行到模型调用时再暴露具体配置错误。这样一个坏的模型配置不会阻塞整个 LangGraph 服务启动。

当存在 `LLM_FALLBACK_MODEL_ID` 或默认跨提供商回退模型时，工厂还会把 `ModelFallbackMiddleware` 放入 middleware。回退模型不是第二个主 Agent，而是模型调用失败时的备用模型。

模型的具体 OpenAI 兼容地址和 Chat Completions/Responses 选择在 [agent/utils/model.py](../../agent/utils/model.py) 中完成；本章只关注工厂如何把模型放进图。

## 五、按权限加载可选工具

静态工具是 `static_tools` 列表，例如：

- `http_request`、`fetch_url`、`web_search`。
- `approve_plan`、`enter_plan_mode`、`save_plan`。
- Linear 评论/Issue 工具。
- PR、sandbox、Slack 回复和唤醒工具。

Deep Agents 的内置文件和终端工具不在这个列表里，它们由 `create_deep_agent` 自动提供；源码中的 `DEEP_AGENT_TOOL_NAMES` 主要用于动态工具命名冲突保护。

可选工具按权限和配置加载：

| 工具组 | 加载条件 |
| --- | --- |
| Observability | 管理员/授权用户，或满足组织成员规则的 LangSmith 工具 |
| Corridor | 配置了 Corridor MCP token |
| Currents、Notion | 有已解析的用户登录身份 |
| Browser subagent | `load_browser_tools()` 返回工具 |

这些可选工具不会因为加载失败就让主 Agent 无法启动；`_cached_tool_loader` 会超时、记录日志并返回空列表。这是一个刻意的降级策略：少几个可选工具，主编码流程仍可工作。

如果至少一个动态工具组非空，工厂创建 `DynamicToolMiddleware`，并把内置工具名和静态工具名放进 `reserved_names`，避免两个工具注册成同一个名字。

## 六、backend 与用户 skills 路由

没有 profile login 时，Agent 直接使用线程 sandbox backend：

```text
默认路径 -> 当前线程 sandbox
```

有 profile login 时，工厂改用 `CompositeBackend`：

```text
默认路径       -> sandbox backend
/skills/ 路径  -> StoreBackend(namespace=("skills", login))
```

`/skills/` 又被 `ReadOnlyBackend` 包住，因此用户保存的 skills 可以被 Agent 读取，但不能被普通文件工具随意覆盖。这个路由是 backend 层能力，不是一个额外的模型或工具。

## 七、`create_deep_agent` 是装配终点

最终工厂把以下部件交给 Deep Agents：

```python
create_deep_agent(
    model=main_model,
    system_prompt="",
    tools=static_tools,
    subagents=[general_purpose_subagent, optional_browser_subagent],
    skills=skill_sources,
    backend=agent_backend,
    middleware=[...],
)
```

这里的 `system_prompt` 先传空字符串并非遗漏。真正依赖线程、仓库、用户和 sandbox 的 prompt 在 `PrepareAgentRunMiddleware._prepare` 中通过 `construct_system_prompt` 注入，因为这些数据只能在本次执行准备阶段可靠解析。

主 Agent 和通用子 Agent 使用不同的 model 实例。子 Agent 会编译成自己的图，所以父图的 middleware 不会自动包住子 Agent；工厂显式给子 Agent 加了 `ModelCallTimeoutMiddleware`，防止子任务里的模型调用无限等待。

## 十、完整案例：用户发送“分析这个仓库的启动入口”

下面不是脱离项目的伪造 Agent，而是把当前 UI 和后端真实使用的字段代入一次请求。为了避免泄露账号、仓库和 token，示例中的登录名、线程 ID 和仓库名是教学替身；字段形状来自源码。

### 10.1 首页如何提交

用户在 `/agents` 首页输入：

```text
分析这个仓库的启动入口，先不要修改文件。
```

`ui/src/features/agents/components/AgentsHome.tsx` 会把它转换成：

```json
{
  "messages": [
    {
      "type": "human",
      "content": [
        {"type": "text", "text": "分析这个仓库的启动入口，先不要修改文件。"}
      ]
    }
  ]
}
```

如果用户选择了模型和仓库，前端还会附加：

```json
{
  "config": {
    "configurable": {
      "agent_model_id": "openai:gpt-5.6-terra",
      "agent_effort": "medium",
      "repo": {"owner": "example", "name": "demo-repo"},
      "plan_mode": true
    }
  }
}
```

首页的 `StreamProvider` 没有固定 thread ID。SDK 首次 `stream.submit` 时会生成一个 ID，例如 `t-abc123`，然后调用 Dashboard 的命令代理。这里的持续 Provider 很重要：用户从首页跳转到 `/agents/t-abc123` 时，原来的 SSE 流不会因为路由切换而被销毁。

源码：

- 首页提交与配置字段：[AgentsHome.tsx](../../ui/src/features/agents/components/AgentsHome.tsx)
- 持续流 Provider：[AgentThreadStreamProvider.tsx](../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx)

### 10.2 Dashboard 命令代理如何补全请求

SDK 发出的命令核心形状是：

```json
{
  "method": "run.start",
  "params": {
    "input": {
      "messages": [
        {"type": "human", "content": "分析这个仓库的启动入口，先不要修改文件。"}
      ]
    },
    "config": {
      "configurable": {
        "agent_model_id": "openai:gpt-5.6-terra",
        "agent_effort": "medium",
        "repo": {"owner": "example", "name": "demo-repo"},
        "plan_mode": true
      }
    }
  }
}
```

`agent/dashboard/routes.py` 把它交给 `proxy_dashboard_thread_commands`。这个函数先做三件事：

1. 读取线程；首次请求如果线程不存在，只允许 `run.start`，并标记 `creating=True`。
2. 调用 `_enrich_run_start_command`，验证登录用户的 GitHub token，创建线程 metadata，并把客户端传来的仓库提示转换为服务端可信 metadata。
3. 调用 `_build_dashboard_configurable` 重建运行配置，而不是原样信任客户端输入。

对于教学示例，重建后的 `configurable` 大致是：

```json
{
  "thread_id": "t-abc123",
  "source": "dashboard",
  "github_login": "example-user",
  "user_email": "example-user@example.com",
  "repo": {"owner": "example", "name": "demo-repo"},
  "plan_mode": true,
  "agent_model_id": "openai:gpt-5.6-terra",
  "agent_effort": "medium"
}
```

服务端会强制指定 `assistant_id`，并在客户端未给出时补上流模式和可恢复流默认值：

```json
{
  "assistant_id": "agent",
  "stream_mode": ["values", "updates", "messages", "messages-tuple", "tools", "checkpoints", "events"],
  "stream_resumable": true
}
```

这一步的关键设计是：**客户端只能提出选择，服务端才是权限和最终配置的裁判**。例如用户传入一个不支持的模型 ID，不会直接进入 `get_agent`。

源码：[proxy_dashboard_thread_commands](../../agent/dashboard/thread_api.py) 和 [_enrich_run_start_command](../../agent/dashboard/thread_api.py)。

### 10.3 `get_agent` 收到什么

LangGraph Runtime 处理命令后，会以类似下面的 `RunnableConfig` 调用 `traced_agent`：

```python
config = {
    "configurable": {
        "thread_id": "t-abc123",
        "__is_for_execution__": True,
        "source": "dashboard",
        "github_login": "example-user",
        "user_email": "example-user@example.com",
        "repo": {"owner": "example", "name": "demo-repo"},
        "plan_mode": True,
        "agent_model_id": "openai:gpt-5.6-terra",
        "agent_effort": "medium",
    }
}
```

`traced_graph_factory` 先调用 `get_agent(config)`。此时第一道分支通过：有 `thread_id`，且 `__is_for_execution__` 为真，所以不会返回空 Agent。

### 10.4 工厂内部的结果快照

把 `get_agent` 想象成一个装配台，执行顺序可以记录成以下快照：

| 阶段 | 代码动作 | 示例结果 |
| --- | --- | --- |
| 入口 | 读取 `thread_id`、设置 recursion limit | `t-abc123`、`9999` |
| 身份 | `resolve_github_login` | `example-user` |
| 默认值 | 并发读取团队设置、profile、Gateway、Fable | 得到主/子 Agent 模型对 |
| backend | `_get_cached_sandbox_backend` | 一个指向该线程 sandbox 的稳定 proxy |
| 模型 | 线程配置覆盖团队/profile 默认 | 主模型 `openai:gpt-5.6-terra` |
| 工具 | 静态工具 + 当前用户允许的动态工具 | 文件工具由 Deep Agents 自动补入 |
| backend 路由 | `CompositeBackend` | `/skills/` 只读映射到该用户 Store |
| 图编译 | `create_deep_agent` | 返回 `Pregel` |
| 配置回传 | `.with_config(config)` | 图携带本次 Run 的 configurable |

注意：模型对象在这里通常只是初始化并放入图中；真正的网络请求发生在图执行到模型节点时，不是 `get_agent` 函数一进入就发送 Chat Completions。

### 10.5 第一个模型调用前发生什么

图开始执行后，`PrepareAgentRunMiddleware.abefore_agent` 先根据“最新用户消息 + 工厂配置”计算 fingerprint。若 checkpoint 已经记录了同一个 fingerprint，恢复执行会跳过重复准备；否则调用 `PrepareAgentRunMiddleware._prepare`：

```text
resolve_github_token
  -> ensure_sandbox_for_thread("t-abc123")
  -> aresolve_sandbox_work_dir
  -> record_turn_checkpoint
  -> 更新 thread metadata
  -> construct_system_prompt
  -> 返回 rendered_system_prompt + work_dir
```

随后 `awrap_model_call` 把 `rendered_system_prompt` 合并到真正发给模型的 system message。于是模型看到的不是只有用户那一句话，而是：Open SWE 基础规则、仓库/用户指令、仓库坐标、工作目录、计划模式等完整上下文。

源码：[BasePrepareRunMiddleware](../../agent/middleware/prepare_run.py) 和 [PrepareAgentRunMiddleware](../../agent/server.py)。

### 10.6 如果用户在运行中再次发送一句话

`ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts` 会优先调用 `/messages` 队列接口。后端检测到线程忙碌后，把内容存到：

```text
namespace = ("queue", "t-abc123")
key       = "pending_messages"
```

当前 Run 的下一次模型调用前，`check_message_queue_before_model` 消费该项，把追问追加为新的 HumanMessage。这样不会并发创建第二个主 Agent，也不会丢失正在进行的工作。

已有线程空闲时，前端会通过同一个 StreamProvider 发送新的 `run.start`；服务端会复用 `t-abc123` 的 thread metadata 和 sandbox，但创建一个新的 `run_id`。

源码：[useSubmitAgentMessage.ts](../../ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts)。

## 可以安全运行的验证

以下命令只验证工厂导出，不会进入真实执行分支：

```bash
uv run python -c 'from agent.server import get_agent, traced_agent; print(get_agent.__name__, callable(traced_agent))'
```

输出应为：

```text
get_agent True
```

此外，以下测试验证了案例第 10.2 节的两项关键行为：首次 `run.start` 创建并标记线程；最终配置使用服务端可信身份和仓库，而不是客户端伪造字段。

```bash
uv run pytest -vvv \
  tests/dashboard/test_dashboard_thread_api.py::test_enrich_run_start_command_creates_and_stamps_new_thread \
  tests/dashboard/test_dashboard_thread_api.py::test_enrich_run_start_command_allowlists_client_configurable
```

当前结果：2 项通过。

进入真实执行分支需要有效的 Dashboard session、LangGraph Store、sandbox 和模型服务；那会创建/复用线程资源并产生模型调用费用，所以本章不自动执行。需要做真实调用时，应明确选择测试仓库和成本上限后再运行。

## 本案例的关键结论

1. 浏览器不是直接调用 `get_agent`，而是提交 LangGraph command；后端先鉴权、补全和重建配置。
2. `get_agent` 不处理具体用户消息，它负责把本次执行需要的资源装配成 `Pregel`。
3. 第一个模型调用前，准备 middleware 才会连接 sandbox、生成动态 system prompt 和 Git diff 起点。
4. 同一线程的新 Run 会复用持久化上下文；同一 Run 的追问则进入队列，由 before-model middleware 注入。

## 八、middleware 的设计意图

工厂安装的主要 middleware 可以分成四层：

| 层 | 组件 | 作用 |
| --- | --- | --- |
| 执行准备 | `PrepareAgentRunMiddleware` | sandbox、身份、prompt、turn checkpoint、线程元数据 |
| 输入与工具 | `SanitizeToolInputsMiddleware`、`ToolErrorMiddleware`、`ToolRetryMiddleware` | 规范化参数、把工具异常转成可处理消息、重试子任务 |
| 运行控制 | `ModelCallLimitMiddleware`、`ModelCallTimeoutMiddleware`、`ModelFallbackMiddleware` | 限制递归/模型调用、超时、切备用模型 |
| 交互与安全 | `PlanModeMiddleware`、`check_message_queue_before_model`、`PullRequestCreationGuardMiddleware`、`refresh_github_proxy_before_model` | 计划模式、插入追问、约束 PR、刷新 GitHub 代理凭据 |

列表最后的 `ModelCallTimeoutMiddleware` 是故意的：它要包住最内层的 provider 调用，使超时能向外传播给 fallback middleware。

## 九、为什么说 Agent 本身是无状态的

`get_agent` 每次根据 `config` 构造一个图；它不把用户消息、sandbox 内容或 profile 写入 Python 全局 Agent 对象。持续性分别放在：

- LangGraph thread/checkpoint：图状态和消息历史。
- thread metadata：模型、来源、sandbox ID、turn refs 等索引信息。
- sandbox：代码、分支和未提交工作。
- Store：队列、用户 skills 和 Dashboard 辅助数据。

所以“重新构造 Agent”不会等于“新建任务”。只要 `thread_id` 和相关持久化数据还在，新的工厂实例仍能接回原任务。

## 最小验证

只验证工厂的导出和追踪包装，不启动 sandbox、不调用模型：

```bash
uv run python -c 'from agent.server import get_agent, traced_agent; print(get_agent.__name__, callable(traced_agent))'
```

真正调用 `get_agent` 的执行分支会触发 profile、LangGraph Store、sandbox 和模型配置，因此不在这个无副作用示例中伪造运行结果。需要真实 Agent 调用时，下一阶段使用已经配置好的 WawAPI，并单独记录外部调用。

## 常见误区

1. 把 `get_agent` 当作只负责“选模型”的函数。它同时决定 sandbox、工具、子 Agent、middleware 和动态 backend。
2. 认为 `create_deep_agent` 会自动知道仓库和用户。仓库、身份和工作目录由 `PrepareAgentRunMiddleware` 注入。
3. 认为所有工具都在 `static_tools`。文件、终端、`task` 等 Deep Agents 内置工具由框架自动加入。
4. 认为模型初始化失败一定会让服务启动失败。主工厂用 deferred error model 延迟暴露部分模型配置错误。

## 改造练习

1. 在不运行真实模型的前提下，指出新增一个静态工具需要修改的源码位置。
2. 如果希望一个工具只对组织成员开放，应该放在静态列表，还是动态加载路径？说明理由。
3. 追踪 `agent_model_id` 从 Dashboard 的 `run.start` 命令到 `make_model` 的路径。
4. 解释为什么不能在 sandbox 失联时默认替换主 Agent 的 sandbox。

## 本章边界

本章已经解释主工厂的装配过程，但没有深入每个 middleware 的内部实现、Deep Agents 内置工具协议和 sandbox provider 的具体实现。下一节可从 middleware 顺序开始，继续拆解一次模型调用前后发生了什么。

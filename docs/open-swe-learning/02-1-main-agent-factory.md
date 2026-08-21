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

<img src="architecture/premium/png/04-agent-factory-sequence.png" alt="主 Agent 工厂装配时序" style="zoom:150%;" />

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

## 十一、逐行源码对照

这一节对应当前 agent/server.py 中的 get_agent，源码范围是 953-1237 行。agent/graphs/agent.py 只有导出代码，不包含业务实现。

### 11.1 agent/graphs/agent.py：导出层

    from agent.server import get_agent, traced_agent

第 1 行从 agent.server 导入两个名称：

- get_agent：真正的异步 Agent 工厂。
- traced_agent：经过 LangSmith tracing 包装后的 LangGraph 入口。

    __all__ = ["get_agent", "traced_agent"]

第 3 行声明该模块的公共导出名称。langgraph.json 引用的是 agent.graphs.agent:traced_agent，因此这个文件是稳定门面。

### 11.2 953-965 行：函数入口和加载阶段短路

    async def get_agent(config: RunnableConfig) -> Pregel:

第 953 行定义异步工厂。输入是当前 Run 的 RunnableConfig，输出是一张可执行的 Pregel 图。它负责装配 Agent，不负责处理某一条用户消息。

    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")

第 955-956 行读取运行时自定义参数和线程 ID。后续 sandbox、checkpoint、消息队列和线程 metadata 都依赖这个 ID。

    config["recursion_limit"] = DEFAULT_RECURSION_LIMIT

第 958 行设置整张图的递归/步骤上限。这里是原地修改传入的 config，调用方会看到字段被补上或覆盖。

    if thread_id is None or not graph_loaded_for_execution(config):

第 960 行判断是不是正式执行阶段。没有 thread_id，或者 LangGraph 只是加载/检查图时，都会进入短路分支。

    logger.info(...)
    return create_deep_agent(
        system_prompt="",
        tools=[],
    ).with_config(config)

第 961-965 行返回一个空壳 Deep Agent：

- 不初始化 sandbox；
- 不加载业务工具；
- 不创建完整模型；
- 仍然绑定 config，让 LangGraph 能识别这张图。

### 11.3 967-975 行：解析身份并并发读取设置

    profile_login = resolve_github_login(as_json_object(config))

第 967 行从配置中解析 GitHub 登录名，用于读取用户 Profile、用户 skills 和权限。

    team_defaults, use_gateway, profile, fable_enabled = await asyncio.gather(
        _cached_team_default_model_pair("agent"),
        _cached_gateway_enabled(),
        _cached_profile(profile_login),
        _cached_fable_enabled(),
    )

第 970-975 行并发读取四项数据：

| 变量 | 内容 |
| --- | --- |
| team_defaults | 主 Agent 和子 Agent 的团队默认模型对 |
| use_gateway | 是否通过 LLM Gateway |
| profile | 当前用户 Profile 或 None |
| fable_enabled | 团队是否允许 Fable 模型 |

gather 的返回顺序固定对应参数顺序，不按完成先后排列。四个缓存函数减少远程 Store 读取。

### 11.4 977-988 行：Linear 上下文和 sandbox backend

    linear_issue = as_json_object(configurable.get("linear_issue"))
    linear_project_id = linear_issue.get("linear_project_id", "")
    linear_issue_number = linear_issue.get("linear_issue_number", "")

第 977-979 行读取 Linear project ID 和 issue 编号。没有 Linear 上下文时使用空字符串；这些值稍后交给 PrepareAgentRunMiddleware。

    async def reconnect_backend(
        _thread_id: str = thread_id,
        _configurable: dict[str, Any] = configurable,
    ) -> SandboxBackendProtocol:

第 981-984 行定义 sandbox 重连闭包。默认参数把本次调用的 thread_id 和配置固定下来，避免后续重连时丢失线程上下文。

    prompt_default_repo = await _resolve_prompt_default_repo(_configurable)

第 985 行解析提示词所需的默认仓库，可能来自线程显式配置、Linear issue 或团队默认仓库。

    return await ensure_sandbox_for_thread(
        _thread_id,
        repo=prompt_default_repo,
    )

第 986 行确保线程 sandbox 可用。它可能复用、重连或创建 sandbox。

    backend = _get_cached_sandbox_backend(
        thread_id,
        reconnect=reconnect_backend,
    )

第 988 行获取线程对应的 backend。缓存命中时直接使用；缓存缺失时使用刚才的闭包重连。

### 11.5 990-1017 行：团队默认和用户 Profile 覆盖

    (model_id, profile_effort), (subagent_model_id, subagent_effort) = team_defaults

第 990 行把团队默认值拆成主 Agent 和子 Agent 两组模型配置。

    if profile_login and profile:

第 993 行只有用户身份和 Profile 都存在时，才应用用户级覆盖。

    overridden_model, overridden_effort = normalize_profile_overrides(profile)

第 994 行读取并规范化 Profile 中的主模型覆盖。

    model_id = overridden_model
    profile_effort = overridden_effort
    subagent_model_id = overridden_model
    subagent_effort = overridden_effort

第 1002-1005 行应用主模型覆盖，并让子 Agent 暂时跟随同一个模型。

    overridden_subagent_model, overridden_subagent_effort = (
        normalize_profile_subagent_overrides(profile)
    )

第 1006-1008 行单独读取子 Agent 覆盖。

    subagent_model_id = overridden_subagent_model
    subagent_effort = overridden_subagent_effort

第 1016-1017 行如果用户专门配置了子 Agent 模型，则覆盖刚才的跟随值。

这一段的优先级是：

    团队默认
      -> 用户 Profile 主模型
      -> 用户 Profile 子 Agent 模型

### 11.6 1019-1038 行：线程级模型覆盖

    per_thread_model = configurable.get("agent_model_id")
    per_thread_effort = configurable.get("agent_effort")

第 1019-1020 行读取当前线程显式选择的模型和 effort。

    canonical_per_thread = canonical_model_pair(
        per_thread_model,
        per_thread_effort,
    )

第 1021 行把别名或旧格式转换成规范模型对。

    if canonical_per_thread is not None:
        per_thread_model, per_thread_effort = canonical_per_thread

第 1022-1023 行使用规范化结果。

第 1024-1029 行验证模型覆盖：

- 模型 ID 必须是字符串；
- 模型必须在 SUPPORTED_MODEL_IDS 中；
- effort 必须是字符串；
- 该模型必须支持这个 effort。

    model_id = per_thread_model
    profile_effort = per_thread_effort
    subagent_model_id = per_thread_model
    subagent_effort = per_thread_effort

第 1035-1038 行验证通过后，线程级设置同时覆盖主 Agent 和子 Agent。

最终优先级是：

    团队默认 < 用户 Profile < 当前线程配置

### 11.7 1040-1050 行：PR 配置和 Fable 门控

    always_create_prs = profile_create_prs(profile)
    draft_prs = profile_draft_prs(profile)

第 1040-1041 行读取用户是否总是创建 PR，以及是否创建 Draft PR。

    model_id, profile_effort = gate_fable_model(...)
    subagent_model_id, subagent_effort = gate_fable_model(...)

第 1045-1050 行对主 Agent 和子 Agent 做 Fable 模型门控。如果团队关闭 Fable，残留的 Fable 模型会被降级。

### 11.8 1052-1074 行：provider 参数和 fallback

    model_kwargs = provider_model_kwargs(...)
    subagent_model_kwargs = provider_model_kwargs(...)

第 1052-1061 行把统一模型配置转换成具体 provider 所需参数，例如 reasoning 和 max tokens。

    fallback_model_id = (
        os.environ.get("LLM_FALLBACK_MODEL_ID")
        or fallback_model_id_for(model_id)
    )

第 1063 行确定 fallback 模型：优先环境变量，否则根据主模型推导。

    if fallback_model_id and fallback_model_id != model_id:

第 1065 行只有存在不同备用模型时才启用 fallback。

    fallback_middleware.append(
        ModelFallbackMiddleware(
            _make_model_or_defer(...)
        )
    )

第 1069-1072 行创建备用模型并包装成 ModelFallbackMiddleware。主模型调用失败时，它负责切换备用模型。

_make_model_or_defer 的意义是模型初始化失败时先返回延迟错误模型，让图可以加载，真正调用模型时再暴露错误。

### 11.9 1076-1092 行：来源、用户和 Plan Mode

    source_value = configurable.get("source")
    source = source_value if isinstance(source_value, str) else "dashboard"

第 1076-1077 行读取请求来源，缺失时默认 dashboard。

    user_email = configurable.get("user_email")
    user_email = user_email if isinstance(user_email, str) else ""

第 1078-1079 行读取用户邮箱并稳定类型。

    plan_mode = configurable.get("plan_mode") is True

第 1087 行只接受严格的 True，不会把字符串 "true" 或数字 1 当成开启。

    plan_mode_middleware: list[Any] = [
        PlanModeMiddleware(
            excluded=PLAN_MODE_EXCLUDED_TOOLS,
            initial=plan_mode,
        )
    ]

第 1090-1092 行无论当前是否已进入 Plan Mode，都安装 middleware。因为模型可能在运行中调用 enter_plan_mode，工具限制必须动态生效。

### 11.10 1094-1118 行：按权限加载集成工具

第 1094 行通过 _observability_authorized 检查完整可观测性权限。

第 1095-1096 行，授权用户加载完整 Observability 工具。

第 1097-1098 行，组织成员但权限较低时加载组织允许的 LangSmith 工具。

第 1099-1100 行，其他用户只能使用个人范围的 LangSmith 工具。

    corridor_tools = await _load_corridor_mcp_tools()
    browser_tools = load_browser_tools()

第 1101-1102 行加载可选 Corridor MCP 工具和浏览器工具。

    currents_tools: list[Any] = []
    notion_tools: list[Any] = []

第 1104-1105 行初始化为空，避免没有用户身份时访问第三方服务。

第 1106-1118 行在有 profile_login 时并行加载 Currents 和 Notion，使用 300 秒缓存。失败时降级为空列表，不阻塞主 Agent。

### 11.11 1120-1159 行：静态工具和动态工具

第 1120-1147 行声明 static_tools，包含网络、Plan Mode、用户 skills、Linear、GitHub PR、sandbox、Slack 和唤醒工具。

文件、终端和 task 等 Deep Agents 内置工具不在这里，它们由 create_deep_agent 自动加入。

    dynamic_tool_middleware: DynamicToolMiddleware | None = None

第 1148 行默认没有动态工具中间件。

    integration_tool_groups = {
        "Corridor": corridor_tools,
        "Observability": observability_tools,
        "Currents": currents_tools,
        "Notion": notion_tools,
    }

第 1149-1154 行按集成来源分组外部工具。

    if any(integration_tool_groups.values()):

第 1155 行只要有一组非空，就安装动态工具中间件。

    dynamic_tool_middleware = DynamicToolMiddleware(
        integration_tool_groups,
        reserved_names={...},
    )

第 1156-1159 行创建动态工具中间件，并保留内置工具名和静态工具名，防止动态工具重名覆盖。

### 11.12 1161-1181 行：backend、skills 和模型实例

    agent_backend: BackendProtocol = backend
    skill_sources: list[str] | None = None

第 1162-1163 行默认使用 sandbox backend，没有额外 skills 路由。

第 1164-1175 行有用户身份时构造 CompositeBackend：

    普通路径 -> sandbox
    USER_SKILLS_ROUTE -> 用户自己的 Store

用户 skills 路由被 ReadOnlyBackend 包装，所以 Agent 可以读取 skills，但不能通过普通文件工具随意覆盖。

    main_model = _make_model_or_defer(...)
    subagent_model = _make_model_or_defer(...)

第 1176-1181 行构造主 Agent 和子 Agent 模型。初始化失败仍采用延迟错误模型策略。

### 11.13 1182-1237 行：创建 Deep Agent

第 1182 行调用 create_deep_agent，开始组装最终图。

第 1183 行 model=main_model，指定主模型。

第 1184 行 system_prompt=""，说明初始 prompt 为空；真实 prompt 在 PrepareAgentRunMiddleware 中动态构造。

第 1185 行 tools=static_tools，注入固定工具。

第 1186-1189 行配置子 Agent：

- general-purpose 始终存在；
- browser 只有浏览器工具存在时才加入；
- 子 Agent 使用独立模型和自己的模型超时保护。

第 1190-1191 行把 skills 路由和文件 backend 交给 Deep Agents。

第 1192-1236 行安装 middleware，顺序不能乱：

1. PrepareAgentRunMiddleware：sandbox、prompt、身份和 checkpoint；
2. DynamicToolMiddleware：外部集成工具；
3. SanitizeToolInputsMiddleware：规范工具参数；
4. ModelCallLimitMiddleware：限制模型调用；
5. ToolErrorMiddleware：处理工具异常；
6. SubdirAgentsReadMiddleware：读取目录级 AGENTS.md；
7. ToolRetryMiddleware：重试 task 子 Agent；
8. PullRequestCreationGuardMiddleware：限制 PR 创建；
9. refresh_github_proxy_before_model：刷新 GitHub 代理；
10. check_message_queue_before_model：注入运行中追加消息；
11. TimeoutWrapupMiddleware：超时收尾；
12. notify_step_limit_reached：通知达到步骤限制；
13. fallback middleware：切换备用模型；
14. PlanModeMiddleware：动态限制工具；
15. provider 消息清理；
16. ModelCallTimeoutMiddleware：包住真实 provider 请求。

最后一个 timeout middleware 放在最内层，是为了让超时覆盖真正的模型调用，并让异常向外传播给 fallback。

    ).with_config(config)

第 1237 行把当前 RunnableConfig 绑定到最终图并返回。

### 11.14 1240 行：包装成 LangGraph 入口

    traced_agent = traced_graph_factory(
        get_agent,
        AGENT_TRACING_PROJECT,
    )

第 1240 行将 get_agent 包装为带 LangSmith tracing context 的入口。

实际链路：

    langgraph.json
      -> agent.graphs.agent:traced_agent
      -> traced_graph_factory
      -> get_agent(config)
      -> create_deep_agent(...)
      -> Pregel graph
      -> LangGraph Runtime 执行

## 十二、读完 get_agent 后必须记住的五件事

1. agent/graphs/agent.py 是导出层，实际实现位于 agent/server.py。
2. get_agent 的核心职责是装配图，不是直接处理用户消息。
3. 模型优先级是团队默认、用户 Profile、线程覆盖，最后还要经过 Fable 门控。
4. sandbox、工具、skills、子 Agent 和 middleware 都在这里汇合。
5. 图加载阶段返回空壳 Agent，只有正式执行阶段才使用真实线程资源。


+## 十三、配置数据从哪里来，记录在哪里

get_agent 并不是从某一个 config.py 读取全部配置。它把多个来源的数据合并到当前运行中：

    代码常量
        +
    LangGraph Store
        +
    Thread metadata
        +
    本次 Run 的 configurable
        +
    部署环境变量
        +
    进程内缓存
        ->
    最终 Pregel Agent

### 13.1 团队设置：LangGraph Store

团队配置由 agent/dashboard/team_settings.py 管理：

    TEAM_SETTINGS_NAMESPACE = ["team_settings"]
    TEAM_SETTINGS_KEY = "default"

持久化键是：

    namespace = ["team_settings"]
    key       = "default"

读取链路：

    _cached_team_default_model_pair("agent")
        -> get_team_default_model_pair("agent")
        -> get_team_settings()
        -> get_client().store.get_item(["team_settings"], "default")

写入链路：

    Dashboard PUT /team-settings
        -> upsert_team_settings()
        -> get_client().store.put_item(
               ["team_settings"],
               "default",
               value,
           )

团队记录中的重要字段：

    default_agent_model
    default_agent_reasoning_effort
    default_agent_subagent_model
    default_agent_subagent_reasoning_effort
    default_reviewer_model
    default_reviewer_reasoning_effort
    gateway_enabled
    fable_enabled
    default_repo

代码位置：

- 读取和合并默认值：[team_settings.py](../../agent/dashboard/team_settings.py)
- 管理员写入路由：[routes.py](../../agent/dashboard/routes.py)
- get_agent 的缓存包装：[server.py](../../agent/server.py)

这里的 Store 是 LangGraph Store 抽象，仓库没有把底层物理数据库写死。具体是本地内存、远程 Store 还是部署平台提供的持久化后端，由 LangGraph Runtime 配置决定。

### 13.2 用户 Profile：按 GitHub login 存储

用户 Profile 使用：

    PROFILES_NAMESPACE = ["profiles"]

持久化键是 GitHub login：

    namespace = ["profiles"]
    key       = "alice"

读取链路：

    _cached_profile("alice")
        -> agent_overrides.load_profile("alice")
        -> Store.get_item(["profiles"], "alice")

写入链路：

    Dashboard PUT /profile
        -> upsert_profile(login, email, update)
        -> Store.put_item(["profiles"], login, value)

Profile 里的字段包括：

    default_model
    reasoning_effort
    default_subagent_model
    subagent_reasoning_effort
    default_repo
    create_prs
    draft_prs
    base_branch
    branch_prefix

代码位置：

- Profile 数据模型和写入：[profiles.py](../../agent/dashboard/profiles.py)
- get_agent 使用的读取包装：[agent_overrides.py](../../agent/dashboard/agent_overrides.py)
- Dashboard Profile 路由：[routes.py](../../agent/dashboard/routes.py)

用户 Profile 的字段名与团队设置不同：

| 层级 | 主模型字段 | effort 字段 |
| --- | --- | --- |
| 团队设置 | default_agent_model | default_agent_reasoning_effort |
| 用户 Profile | default_model | reasoning_effort |
| 当前线程 | agent_model_id | agent_effort |

读取后会被归一为 get_agent 内部使用的 model_id 和 effort。

### 13.3 线程 metadata：保存线程选择和运行索引

Dashboard 不把完整 RunnableConfig 当作一条长期配置保存，而是把重要字段写入 LangGraph Thread metadata。

创建 Dashboard 线程时，thread metadata 会保存：

    model
    effort
    resolved_model
    resolved_effort
    plan_mode
    repo_owner
    repo_name
    github_login
    source
    base_branch
    branch_prefix

创建和更新位置：

    agent/dashboard/thread_api.py
        _create_dashboard_thread_record()
        client.threads.create(...)
        client.threads.update(...)

一次 Run 开始前，Dashboard 再从 metadata、当前登录会话和本次 override 重建 configurable：

    _build_dashboard_configurable()
        -> thread_id
        -> source
        -> github_login
        -> user_email
        -> repo
        -> plan_mode
        -> agent_model_id / agent_effort

然后把它放入：

    params["config"]["configurable"]

因此真实链路是：

    thread metadata
        -> _build_dashboard_configurable()
        -> RunnableConfig.configurable
        -> get_agent(config)

不是所有 configurable 字段都会被永久保存。它更像一次 Run 的运行快照；长期恢复依赖 Thread metadata、LangGraph checkpoint 和其他 Store 数据。

### 13.4 模型允许列表和全局默认：代码常量

允许使用哪些模型，不在数据库里，而在：

    agent/dashboard/options.py

中定义：

    SUPPORTED_MODELS
    SUPPORTED_MODEL_IDS
    FABLE_MODEL_IDS
    DEPRECATED_MODEL_REPLACEMENTS
    DEFAULT_MODEL_ID
    DEFAULT_MODEL_EFFORT

这里决定：

- 模型 ID 是否支持；
- 每个模型支持哪些 effort；
- 是否支持图片；
- Fable 模型有哪些；
- 旧模型如何迁移；
- 没有团队配置时使用哪个硬编码默认。

默认解析顺序是：

    有效的团队配置
        -> 同 provider 的可用 fallback
        -> options.py 的 default_model_pair()

所以 options.py 是模型目录和最终兜底，不是用户配置存储。

### 13.5 Gateway：团队设置加环境变量

get_agent 中的 use_gateway 来自：

    _cached_gateway_enabled()
        -> get_effective_gateway_enabled()
        -> get_team_gateway_enabled()
        -> get_team_settings()

团队字段 gateway_enabled 是三态值：

    True  -> 强制开启
    False -> 强制关闭
    None  -> 继承环境变量

继承的环境变量是：

    LANGSMITH_GATEWAY_ENABLED

Gateway 的 API key 和地址不放在团队设置 Store，而是从部署环境读取：

    LANGSMITH_GATEWAY_API_KEY
    LANGSMITH_API_KEY_PROD
    LANGSMITH_API_KEY
    LANGSMITH_GATEWAY_BASE_URL

代码位置：

- 开关合并：[agent/utils/gateway.py](../../agent/utils/gateway.py)
- 模型构造：[agent/utils/model.py](../../agent/utils/model.py)

### 13.6 Provider 密钥和模型地址：部署环境

真正调用模型时，make_model() 还会读取 provider 配置：

    OPENAI_API_KEY
    ANTHROPIC_API_KEY
    GOOGLE_API_KEY
    FIREWORKS_API_KEY
    DEEPSEEK_API_KEY
    OPENAI_BASE_URL
    DEEPSEEK_BASE_URL

这些不是用户 Profile，也不是团队模型设置；它们属于部署环境或 Secret 管理系统。

get_agent 只决定：

    使用哪个 model_id
    使用哪个 effort
    是否走 Gateway
    传哪些 provider kwargs

make_model() 才把这些选择和环境密钥组合成具体 ChatModel。

调用链：

    model_id + effort
        -> provider_model_kwargs()
        -> _make_model_or_defer()
        -> make_model()
        -> init_chat_model()

### 13.7 进程内缓存不是持久化记录

配置读取会经过 TTL 缓存：

    agent/utils/ttl_cache.py
        _CACHE
        _LOCKS
        _REFRESH_TASKS

缓存只存在当前 Python 进程：

- 服务重启后消失；
- 不应当当作配置真相；
- 只是避免每次构图都访问 Store；
- Store 失败时，部分读取器可以短时间返回旧值。

模型对象还有单独的进程缓存：

    agent/utils/model.py
        _MODEL_CACHE

它缓存的是已经创建的 ChatModel 实例，不是模型选择配置。服务重启后同样消失。

### 13.8 用户身份映射：email、GitHub login 和 Slack ID

get_agent 需要先得到 profile_login。解析顺序是：

    configurable["github_login"]
        -> 直接使用

    如果没有 github_login：
        configurable["user_email"]
        -> user_mappings 缓存
        -> GitHub login

用户映射的持久化 namespace 是：

    ["user_mappings"]

映射记录通常包含：

    github_login
    work_email
    slack_user_id
    source
    status

代码位置：[user_mappings.py](../../agent/dashboard/user_mappings.py)。

这部分也有进程内索引缓存，但持久化真相仍然是 LangGraph Store。

### 13.9 一张表看清所有来源

| 配置项 | 运行时变量 | 真实来源 | 是否持久化 |
| --- | --- | --- | --- |
| 允许模型列表 | SUPPORTED_MODEL_IDS | options.py | 代码固定 |
| 全局默认模型 | DEFAULT_MODEL_ID | options.py | 代码固定 |
| 团队默认模型 | team_defaults | Store: ["team_settings"] / "default" | 是 |
| 用户默认模型 | profile | Store: ["profiles"] / login | 是 |
| 当前线程模型 | agent_model_id | Thread metadata + Run override | 是/临时组合 |
| Gateway 开关 | use_gateway | 团队 Store 或 LANGSMITH_GATEWAY_ENABLED | 是/环境 |
| Fable 开关 | fable_enabled | Store: ["team_settings"] / "default" | 是 |
| Provider API key | make_model 内部读取 | 部署环境变量/Secret | 环境管理 |
| 模型实例 | main_model | 进程内 _MODEL_CACHE | 否 |
| sandbox ID | backend 相关状态 | Thread metadata 和 sandbox 状态 | 是 |
| 图 state/checkpoint | Pregel 执行状态 | LangGraph checkpointer | 是 |

### 13.10 最终合并顺序

主 Agent 的模型配置最终按以下顺序合并：

    1. 团队默认模型
    2. 用户 Profile 主模型覆盖
    3. 用户 Profile 子 Agent 覆盖
    4. 当前线程 agent_model_id / agent_effort
    5. 模型 ID 和 effort 合法性校验
    6. Fable 模型门控
    7. provider_model_kwargs 参数转换
    8. Gateway 和环境变量注入
    9. make_model() 创建或复用 ChatModel

因此 get_agent 的职责不是保存配置，而是：

    从 Store、Thread metadata、configurable、代码常量和环境变量读取数据
        -> 按优先级合并
        -> 构造模型、工具、sandbox、skills 和 middleware
        -> 返回 Pregel 图

## 十四、LangGraph Store 在 dev 和生产环境到底存在哪里

先给结论：**当前 `langgraph dev` 使用的是 in-memory runtime；生产部署使用的是 Postgres runtime。**
不过本地开发版并非只在进程内存中存在：当前安装的 `langgraph_runtime_inmem` 会把 Store 默认刷到项目目录下的 `.langgraph_api/store.pckl` 和
`.langgraph_api/store.vectors.pckl`。因此它能在本地进程重启后恢复一部分数据，但它仍然不是适合多 worker、横向扩容或生产容灾的数据库。

### 14.1 当前仓库的运行时选择

仓库依赖明确选择了 CLI 的 in-memory extra：

```toml
# pyproject.toml
"langgraph-cli[inmem]>=0.4.31"
```

`langgraph.json` 只注册 graph、HTTP app 和 checkpointer TTL，没有声明自定义 `store`：

```json
{
  "graphs": {"agent": "agent.graphs.agent:traced_agent"},
  "http": {"app": "agent.webapp:app"}
}
```

所以 Store 不是在 `agent/` 里实例化的，而是由 LangGraph API runtime 注入。业务代码拿到的是统一接口：

```python
await get_client().store.get_item(namespace, key)
await get_client().store.put_item(namespace, key, value)
```

例如团队设置的读写在 [team_settings.py](../../agent/dashboard/team_settings.py) 的 `get_team_settings()` 和
`upsert_team_settings()` 中，Profile 的读写在 [profiles.py](../../agent/dashboard/profiles.py) 的
`_get_value()` 和 `upsert_profile()` 中。它们只描述 namespace、key 和 value，不负责选择底层数据库。

### 14.2 `langgraph dev` 的实际实现

本项目当前虚拟环境中，`langgraph dev` 默认参数来自 `langgraph_api/cli.py`：

```python
runtime_edition = "inmem"
__database_uri__ = ":memory:"
__redis_uri__ = "fake"
```

运行时通过 [langgraph_runtime/__init__.py](../../.venv/lib/python3.11/site-packages/langgraph_runtime/__init__.py) 读取
`LANGGRAPH_RUNTIME_EDITION`，再导入对应的 `langgraph_runtime_inmem` 后端。

Store 的实现位于依赖包（不是本仓库业务代码）：

```text
.venv/lib/python3.11/site-packages/langgraph_runtime_inmem/store.py
  DiskBackedInMemStore(InMemoryStore)
  STORE = DiskBackedInMemStore()
  Store() -> BatchedStore(STORE)
```

`DiskBackedInMemStore` 继承 LangGraph 的 `InMemoryStore`，所以读写首先发生在内存数据结构；当文件持久化未禁用时，它把数据写入：

```text
.langgraph_api/store.pckl
.langgraph_api/store.vectors.pckl
```

刷盘逻辑在 `langgraph_runtime_inmem/_persistence.py`：后台线程默认每 10 秒调用 `PersistentDict.sync()`。设置
`LANGGRAPH_DISABLE_FILE_PERSISTENCE=true` 后才会完全关闭这层本地文件持久化。

线程、运行和 checkpoint 也由 in-memory runtime 管理，但它们是另一套数据：相关实现位于
`langgraph_runtime_inmem/database.py`、`checkpoint.py` 和 `ops.py`，不能把 `.langgraph_api/store.pckl` 当成全部 LangGraph 状态。

### 14.3 生产部署为何使用数据库

生产 Dockerfile 的基础镜像是：

```dockerfile
FROM langchain/langgraph-api:0.11.2-py3.12
```

仓库的生产安装文档要求配置：

```bash
DATABASE_URI="postgres://..."
REDIS_URI="redis://..."
```

其中 PostgreSQL 保存 LangGraph 的线程、运行、checkpoint 和 Store 记录；Redis 用于队列和跨进程协调。生产 runtime 通过
`LANGGRAPH_RUNTIME_EDITION=postgres` 选择 `langgraph_runtime_postgres`（该包随生产基础镜像提供，未安装在当前本地 inmem 虚拟环境中）。

对应的选择代码仍在依赖包的 `langgraph_api/cli.py`：当 `--runtime-edition postgres` 时，它把 `DATABASE_URI` 和 `REDIS_URI` 传给服务器；
`langgraph_api/store.py:get_store()` 在没有自定义 Store 时返回 `langgraph_runtime.store.Store()`，实际的 Postgres 实现由 runtime edition 提供。

生产环境的完整部署说明见 [INSTALLATION-zh.md](../INSTALLATION-zh.md) 的“生产环境”章节。

### 14.4 是否可以换成自定义 Store

可以。LangGraph API 支持通过 `LANGGRAPH_STORE` 配置加载自定义 Store；依赖包的
`langgraph_api/store.py:collect_store_from_env()` 会按路径导入一个 `BaseStore` 实例或工厂。
但当前仓库的 `langgraph.json` 没有配置自定义 Store，因此实际使用的是 runtime 默认实现：本地 `inmem`，生产 `postgres`。

```text
业务代码: get_client().store.get_item/put_item
    -> LangGraph API 注入 store
    -> runtime edition
       ├─ dev:  langgraph_runtime_inmem -> 内存 + .langgraph_api/*.pckl
       └─ prod: langgraph_runtime_postgres -> PostgreSQL（DATABASE_URI）
```

## 本章边界

本章已经解释主工厂的装配过程，但没有深入每个 middleware 的内部实现、Deep Agents 内置工具协议和 sandbox provider 的具体实现。下一节可从 middleware 顺序开始，继续拆解一次模型调用前后发生了什么。

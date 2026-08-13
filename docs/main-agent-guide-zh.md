# Open SWE 主 Agent 全链路通俗讲解

## 这份文档解决什么问题

如果只看 `agent/server.py`，很容易以为主 Agent 就是一个 `create_deep_agent()` 调用。实际情况没这么简单：一条用户消息要经过入口鉴权、线程定位、LangGraph 调度、主 Agent 工厂、沙箱准备、提示词拼装、模型/工具循环，最后再通过 Dashboard、Slack、Linear 或 GitHub 返回结果。

这份文档按一次真实请求的时间顺序解释整条链路，重点回答：

- 主 Agent 从哪里进来？
- `get_agent()` 到底负责哪些事？
- LangGraph、Deep Agents、LangChain 分别在哪一层工作？
- 模型什么时候真正被调用？
- 文件修改、Token、线程状态和本轮 diff 存在哪里？
- 为什么同一个线程可以继续工作，而不会每次从零开始？

## 先给一张总图

主 Agent 不是一个全局单例，而是“每次执行根据线程配置装配一张图”。可以先按下面的方向读：

```text
用户消息
  |
  +--> Dashboard 命令
  +--> GitHub / Linear / Slack webhook
          |
          v
    dispatch_agent_run()
          |
          v
    LangGraph Runtime
          |
          v
    agent.graphs.agent:traced_agent
          |
          v
    agent.server:get_agent(config)
      |       |        |        |
      v       v        v        v
    身份    模型     沙箱     工具/中间件
      \       |        |        /
       \      v        v       /
        +--> create_deep_agent()
                    |
                    v
             模型 <-> 工具循环
                    |
        +-----------+------------+
        |                        |
   LangGraph 状态/事件       沙箱 Git 工作区
        |                        |
        +--> Dashboard SSE / Slack / Linear / GitHub 回复
```

系统上下文和容器边界可以看现有 [C4 总览图](open-swe-learning/architecture/premium/png/01-c4-containers.png)，主工厂装配可以看 [主 Agent 工厂时序图](open-swe-learning/architecture/premium/png/04-agent-factory-sequence.png)。对应可编辑文件分别是 [`01-c4-overview.drawio`](open-swe-learning/architecture/01-c4-overview.drawio) 和 [`04-agent-factory-sequence.drawio`](open-swe-learning/architecture/04-agent-factory-sequence.drawio)。

读图时要区分两种箭头：

- **命令箭头**：请求 Agent 开始、继续、停止或追加消息。
- **事件箭头**：把模型消息、工具调用、状态变化通过 SSE/平台回复通知出去。

## 一、三层框架先分清

| 层 | 通俗比喻 | 当前项目里的实际职责 |
|---|---|---|
| LangChain | 零件和插头标准 | 模型接口、消息格式、工具格式、Provider 适配、中间件类型。 |
| LangGraph | 流程引擎和任务账本 | Thread、Run、状态、checkpoint、图执行、流式事件和持久化。 |
| Deep Agents | 已经组装好的 Agent 工作台 | `create_deep_agent()`、文件/终端工具、子 Agent、backend、上下文管理。 |

当前项目的主 Agent 是：

```text
LangChain 提供零件
  -> Deep Agents 组装成 coding agent
  -> LangGraph 把它注册、运行、暂停、恢复
  -> Open SWE 负责入口、权限、业务工具和集成
```

源码证据：

- Deep Agents：`agent/server.py` 的 `create_deep_agent`。
- LangGraph：`langgraph.json`、`agent/dispatch.py`、`agent/graphs/agent.py`。
- LangChain：`agent/utils/model.py` 的 `init_chat_model`，以及 `agent/middleware/` 对 `AgentMiddleware`、`BaseTool`、消息类型的使用。

## `create_agent()` 和“LangGraph 图”到底有没有用

这里必须把两个问题分开回答。

### 1. 项目没有直接使用 LangChain 的 `create_agent()`

在 `agent/` 的业务代码里，没有 `from langchain.agents import create_agent`，也没有真正调用 LangChain 的 `create_agent()`。搜索结果中测试文件里出现的 `create_agent`，只是 mock `create_deep_agent()` 时使用的局部变量名，不是框架 API 调用。

主 Agent 选择的工厂是：

```python
from deepagents import create_deep_agent
```

也就是说，Open SWE **不用 LangChain 的通用 `create_agent()` 来创建主 Agent**，而是使用更适合编码 Agent 的 `create_deep_agent()`。

两者的关系可以粗略理解成：

```text
LangChain create_agent()
  通用 Agent 工厂：模型 + 工具 + middleware

Deep Agents create_deep_agent()
  编码 Agent 工厂：在通用 Agent 能力上，再加文件系统、终端、沙箱 backend、
  子 Agent、skills、长任务上下文管理等编码工作流能力
```

因此不是“没有使用 LangChain”，而是没有使用它的那一个高级工厂函数；项目仍大量使用 LangChain 的模型、消息、工具和 middleware 抽象。

### 2. 项目明确使用了 LangGraph 图

主 Agent 不是手写 `StateGraph`，但 `create_deep_agent()` 返回的就是 LangGraph 编译图。

本地已安装版本的实际函数签名显示：

```text
create_deep_agent(...) -> langgraph.graph.state.CompiledStateGraph
```

运行时验证得到的对象类型也是：

```text
<class 'langgraph.graph.state.CompiledStateGraph'>
```

项目自己的工厂函数签名把它标注为 `Pregel`：

```python
async def get_agent(config: RunnableConfig) -> Pregel:
    return create_deep_agent(...).with_config(config)
```

所以主 Agent 的实际情况是：

```text
Open SWE 配置模型、工具、backend、middleware
             |
             v
create_deep_agent(...)
             |
             v
LangGraph CompiledStateGraph / Pregel
             |
             v
LangGraph Runtime 以 thread / run / checkpoint 方式执行
```

只是**图的节点和边主要由 Deep Agents 代替项目自动生成**，因此你在 `agent/server.py` 看不到主 Agent 的 `StateGraph()`、`add_node()`、`add_edge()`。

### 3. 项目也有手写的 LangGraph `StateGraph`

`agent/scheduler.py` 是最直观的对照案例：

```python
builder = StateGraph(SchedulerState)
builder.add_node("launch", _launch)
builder.add_edge(START, "launch")
builder.add_edge("launch", END)
return builder.compile()
```

这个 scheduler 图很简单，所以项目直接手写节点和边。主 Agent 的逻辑更复杂，项目把“模型 -> 工具 -> 模型、文件工具、子 Agent、上下文管理”等通用图结构交给 Deep Agents 生成。

| 图 | 谁构建节点/边 | 典型用途 |
|---|---|---|
| 主 Agent | `create_deep_agent()` 自动构建 | 复杂编码任务、工具循环、子 Agent、sandbox。 |
| Reviewer / Analyzer / Chat | `create_deep_agent()` 自动构建 | 评审、风格分析、只读 PR 聊天。 |
| Scheduler | 项目直接写 `StateGraph` | 定时 tick 后启动新 Agent Run 或清理旧运行。 |

### 4. 一句最准确的总结

> Open SWE 不直接使用 LangChain `create_agent()`；它用 `create_deep_agent()` 生成 LangGraph 的 `CompiledStateGraph`，并用 LangGraph Runtime 负责 thread、Run、checkpoint、interrupt 和流式事件；同时在模型、消息、工具和 middleware 层大量使用 LangChain。

## 二、第一步：消息从哪里进入

主 Agent 有四类常见入口，但最终都会进入同一个 `dispatch_agent_run()`。

| 入口 | 典型场景 | 进入位置 |
|---|---|---|
| Dashboard | 用户在 Web/Electron 页面输入消息 | `agent/dashboard/thread_api.py` 命令代理。 |
| Slack | `@Open SWE`、私信或受控线程跟进 | `agent/webhooks/slack_routes.py` -> `slack.py`。 |
| Linear | Issue 评论中提及 `@openswe` | `agent/webhooks/linear_routes.py` -> `linear.py`。 |
| GitHub | Issue/PR 评论、自动评审、PR 状态事件 | `agent/webhooks/github_routes.py` -> `github.py`。 |

### Dashboard 入口

浏览器不会直接调用 `get_agent()`。它发送的是 LangGraph command，例如 `run.start`：

```text
UI useSubmitAgentMessage
  -> POST /dashboard/api/threads/{thread_id}/commands
  -> 鉴权、CSRF/来源检查、补全可信配置
  -> LangGraph client.commands()
  -> 创建 durable run
```

服务端会重新确认用户身份、仓库和模型，不会原样相信浏览器传来的 `github_login` 或 Token。Dashboard 的命令和 SSE 是两条不同链路：命令负责触发，SSE 负责观察运行过程。可以看 [Dashboard 时序图](open-swe-learning/architecture/premium/png/02-dashboard-run-sequence.png)。

### Webhook 入口

Webhook 路由先验签和过滤，然后把慢工作放进 FastAPI `BackgroundTasks`。处理器整理外部平台上下文后才调用 dispatch：

```text
GitHub / Linear / Slack webhook
  -> 验签、事件白名单、仓库门禁、提及检查
  -> 生成确定性 thread_id
  -> 拼 content_blocks + configurable
  -> dispatch_agent_run(...)
```

这样外部平台可以快速收到 `accepted`，不会因为模型调用时间长而超时重试。详情见 [`webhooks-guide-zh.md`](webhooks-guide-zh.md) 和 [Webhook 时序图](open-swe-learning/architecture/premium/png/03-webhook-sequence.png)。

## 三、第二步：`dispatch_agent_run()` 把消息变成 durable Run

统一调度代码在 [`agent/dispatch.py`](../agent/dispatch.py)。它做的不是模型推理，而是调用 LangGraph SDK 创建运行：

```python
await client.runs.create(
    thread_id,
    assistant_id="agent",
    input={"messages": [{"role": "user", "content": content}]},
    config={"configurable": configurable, "metadata": metadata},
    multitask_strategy="interrupt",
    durability="sync",
    stream_resumable=True,
)
```

几个关键参数用大白话解释：

| 参数 | 通俗含义 |
|---|---|
| `thread_id` | 这条任务属于哪个长期会话/工作区。 |
| `assistant_id="agent"` | 选择 `langgraph.json` 中注册的主 Agent 图。Reviewer 会使用另一个 assistant ID。 |
| `multitask_strategy="interrupt"` | 同一线程有新指令时，暂停当前运行并带着已有进度处理新消息。 |
| `durability="sync"` | 每一步先写 checkpoint，进程崩了能从最近一步恢复。 |
| `stream_resumable=True` | UI 晚一点连上，也能回放已经产生的事件。 |
| `prepare_run_id` | 给一次准备阶段一个稳定 ID，避免恢复时重复做同一轮准备。 |

如果配置了绝对公网 completion webhook，LangGraph 结束或失败后还会通知 Open SWE；本地回环地址会被主动跳过，避免创建 Run 时被平台拒绝。

## 四、第三步：LangGraph 找到主 Agent 图

[`langgraph.json`](../langgraph.json) 中的注册关系是：

```text
"agent": "agent.graphs.agent:traced_agent"
```

[`agent/graphs/agent.py`](../agent/graphs/agent.py) 只是导出符号，真正的入口在：

```text
traced_agent
  -> traced_graph_factory(get_agent, "open-swe-agent")
  -> await get_agent(config)
  -> 返回 Pregel graph
  -> LangGraph 执行 graph
```

`traced_graph_factory()` 的作用是给这张图设置 LangSmith tracing project。它不改变 Agent 业务逻辑，只让运行记录归到 `open-swe-agent` 项目。

## 五、第四步：`get_agent(config)` 装配主 Agent

这是整个项目最关键的装配函数，源码在 [`agent/server.py:get_agent`](../agent/server.py)。它可以理解成“开工前的装配台”。

### 5.1 先判断是不是实际执行

函数先读取 `thread_id` 并设置 `recursion_limit`：

```python
configurable = config.get("configurable") or {}
thread_id = configurable.get("thread_id")
config["recursion_limit"] = DEFAULT_RECURSION_LIMIT
```

如果只是 LangGraph 在加载/检查图，没有真正执行标记：

```python
if thread_id is None or not graph_loaded_for_execution(config):
    return create_deep_agent(system_prompt="", tools=[]).with_config(config)
```

也就是说，服务启动时不会立刻连接每个用户的沙箱、Token 和模型。只有 `thread_id` 存在且 `__is_for_execution__=True` 时，才进入完整装配。

### 5.2 解析用户和团队配置

接下来会解析：

- 当前 GitHub login；
- 团队默认模型和子 Agent 模型；
- 用户 profile 覆盖；
- Gateway 是否开启；
- Fable/模型能力开关；
- Linear 项目、来源渠道和 plan mode。

模型优先级是：

```text
团队默认
  -> 用户 profile 主模型
  -> 用户 profile 子 Agent 模型
  -> 当前 thread 的 agent_model_id / agent_effort
  -> 模型名称和 effort 能力校正
```

模型实例通过 `agent/utils/model.py` 创建。`get_agent()` 本身不发模型请求，它只是把模型对象放进图。真正的网络调用要等图运行到模型节点。

### 5.3 取线程沙箱 backend

主 Agent 要修改代码，必须有自己的工作区。工厂调用 `_get_cached_sandbox_backend(thread_id, reconnect=...)`，得到一个绑定线程的 backend。

沙箱处理逻辑最终会进入：

```text
server.ensure_sandbox_for_thread()
  -> 内存缓存命中：ping 并复用
  -> metadata 有 sandbox_id：重连
  -> 都没有：按 SANDBOX_TYPE 创建
  -> 写回 thread metadata
```

主 Agent 的 sandbox 不能在不可达时随便换新，因为旧沙箱里可能有未提交代码。这个保护由 `agent/utils/sandbox_state.py` 实现。沙箱供应商适配在 `agent/integrations/`，详见 [`integrations-guide-zh.md`](integrations-guide-zh.md)。

### 5.4 加载静态工具和动态工具

主 Agent 的工具分三类：

| 来源 | 例子 | 谁加入 |
|---|---|---|
| Deep Agents 内置 | `read_file`、`write_file`、`edit_file`、`execute`、`grep`、`task` | `create_deep_agent()` 根据 backend 自动加入。 |
| Open SWE 静态工具 | GitHub PR、Slack 回复、Linear Issue、计划和网络工具 | `server.py` 的 `static_tools` 列表。 |
| 动态集成工具 | Datadog、LangSmith、Corridor、Currents、Notion、Browser | 有凭据且通过权限检查才加载。 |

动态工具会放进 `DynamicToolMiddleware`，并用 `reserved_names` 避免外部工具覆盖 `execute` 或 `open_pull_request` 等核心工具。

### 5.5 配置 `/skills/` 和 backend

有已解析用户身份时，工厂用 `CompositeBackend`：

```text
默认路径       -> 当前线程 sandbox
/skills/ 路径  -> 用户 Store 中的 skills，只读
```

这意味着 Agent 可以读用户保存的技能文件，但不能把它们当普通工作区文件随便改掉。

### 5.6 配置主模型和子 Agent

主 Agent 使用 `main_model`；通用子 Agent 使用 `subagent_model`。两者可以使用不同模型和 effort。浏览器工具存在时，还会增加一个 browser subagent。

子 Agent 不是父 Agent 中再调用一次普通函数，而是独立编译的小图。父 Agent 只拿到子 Agent 的最终摘要；子图内部的中间模型消息不会全部冒泡到父图。

### 5.7 安装 middleware，然后调用 `create_deep_agent`

最后把模型、工具、backend、skills、subagents 和 middleware 交给 Deep Agents：

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

`system_prompt` 故意是空的，因为和线程、仓库、用户、工作目录有关的提示词要等本轮准备阶段再生成。

## 六、第五步：第一次模型调用前的准备 middleware

图编译完不代表模型已经开始回答。真正执行后，最先重要的是 `PrepareAgentRunMiddleware`。

它按下面顺序准备本轮：

```text
resolve_github_token()
  -> ensure_sandbox_for_thread()
  -> resolve_sandbox_work_dir()
  -> record_turn_checkpoint()
  -> 读取 repo/user custom instructions
  -> 更新 thread metadata 和 usage
  -> construct_system_prompt()
  -> 返回 rendered_system_prompt + work_dir
```

这里有一个“幂等”保护：middleware 根据最新用户消息和配置算 fingerprint。LangGraph 恢复同一次 invocation 时，如果 fingerprint 相同，就跳过已经完成的准备；新的用户消息会得到新的准备。

### 系统提示词里有什么

`construct_system_prompt()` 会把这些信息合起来：

- Open SWE 基础行为规则；
- 当前工作目录；
- 仓库和来源渠道；
- Git 提交/PR 署名策略；
- plan mode 状态和审批链接；
- repo-level 和 user-level 自定义指令；
- Linear 项目编号或 Dashboard 线程链接；
- 可用的 Corridor 等能力说明。

所以模型看到的不是用户的一句话，而是“系统规则 + 当前线程运行环境 + 用户任务”。

## 七、第六步：模型和工具循环

Deep Agents 编译出的图会持续做下面这件事：

```text
SystemMessage + 历史消息 + 最新用户消息
              |
              v
          主模型调用
              |
      +-------+--------+
      |                |
   最终回答         tool_calls
                       |
                       v
                    工具执行
                       |
                       v
                 ToolMessage
                       |
                       +--> 回到主模型
```

一个具体例子：

```text
用户：修复测试失败
  -> 模型调用 execute("pytest ...")
  -> 沙箱返回失败输出
  -> 模型调用 read_file("...")
  -> 模型调用 edit_file(...)
  -> 模型调用 execute("pytest ...")
  -> 测试通过
  -> 模型调用 open_pull_request(...) 或直接回复结果
```

主 Agent 不会自动替用户开 PR。是否 commit、push、创建/更新 PR，取决于模型是否调用这些已注册工具以及提示词策略。

## 八、middleware 为什么这么多

可以按功能分组理解，而不是死记名字：

| 组 | 主要 middleware | 通俗作用 |
|---|---|---|
| 开工准备 | `PrepareAgentRunMiddleware` | 沙箱、Token、prompt、checkpoint、thread metadata。 |
| 工具输入/错误 | `SanitizeToolInputsMiddleware`、`ToolErrorMiddleware` | 先整理参数，工具报错时转成模型能理解的 ToolMessage。 |
| 限制和重试 | `ModelCallLimitMiddleware`、`ToolRetryMiddleware` | 限制模型调用总量，只对 `task` 子 Agent 做有限重试。 |
| 用户协作 | `check_message_queue_before_model`、`SlackAssistantStatusMiddleware` | 运行中接收追问、更新 Slack 状态。 |
| 安全边界 | `PlanModeMiddleware`、`PullRequestCreationGuardMiddleware` | 计划模式下限制修改，阻止不合规 PR 操作。 |
| 可靠性 | `ModelFallbackMiddleware`、`ModelCallTimeoutMiddleware`、`SandboxCircuitBreakerMiddleware` | 模型故障降级、单次调用超时、沙箱重复失败时刹车。 |
| 输出收尾 | `TimeoutWrapupMiddleware`、`notify_step_limit_reached` | 超时或达到步数上限时给用户明确反馈。 |

顺序有意义。例如 `ModelCallTimeoutMiddleware` 放在最内层，让超时异常向外传给 fallback；`ToolErrorMiddleware` 和 `ToolRetryMiddleware` 的先后决定失败如何被重试和呈现。

## 九、运行中的追问怎么处理

同一线程的追问有两种情况：

```text
显式 @mention / interrupt
  -> multitask_strategy="interrupt"
  -> 暂停当前 Run，带着 checkpoint 接收新消息

普通 Dashboard 跟进（当前 Run 很忙）
  -> 写入 LangGraph Store 的 queue namespace
  -> check_message_queue_before_model
  -> 下一次模型调用前注入 HumanMessage
```

这就是为什么用户可以在 Agent 工作时继续发消息，同时又不会凭空创建一堆互相抢沙箱的运行。

## 十、状态、文件和结果分别存在哪里

这是理解项目最容易混淆的部分：不是所有东西都塞进一个数据库。

| 数据 | 存放位置 | 用途 |
|---|---|---|
| 消息历史和图状态 | LangGraph thread/checkpoint | 让 Run 能继续、恢复和回放事件。 |
| `sandbox_id`、模型、来源、PR、turn refs | thread metadata | 让下一次运行找到同一资源。 |
| 代码、分支、未提交文件 | sandbox 工作目录 | Agent 实际修改的地方。 |
| 运行中追问队列 | LangGraph Store namespace | 忙碌线程的后续消息。 |
| 用户 skills 和凭据 | Dashboard/Store 的对应 namespace | 按用户或团队隔离。 |
| 本轮代码差异 | sandbox Git ref `refs/open-swe/turns/<key>` | Dashboard 展示本轮新增/修改/删除文件。 |
| LangSmith trace | LangSmith | 调试模型、工具和 middleware 的执行过程。 |

本轮 diff 不是重放 `edit_file` 日志，而是 `turn_checkpoint.py` 在 Git 里快照，再用 `git diff` 计算，因此通过 `execute` 改动、后来又撤销的文件也能被正确识别。

状态生命周期图见 [state lifecycle](open-swe-learning/architecture/premium/png/05-state-lifecycle.png)，源码对应 `agent/utils/sandbox_state.py` 和 `agent/utils/turn_checkpoint.py`。

## 十一、结果怎么回到用户面前

### Dashboard

Dashboard 的命令响应通常只告诉前端“Run 已创建”，真正的模型消息、工具调用、生命周期和 checkpoint 事件通过 SSE 流返回。UI 的 `streamMessagesToUi` 再把事件投影成消息气泡、工具卡片、子 Agent 状态和按钮。

### Slack / Linear / GitHub

主 Agent 使用已经注册的渠道工具回复：

- Slack：`slack_thread_reply`；
- Linear：`linear_comment`；
- GitHub：`GH_TOKEN=dummy gh ...` 或 GitHub 工具。

这些渠道的 thread ID 是确定性的，所以回复会回到原来的外部对象，而不是新开一条无关会话。

### LangSmith

`traced_agent` 把运行放入 `open-swe-agent` tracing project；Slack/Linear/Dashboard 可以生成 trace URL，便于看模型调用和工具调用细节。

## 十二、一条完整的“修复测试并开 PR”链路

下面把所有步骤串起来：

```text
1. 用户在 Slack 发送：@open-swe 修复这个测试并开 PR
2. slack_routes.py 验签、识别线程、确认不是机器人消息
3. slack.py 读取线程上下文、用户和仓库，检查 GitHub 授权
4. dispatch_agent_run() 创建 assistant_id="agent" 的 durable Run
5. LangGraph 根据 langgraph.json 找到 traced_agent
6. traced_agent 调用 server.get_agent(config)
7. get_agent 解析模型、profile、Gateway、权限和动态工具
8. get_agent 获取该 thread 的 SandboxBackend，并编译 create_deep_agent
9. PrepareAgentRunMiddleware 获取 Token、确认 sandbox、保存 turn checkpoint
10. construct_system_prompt() 注入工作目录、仓库、署名、渠道和规则
11. 主模型调用 execute/read_file/edit_file/execute
12. 测试通过后，模型调用 git/PR 工具或回复结果
13. LangGraph 保存 state/checkpoint，保留可恢复事件
14. Slack 工具把最终结果回发原 thread，Dashboard SSE 同步显示过程
```

伪代码压缩如下：

```python
async def handle_user_message(message, source_context):
    thread_id, configurable = normalize_source(message, source_context)
    run = await dispatch_agent_run(
        thread_id,
        content=build_prompt_blocks(message),
        configurable=configurable,
        source=source_context.source,
    )

async def traced_agent(config):
    graph = await get_agent(config)
    with langsmith_tracing("open-swe-agent"):
        yield graph

async def get_agent(config):
    if not real_execution(config):
        return empty_deep_agent()
    identity = resolve_user_and_token(config)
    backend = cached_thread_sandbox(config.thread_id)
    model = resolve_and_build_model(config, identity)
    tools = load_static_and_authorized_dynamic_tools(config)
    return create_deep_agent(
        model=model,
        backend=backend,
        tools=tools,
        subagents=build_subagents(model, tools),
        middleware=ordered_middleware(config),
    )

async def prepare_before_first_model_call(state):
    token = resolve_github_token(config)
    sandbox = ensure_sandbox_for_thread(thread_id)
    checkpoint = snapshot_worktree(sandbox)
    prompt = construct_system_prompt(sandbox, identity, repo_rules)
    return {"rendered_system_prompt": prompt, "checkpoint": checkpoint}
```

## 十三、最容易犯的 8 个误解

1. **以为 Dashboard 直接调用 `get_agent`。** 实际是 Dashboard command -> LangGraph Run -> graph factory。
2. **以为 `get_agent` 一进来就调用模型。** 它只装配图，真正模型请求在图执行期间发生。
3. **以为 Deep Agents 不使用 LangChain。** 模型、消息、工具和 middleware 大量来自 LangChain。
4. **以为 LangGraph 只是一个 HTTP 服务。** 它还负责 graph execution、thread、Run、checkpoint 和事件流。
5. **以为 Agent 的状态都在 Python 进程内。** Agent 图本身尽量无状态，持续性在 thread、Store、checkpoint 和 sandbox。
6. **以为每个工具都是 Open SWE 自己手写的。** 文件和终端工具由 Deep Agents 自动加入，外部工具由 integrations 动态加载。
7. **以为主 Agent 会自动开 PR。** 没有隐藏的 after-agent 开 PR 钩子，模型必须按任务调用相应工具。
8. **以为 sandbox 失联就应该创建一个新的。** 主 Agent 默认拒绝静默替换，因为新沙箱没有旧的未提交代码。

## 十四、源码阅读路线

按这个顺序读，最不容易迷路：

1. [`langgraph.json`](../langgraph.json)：看图入口和 assistant ID。
2. [`agent/dispatch.py`](../agent/dispatch.py)：看外部请求如何创建 durable Run。
3. [`agent/graphs/agent.py`](../agent/graphs/agent.py)：看导出层。
4. [`agent/utils/tracing.py`](../agent/utils/tracing.py)：看 tracing 包装。
5. [`agent/server.py:get_agent`](../agent/server.py)：看主 Agent 装配。
6. [`agent/middleware/prepare_run.py`](../agent/middleware/prepare_run.py)：看每轮执行前如何准备。
7. [`agent/prompt.py`](../agent/prompt.py)：看系统提示词如何组装。
8. [`agent/utils/sandbox_state.py`](../agent/utils/sandbox_state.py)：看线程和沙箱生命周期。
9. [`agent/tools/`](../agent/tools-guide-zh.md)：看主 Agent 能做哪些业务动作。
10. [`agent/middleware/`](../agent/middleware-guide-zh.md)：看模型/工具循环中的保护措施。

## 十五、验证和学习边界

### 已静态确认

- `langgraph.json` 的 `agent -> agent.graphs.agent:traced_agent` 注册关系。
- `traced_agent -> get_agent -> create_deep_agent` 调用链。
- `dispatch_agent_run` 的 `interrupt`、`sync`、可恢复流默认值。
- 主 Agent 的模型优先级、沙箱入口、动态工具组和 middleware 顺序。
- Dashboard/Webhook 入口都会收敛到统一 dispatch。

### 已有本地测试/图验证

- 主 Agent 装配测试和 subagent 模型测试：见 `tests/agent/test_agent_assembly_context.py`、`tests/models/test_agent_subagent_models.py`。
- 主工厂、Webhook、状态生命周期图已生成并通过 Draw.io 结构校验。
- 现有课程章节已记录 Dashboard、Webhook、SSE、sandbox 和 checkpoint 的相关测试。

### 本文没有自动执行的外部调用

没有在本次讲解中启动真实远程 sandbox、调用真实模型、发送 GitHub/Slack/Linear 消息或创建 PR。这些操作需要有效凭据、测试仓库和费用边界；静态讲解不能冒充真实运行。

## 下一步学习建议

主 Agent 全链路已经串起来后，最值得继续深挖的顺序是：

1. `PrepareAgentRunMiddleware`：一次模型调用前的状态、prompt 和 checkpoint 细节。
2. `agent/tools/`：模型如何通过工具真正修改代码、评论和开 PR。
3. `agent/middleware/`：每个保护层如何影响模型/工具循环。
4. Dashboard SSE：UI 如何把 LangGraph 事件显示成消息和工具卡片。
5. Reviewer：为什么它和主 Agent 共用沙箱基础设施，但工具和权限完全不同。

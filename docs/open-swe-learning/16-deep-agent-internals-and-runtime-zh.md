# 第 16 章：`create_deep_agent` 内部如何编译运行图

## 学习目标

本章进入 `.venv` 中安装的 `deepagents` 源码，重点解释 `deepagents/graph.py:create_deep_agent` 的内部装配，不再停留在 Open SWE 的调用方。

读完后，你应该能回答：

- Deep Agents 的内置文件工具从哪里出现？
- `subagents` 为什么会变成 `task` 工具和独立子图？
- 自定义 middleware 插入到哪个位置？
- 最终为什么返回 LangGraph 的 `CompiledStateGraph`？

## 1. 内部入口

当前安装源码：

```text
.venv/lib/python3.11/site-packages/deepagents/graph.py
```

函数最后调用的是 LangChain 的 `create_agent`：

```python
return create_agent(
    model,
    system_prompt=final_system_prompt,
    tools=_tools,
    middleware=deepagent_middleware,
    response_format=response_format,
    context_schema=context_schema,
    checkpointer=checkpointer,
    store=store,
    state_schema=state_schema if state_schema is not None else DeepAgentState,
).with_config(...)
```

因此真实层次是：

```text
Open SWE get_agent
  -> deepagents.create_deep_agent
     -> langchain.agents.create_agent
        -> LangGraph CompiledStateGraph
```

Open SWE 没有自己手写 `model -> tools -> model` 循环；Deep Agents 最后把任务交给 LangChain Agent 工厂。

## 2. 第一步：解析模型和 profile

内部首先把字符串模型解析成 `BaseChatModel`：

```python
model = resolve_model(model)
_profile = _harness_profile_for_model(model, _model_spec)
```

这里的 profile 可能决定：

- 工具描述覆盖
- 额外 middleware
- 禁用工具
- 禁用或覆盖部分 middleware
- 默认 general-purpose subagent 配置

这和 Open SWE 的用户 Profile 不是同一个概念：

```text
Open SWE dashboard profile = 业务配置，决定用户/团队选择
Deep Agents harness profile  = 框架配置，决定模型能力的默认装配
```

## 3. 第二步：确定 backend

如果调用方没有传 backend，Deep Agents 使用默认 `StateBackend`：

```python
backend = backend if backend is not None else StateBackend()
```

在 Open SWE 中，`get_agent` 已经传入了 Sandbox/Composite backend，所以不会走这个默认分支。这个判断解释了为什么同一个 `create_deep_agent` API 可以用于：

- 临时内存文件工作区
- 本地 shell
- LangSmith Sandbox
- 组合 backend 和 StoreBackend

## 4. 第三步：处理用户 subagents

`subagents` 中的条目会被分成三种：

```text
SubAgent          = 描述式子 Agent，Deep Agents 负责编译
CompiledSubAgent  = 已编译 runnable，直接使用
AsyncSubAgent     = 远程/后台子 Agent，交给 AsyncSubAgentMiddleware
```

对于普通 `SubAgent`，Deep Agents 会：

1. 选择子 Agent 模型，默认继承主模型。
2. 准备子 Agent 的 `FilesystemMiddleware`。
3. 准备摘要和 patch 工具调用 middleware。
4. 处理子 Agent 自己的 skills、权限和 middleware。
5. 构造处理后的 spec。

Open SWE 的 `_general_purpose_subagent` 就属于第一种。

## 5. 第四步：自动添加 general-purpose

如果调用方没有显式提供同名 subagent，Deep Agents 会自动加入默认的 `general-purpose`：

```text
main Agent
  -> task tool
     -> general-purpose subagent
```

它继承主 Agent 的工具和 backend，但会拥有自己的 middleware 和模型配置。Open SWE 仍然显式传入 `_general_purpose_subagent(...)`，目的是覆盖默认描述、系统提示词、模型和超时边界。

这说明 `subagents=[...]` 不只是一个“可选数组”，还会影响 `task` 工具是否存在以及模型能看到哪些可委派目标。

## 6. 第五步：构造主 Agent middleware

`deepagent_middleware` 的基础顺序大致是：

```text
SkillsMiddleware（传入 skills 时）
FilesystemMiddleware
SubAgentMiddleware（存在同步 subagent 时）
SummarizationMiddleware
PatchToolCallsMiddleware
AsyncSubAgentMiddleware（存在异步 subagent 时）
```

然后插入：

```text
Harness profile extra middleware
prompt caching middleware
MemoryMiddleware（传入 memory 时）
HumanInTheLoopMiddleware（传入 interrupt_on 时）
```

最后才应用：

```text
excluded middleware
custom middleware
excluded tools
```

这个顺序有明确原因：工具排除必须发生在所有工具注入之后，否则后续 middleware 可能把被禁用工具重新放回来。

## 7. FilesystemMiddleware 做了什么？

它不是简单注册几个函数。它把 backend 转成模型可调用的文件能力，并在工具执行边界处理：

```text
模型 tool call
  -> FilesystemMiddleware
  -> 路径校验和权限判断
  -> backend.read/write/execute
  -> ToolMessage
```

默认工具包括：

```text
ls、read_file、glob、grep
write_file、edit_file、delete
execute
```

当 backend 不实现 Sandbox 执行能力时，`execute` 会返回错误消息，而不是假装命令已经运行。

这也是为什么不能只看 `tools=[]` 判断 Agent 没有工具：内置工具由 FilesystemMiddleware 注入。

## 8. SubAgentMiddleware 做了什么？

它负责两件事：

1. 根据 subagent 名称和描述生成 `task` 工具的模型可见说明。
2. 收到 `task` 工具调用后，找到对应子 Agent runnable 并运行。

调用关系是：

```text
主模型
  -> tool_call: task(name="general-purpose", ...)
  -> SubAgentMiddleware
  -> 子 Agent graph
  -> 子 Agent 最终 AIMessage/structured_response
  -> ToolMessage 返回主 Agent
  -> 主模型继续下一轮
```

父 Agent 不会自动共享子 Agent 的所有中间消息；通常只拿到子 Agent 最终摘要或结构化结果。这是隔离上下文和控制 token 成本的重要边界。

## 9. 第六步：组装最终 system prompt

内部会把调用方的 `system_prompt` 和 profile prompt 合并：

```text
caller system_prompt
    + harness profile base/suffix
    -> final_system_prompt
```

Open SWE 传入空字符串，是因为它还会在运行前由自己的 `PrepareAgentRunMiddleware` 注入动态 prompt。两层机制不要混为一谈：

```text
Deep Agents prompt assembly = 框架层静态拼接
Open SWE PrepareRun = 业务层按 thread/run 动态生成
```

## 10. 第七步：委托给 LangChain `create_agent`

到这里，Deep Agents 已经准备好：

```text
model
tools
deepagent_middleware
state_schema
context_schema
checkpointer
store
```

然后调用 LangChain `create_agent`。这一调用不是“运行模型”，而是使用 `StateGraph` builder 把模型、工具和 middleware 编译成一张 LangGraph 图。下面展开当前安装版本 `.venv/lib/python3.11/site-packages/langchain/agents/factory.py` 的实际过程。

### 10.1 `create_agent` 先合并 State Schema

LangChain 先收集每个 middleware 的 `state_schema`，再与调用方给出的 `state_schema` 合并，创建：

```python
graph = StateGraph(
    state_schema=resolved_state_schema,
    input_schema=input_schema,
    output_schema=output_schema,
    context_schema=context_schema,
)
```

其中：

```text
state_schema   = 图执行时不断读写的可变 state，例如 messages、structured_response
context_schema = 本次运行只读 context 的类型约束
input_schema   = invoke/ainvoke 接收的输入形状
output_schema  = 图结束时对外输出的形状
```

`StateGraph` 此时只是 builder，不能执行。它的节点签名是“读当前 State，返回部分 State 更新”；只有之后调用 `.compile()` 才会得到可以 `ainvoke`、`astream` 的 `CompiledStateGraph`。

### 10.2 LangChain 注册哪些节点？

最小 Agent 图至少有一个 `model` 节点；传入任意工具时，还会有 `tools` 节点：

```python
graph.add_node("model", RunnableCallable(model_node, amodel_node, trace=False))
graph.add_node("tools", tool_node)
```

middleware 的 hook 也会变成独立节点。只有真正实现对应 hook 的 middleware 才会增加节点：

```text
Middleware.abefore_agent / before_agent -> <name>.before_agent
Middleware.abefore_model / before_model -> <name>.before_model
Middleware.aafter_model / after_model   -> <name>.after_model
Middleware.aafter_agent / after_agent   -> <name>.after_agent
```

所以 Middleware 不只是 Python 装饰器。对拥有 lifecycle hook 的 Middleware，`create_agent` 会把它放进 LangGraph 拓扑，允许它更新 state 或用 `Command` 改变下一跳。

### 10.3 `model` 节点实际做什么？

异步路径 `amodel_node(state, runtime)` 会构造 `ModelRequest`：

```text
当前 state.messages
  + system_message
  + 当前可见工具 schema
  + runtime（含 context/config/store 等）
  -> ModelRequest
  -> middleware awrap_model_call 链
  -> model.ainvoke(messages)
  -> AIMessage / tool_calls
  -> Command：把消息更新写回 state
```

模型节点只负责“做一次决策”。模型返回普通 `AIMessage` 时通常准备结束；模型返回 `tool_calls` 时，条件边决定跳到 `tools`。它本身不直接调用 Python 工具函数。

### 10.4 `tools` 节点实际做什么？

`create_agent` 使用 LangChain `ToolNode` 作为 `tools` 节点。模型产生工具调用后，条件边会为每个尚未执行的 tool call 发送：

```python
Send("tools", [tool_call])
```

这让多个工具调用可以作为独立工作项调度。`ToolNode` 负责：

```text
读取 tool call
  -> 根据工具名查找工具
  -> 注入 ToolRuntime（state、context、config、store）
  -> 执行工具
  -> 生成 ToolMessage
  -> 将 ToolMessage 合并回 messages state
```

在 Deep Agents 场景中，`FilesystemMiddleware` 和 `SubAgentMiddleware` 会向这个工具体系提供 `read_file`、`execute`、`task` 等工具。因此 `tools` 是统一执行节点，并不只执行调用方在 `tools=[...]` 中显式传入的业务工具。

### 10.5 条件边如何形成 Agent Loop？

`create_agent` 先连接入口：

```text
START -> before_agent hooks -> before_model hooks -> model
```

随后从模型循环出口添加条件边。核心判断在 `_make_model_to_tools_edge()`：

```text
最后一个 AIMessage 没有 tool_calls
    -> exit_node -> after_agent hooks -> END

存在尚未得到 ToolMessage 的 tool_calls
    -> Send("tools", tool_call) -> tools

AIMessage 有 tool_calls，但 ToolMessage 已被 middleware 人工补齐
    -> 回到 model，让模型基于结果继续判断
```

`tools` 完成后，`_make_tools_to_model_edge()` 再判断：

```text
工具设置 return_direct=True
或工具刚生成 structured_response
    -> exit_node

其他普通工具结果
    -> before_model hooks -> model
```

因此最简单的拓扑是：

```text
START -> model
          ├─ 无 tool call -> END
          └─ 有 tool call -> tools -> model
```

加入 Open SWE Middleware 后，真实拓扑更接近：

```text
START
  -> PrepareAgentRunMiddleware.before_agent
  -> before_model middleware 链
  -> model
  -> after_model middleware 链
  ├─ 无待执行工具 -> after_agent middleware 链 -> END
  └─ 有待执行工具 -> tools -> before_model middleware 链 -> model
```

这里解释了两个事实：`PrepareAgentRunMiddleware` 只在一次图运行开始时准备本轮上下文；`check_message_queue_before_model` 则位于循环路径，每次下一次模型调用前都有机会把中途消息加入 state。

### 10.6 `.compile()` 做了什么？

所有节点和边都登记到 `StateGraph` 后，LangChain 调用：

```python
graph.compile(
    checkpointer=checkpointer,
    store=store,
    interrupt_before=interrupt_before,
    interrupt_after=interrupt_after,
    cache=cache,
    debug=debug,
    name=name,
)
```

`StateGraph.compile()` 会验证节点和边的目标、校验 interrupt 配置，然后创建 `CompiledStateGraph`。这个对象实现了 Runnable 接口：

```text
CompiledStateGraph
  -> invoke / ainvoke
  -> stream / astream
  -> get_state / get_state_history（有 checkpointer 时）
  -> 可被 LangGraph Runtime 调度和 checkpoint
```

编译不执行节点、不调用模型。它只是把“节点、state channel、边、持久化配置”冻结为可执行图。

### 10.7 为什么返回后还会 `.with_config(...)`？

`create_agent` 编译完成后还会绑定默认配置，例如当前 LangChain 版本把 `recursion_limit=9999` 和 LangChain tracing metadata 绑定到图。Deep Agents 再在外层绑定自己的 `ls_integration="deepagents"` metadata；Open SWE 的 `get_agent` 最后再绑定本次 Run 的 `RunnableConfig`。

```text
LangChain create_agent：CompiledStateGraph.with_config(框架默认值)
  -> Deep Agents：with_config(Deep Agents metadata)
     -> Open SWE get_agent：with_config(当前 Run config)
```

这些绑定在真正 `ainvoke` 或 LangGraph Runtime 运行图时合并。它们不执行图，也不创建 checkpoint；checkpoint 是否持久化仍由 Runtime 传入的 checkpointer 和 `thread_id` 决定。

## 11. 用最小图验证拓扑

下面代码只构造图并打印节点/边，不调用模型：

```python
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.tools import tool

@tool
def lookup() -> str:
    """Return a fixed value."""
    return "ok"

agent = create_agent(FakeListChatModel(responses=["done"]), tools=[lookup])
graph = agent.get_graph()

print(sorted(node.name for node in graph.nodes.values()))
print(sorted((edge.source, edge.target) for edge in graph.edges))
```

运行：

```bash
uv run python /tmp/inspect_create_agent.py
```

当前版本的预期核心输出是：

```text
['__end__', '__start__', 'model', 'tools']
[('__start__', 'model'), ('model', '__end__'), ('model', 'tools'), ('tools', 'model')]
```

`model -> __end__` 与 `model -> tools` 同时存在，是因为它是条件分支的两种可能，不代表每次执行都会先走工具再结束。常见误区是把图的静态边当成一次 Run 的实际轨迹；实际走哪条边由 AIMessage 的 `tool_calls` 和 middleware 返回的 `Command` 决定。

## 12. `task`、subagent 与 Skills：谁是工具，谁是子图？

这三个概念在 UI 上都可能看起来像“Agent 在做事情”，但在编译和执行层属于不同对象：

```text
subagent = 一张独立运行的子图/Runnable
task     = 主 Agent 调用 subagent 的工具接口
skills   = 按需读取的工作说明资源，不是工具也不是子图
```

### 12.1 在父图里，subagent 表现为 `task` 工具

Deep Agents 的 `SubAgentMiddleware` 接收 `subagents=[...]` 后，会先把每个普通 `SubAgent` spec 编译成独立的 Runnable：

```text
SubAgent spec
  -> create_sub_agent(...)
  -> langchain.create_agent(...)
  -> 子 Agent CompiledStateGraph
```

随后它把所有子图封装到一个 `StructuredTool`：

```python
task(
    description: str,
    subagent_type: str,
)
```

于是父 Agent 看到的并不是 `researcher_subagent_node` 这样的拓扑节点，而是一个普通的可调用工具：

```text
父 model
  -> tool_call: task(subagent_type="general-purpose", description="...")
  -> 父 tools 节点
  -> task 工具内部运行对应子图
  -> 子图最终结果转换为 ToolMessage
  -> 父 model 基于该 ToolMessage 继续判断
```

因此要同时保留两种视角：

| 视角 | subagent 是什么 |
| --- | --- |
| 父 Agent 模型 | `task` 工具的一个 `subagent_type` 选择 |
| 父图控制流 | `tools` 节点执行的一次工具调用 |
| Deep Agents 内部 | 通过 `create_agent()` 编译出的独立 Runnable/子图 |
| LangSmith tracing | 带 `ls_agent_type="subagent"` 标记的嵌套运行 |

这就是“父图从表面上仍是 `model -> tools -> model`，但 tools 内部可运行另一张完整图”的准确含义。

### 12.2 `task` 调用时，子 Agent 看到了什么？

`task` 不会把父 Agent 的全部对话历史原样复制给子 Agent。框架在执行时准备新的子图 state：

```python
subagent_state["messages"] = [HumanMessage(content=description)]
```

因此 `description` 必须包含独立完成任务所需的上下文、限制和期望输出。父图的部分 state、运行配置、callbacks、tags 会按框架规则传播，但子图的起始消息是这条明确的任务描述。

子图完成后，框架优先取其 `structured_response`；没有结构化输出时，则提取最后一个非空 `AIMessage` 文本，转换为：

```python
ToolMessage(content=child_final_result, tool_call_id=...)
```

这条 ToolMessage 才是父模型下一轮能稳定看到的子 Agent 结果。它默认不会拿到子 Agent 每一次模型调用、每一次工具调用的完整内部历史。这样既隔离上下文，也避免父会话无限增长。

当前 Open SWE 的 `general-purpose` subagent 在 `agent/server.py:_general_purpose_subagent()` 明确配置模型、共享基础提示词和独立的 `ModelCallTimeoutMiddleware`。父 middleware 不会自动包住子图模型调用，所以子图必须自己带超时等保护。

### 12.3 Skills 如何触发？

Skill 不是模型可调用的函数。`SkillsMiddleware` 在一次 Agent Run 的 `before_agent` 阶段扫描配置的 sources，例如 Open SWE 登录用户的：

```text
/skills/
  -> CompositeBackend
  -> ReadOnlyBackend
  -> StoreBackend(namespace=(SKILLS_NAMESPACE, github_login))
```

它只读取每个 skill 目录中 `SKILL.md` 的元数据：

```text
name、description、path、allowed_tools
```

这些元数据会被写入 `skills_metadata` state，并由 `SkillsMiddleware.awrap_model_call()` 追加到 system prompt。模型最初看到的是技能目录，不是所有 Skill 正文：

```text
- release-notes: 生成发布说明
  -> Read `/skills/release-notes/SKILL.md` for full instructions
```

真正的触发是模型主动选择内置文件工具：

```text
用户任务匹配 skill 描述
  -> 模型决定 read_file("/skills/release-notes/SKILL.md")
  -> FilesystemMiddleware/backend 返回完整指令
  -> 模型按该指令调用其他工具或继续回答
```

这叫渐进披露：避免每轮把所有 Skill 全文塞进 prompt，同时仍让模型能发现并读取适用的工作流。Skill 文件中的脚本也不会自动执行，模型需要明确调用 `execute` 或其他允许的工具。

### 12.4 谁选择具体工具？

严格说，**模型选择工具，Graph 只选择控制流。**

```text
模型看到：系统提示词 + messages + 当前可见工具 schema
  -> 输出 AIMessage.tool_calls，例如 read_file / execute / task

LangGraph 条件边看到：AIMessage 是否包含未执行 tool_calls
  -> 有：路由到 tools
  -> 无：路由到 END

ToolNode 看到：tool call.name
  -> 查找相同名称的工具
  -> 注入 ToolRuntime(state/context/config/store)
  -> 执行工具
  -> 写回 ToolMessage
```

Graph 不会根据“这个任务像搜索”自动选择 `web_search`，也不会根据“这是复杂问题”自动选择 subagent；这些是模型基于工具 schema、系统提示词和已有证据做出的决策。框架负责限制模型当下可见和可执行的工具集合。

在 Open SWE 中，工具可见性分为四类：

| 工具类别 | 如何进入模型可见列表 |
| --- | --- |
| 静态业务工具 | `get_agent` 的 `tools=static_tools` |
| 文件/终端/`task` | Deep Agents 的 Filesystem/SubAgent middleware 注入 |
| Skill 正文 | 模型通过 `read_file` 按需读取，不是 tool schema |
| 可选集成工具 | `DynamicToolMiddleware` 在已加载后动态加入 |

可选集成工具还有明确的两阶段机制：

```text
第 1 轮：模型调用 load_integration_tools(["notion_search"])
  -> middleware 把名称写入 state.loaded_integration_tools

第 2 轮：DynamicToolMiddleware.awrap_model_call()
  -> 把 notion_search schema 加入 request.tools
  -> 模型才能正常调用 notion_search(...)
```

即使模型绕过第一步直接调用，`awrap_tool_call()` 也会返回错误 ToolMessage。这样不会把所有可选集成的 schema、token 成本和权限面默认暴露给每一轮模型。

### 12.5 `create_agent` 最终是否只有 model 和 tools？

没有 Middleware 时，最小静态图确实只有：

```text
START -> model
          ├-> END
          └-> tools -> model
```

但 `create_agent` 会把实现 lifecycle hook 的 Middleware 编译成更多节点：

```text
before_agent -> before_model -> model -> after_model
                                      -> tools -> before_model
after_model -> after_agent -> END
```

而以下对象不是父图的独立节点：

| 对象 | 父图中的位置 |
| --- | --- |
| 普通业务工具 | `tools` 节点内部按 name 分派 |
| `task` | `tools` 节点中的一个 StructuredTool |
| subagent 图 | 被 `task` 在运行时嵌套调用 |
| Skill | metadata 进入 prompt，正文由 `read_file` 取得 |

所以正确表述是：**`create_agent` 的核心控制节点是 model 和 tools；Middleware hook 可扩展为额外图节点；工具节点内部还可以嵌套执行完整 subagent 图。**

## 13. 内部时序图

```text
create_deep_agent(...)
  -> resolve_model
  -> select harness profile
  -> choose backend
  -> normalize subagents
  -> auto-add general-purpose
  -> build FilesystemMiddleware
  -> build SubAgentMiddleware
  -> build summarization/patch/prompt cache/memory/HITL
  -> apply custom/excluded middleware and tools
  -> merge system prompt
  -> langchain.create_agent(...)
  -> CompiledStateGraph
```

## 14. 最小源码检查

查看当前安装版本的签名和实现位置：

```bash
uv run python -c \
  'import inspect; from deepagents import create_deep_agent; print(inspect.signature(create_deep_agent)); print(inspect.getsourcefile(create_deep_agent))'
```

检查最终委托关系和图构建位置：

```bash
rg -n "return create_agent\(|FilesystemMiddleware|SubAgentMiddleware" \
  .venv/lib/python3.11/site-packages/deepagents/graph.py

rg -n "StateGraph\(|add_node\(|add_conditional_edges\(|graph.compile\(" \
  .venv/lib/python3.11/site-packages/langchain/agents/factory.py
```

预期能看到 `create_agent`、`FilesystemMiddleware` 和 `SubAgentMiddleware` 的装配位置。

这些检查只读取本地安装源码，不调用模型、不创建 Run、不访问 Sandbox。

## 常见误区

1. `create_deep_agent` 自己实现完整 Agent 循环。更准确地说，它负责深度能力装配，最终循环由 LangChain `create_agent`/LangGraph 图执行。
2. `subagents` 只是父 Agent 的普通 Python 回调。实际会形成独立 runnable/graph，并通过 `task` 工具返回结果。
3. `backend` 只是保存文件的对象。它还决定文件工具是否可用，以及 `execute` 是否具备 Sandbox 执行能力。
4. 自定义 middleware 总是追加到末尾。Deep Agents 会按核心栈、profile tail 和自定义覆盖规则插入，顺序由实现保证。
5. `create_agent` 返回时模型已经执行。错，返回的是已编译图；只有 `invoke/ainvoke/stream` 或 LangGraph Run 调度后才会执行节点。
6. 读完 `deepagents/graph.py` 就能理解 Open SWE。仍然需要回到第 15 章，看业务配置、权限和 Sandbox 是如何传入的。

## 三篇文章的总链路

```text
第 14 章：API 参数表达什么能力
    -> 第 15 章：Open SWE 如何准备这些参数
        -> 第 16 章：Deep Agents 如何把参数编译成图
            -> LangGraph Runtime 执行图
```

## 小结

`create_deep_agent` 的核心价值不是“隐藏一个神秘 Agent”，而是把一组稳定能力组合成 LangGraph 图：文件系统、子 Agent、摘要、权限、记忆、人工介入和模型循环。理解内部实现后，你就能判断问题到底属于 Open SWE 装配错误、Deep Agents middleware 行为，还是 LangGraph Runtime 执行问题。

# 第 14 章：`create_deep_agent` API 与核心概念

## 学习目标

读完本章，你应该能回答：

- `create_deep_agent` 和普通 `create_agent` 的关系是什么？
- `model`、`tools`、`backend`、`subagents`、`middleware` 分别控制什么？
- 为什么它返回的是编译后的 Graph，而不是一个简单的 Python Agent 对象？
- 哪些参数是“能力配置”，哪些参数是“持久化或运行时配置”？

## 1. 它解决什么问题？

`create_deep_agent` 是 Deep Agents 提供的高级图工厂。它在 LangChain Agent 的模型-工具循环上，自动增加文件系统工具、命令执行、`task` 子 Agent、摘要和上下文管理能力。

它不是模型，也不是一次调用函数，而是：

```text
模型 + 业务工具 + backend + subagents + middleware
    -> 自动装配
    -> LangGraph CompiledStateGraph
```

Open SWE 的 `get_agent` 负责准备这些输入；`create_deep_agent` 负责把它们编译成可执行图。

## 2. 当前版本签名

当前项目通过 `.venv` 中的 `deepagents` 包使用以下签名（源码：`.venv/lib/python3.11/site-packages/deepagents/graph.py`）：

```python
create_deep_agent(
    model=None,
    tools=None,
    *,
    system_prompt=None,
    middleware=(),
    subagents=None,
    skills=None,
    memory=None,
    permissions=None,
    backend=None,
    interrupt_on=None,
    response_format=None,
    state_schema=None,
    context_schema=None,
    checkpointer=None,
    store=None,
    debug=False,
    name=None,
    cache=None,
)
```

实际项目代码不要依赖 `model=None` 的默认模型。当前 Deep Agents 已经提示该行为未来会移除，应显式传入模型。

## 3. 最重要的参数

### 3.1 `model`

主 Agent 使用的聊天模型，可以是：

```python
model="openai:gpt-5.5"
```

也可以传入已经初始化好的 `BaseChatModel`。Open SWE 选择后者：先在 `agent/server.py` 解析团队、Profile 和 thread 覆盖，再把 `main_model` 传入工厂。

模型负责决定下一步是输出文本、调用工具，还是调用 `task` 委派子 Agent。它不负责创建 Sandbox，也不负责保存 checkpoint。

### 3.2 `tools`

这是调用方额外提供的业务工具集合，例如：

```python
tools=[http_request, web_search, open_pull_request]
```

它是“额外工具”，不是完整工具列表。Deep Agents 还会根据 `backend` 自动注册：

```text
ls / read_file / write_file / edit_file
glob / grep / delete / execute / task
```

因此不要把这些内置工具重复放入 `tools`，否则容易出现同名工具和权限边界混乱。

### 3.3 `backend`

`backend` 决定文件操作和命令执行实际落在哪里：

```text
read_file -> backend.read()
write_file -> backend.write()
execute -> backend.execute()
```

如果不传，Deep Agents 会使用默认状态 backend；如果传入 Open SWE 的 Sandbox backend，工具才会操作线程对应的隔离工作区。`backend` 是能力边界，不是普通工作目录字符串。

### 3.4 `subagents`

`subagents` 是可以被主 Agent 通过 `task` 工具调用的子 Agent 描述：

```python
subagents=[
    {
        "name": "researcher",
        "description": "调查独立问题并返回证据",
        "system_prompt": "只调查，不修改仓库。",
        "model": subagent_model,
        "tools": [web_search],
    }
]
```

子 Agent 不是父 Agent 的一次普通函数调用。Deep Agents 会为它准备独立的模型、工具和 middleware，然后把最终结果以工具结果返回给父 Agent。

### 3.5 `middleware`

调用方传入的 middleware 会包住模型调用、工具调用或 Agent 生命周期。例如 Open SWE 用它实现：

- 每轮准备 Sandbox 和动态系统提示词
- 清洗工具输入
- 限制模型调用次数
- 捕获工具异常
- 消费运行中的消息队列
- 设置单次模型超时和 fallback

middleware 的顺序会改变行为，不能当成无序列表。

### 3.6 `skills`、`memory`、`permissions`

这三个参数容易混淆：

| 参数 | 作用 |
| --- | --- |
| `skills` | 提供给 Agent 的技能目录/文件来源 |
| `memory` | 由 Deep Agents 的 MemoryMiddleware 注入的记忆来源 |
| `permissions` | 文件工具的读写权限规则 |

Open SWE 的 `/skills/` 路由主要通过 `backend` 和 `SkillsMiddleware` 接入，不是把整份技能文本硬编码进 `get_agent`。

### 3.7 `checkpointer`、`store`、`context_schema`

这些参数属于状态和运行上下文边界：

| 参数 | 作用 |
| --- | --- |
| `checkpointer` | 保存图的 state/checkpoint，支持恢复执行 |
| `store` | 保存跨 thread/run 的业务数据 |
| `context_schema` | 声明本次运行只读的 context 类型 |
| `state_schema` | 扩展图内部 state 的字段结构 |

`create_deep_agent` 可以把它们传给底层 `create_agent`，但 Open SWE 的主线程 checkpoint 主要由 LangGraph Runtime 管理，不是在 `get_agent` 中手写保存逻辑。

## 4. 最小本地构图示例

下面的示例只验证“能够构造 Deep Agent 图”，不调用外部模型：

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from deepagents import create_deep_agent

model = FakeListChatModel(responses=["完成"])
agent = create_deep_agent(model=model, tools=[])

assert agent is not None
print(type(agent).__name__)
```

运行：

```bash
uv run python /tmp/deep_agent_construct.py
```

预期：输出编译图类型，而不是 `None`。这个示例不验证模型调用，也不创建 Sandbox。

常见误区：把“构图成功”理解成“Agent 已经开始调用模型”。构图只发生在 `create_deep_agent`；模型调用要等图执行时才发生。

## 5. 这篇和后两篇的边界

```text
本章：create_deep_agent 接收什么参数、每个参数表达什么能力
第 15 章：Open SWE 如何在 get_agent 中准备并传入这些参数
第 16 章：Deep Agents 内部如何把这些参数编译成 middleware 和 Graph
```

## 小结

`create_deep_agent` 是“深度 Agent 图工厂”，不是“替你选择业务配置的万能函数”。它负责把模型、工具、backend、子 Agent 和 middleware 组装成可执行图；用户身份、仓库、Sandbox 生命周期和团队模型策略仍然属于上层应用。

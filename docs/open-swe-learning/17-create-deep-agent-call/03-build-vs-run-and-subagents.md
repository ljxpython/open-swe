# 03：构图、配置绑定与真正运行

## 1. `create_deep_agent` 返回什么

当前 Deep Agents 实现的签名返回 `CompiledStateGraph`（见 `.venv/lib/python3.11/site-packages/deepagents/graph.py:268-288`）。在 Open SWE 的类型注解中，最终被视为 `Pregel`，因为 `CompiledStateGraph` 是 LangGraph 可执行图的一种实现。

构图过程可以压缩成：

```text
create_deep_agent(...)
  -> 规范化 model
  -> 添加 FilesystemMiddleware
  -> 添加 SkillsMiddleware（如果 skills 非空）
  -> 编译 SubAgentMiddleware（如果 subagents 非空）
  -> 添加 summarization / patch middleware
  -> 合并 Open SWE middleware
  -> langchain.agents.create_agent(...)
  -> CompiledStateGraph
```

`create_deep_agent` 的返回值是 Runnable，但此时没有执行用户消息，也没有发起模型网络请求。

## 2. `with_config(config)` 的真正含义

最后的：

```python
).with_config(config)
```

等价于给图绑定默认运行配置：

```python
graph = create_deep_agent(...)
bound_graph = graph.with_config(config)
return bound_graph
```

它不会：

- 创建 thread；
- 写入 Store；
- 保存 checkpoint；
- 调用模型；
- 创建第二张图。

它做的是让后续 Runtime/节点/middleware 继续拿到本次工厂已经读取过的配置，例如：

```python
{
    "configurable": {
        "thread_id": "thread-123",
        "source": "dashboard",
        "agent_model_id": "openai:gpt-5.6-terra",
    },
    "recursion_limit": DEFAULT_RECURSION_LIMIT,
}
```

要区分三个动作：

```text
create_deep_agent(...)  -> 构图
with_config(config)     -> 绑定默认配置
ainvoke/astream(...)    -> 执行图
```

checkpoint 是否持久化，取决于外层 LangGraph Runtime/checkpointer，而不是 `with_config`。

## 3. Deep Agents 如何把 `subagents` 变成 `task`

传入的是 `SubAgent` 规格：

```python
{
    "name": "general-purpose",
    "description": "...",
    "system_prompt": "...",
    "model": subagent_model,
    "middleware": [...],
}
```

Deep Agents 在 [deepagents/graph.py:827-838](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:827) 创建 `SubAgentMiddleware`。该 middleware 使用 [subagents.py:333-385](../../../.venv/lib/python3.11/site-packages/deepagents/middleware/subagents.py:333) 的 `create_sub_agent`：

1. 检查 spec 是否有 `model` 和 `tools`。
2. 解析模型。
3. 取出 spec 的 system prompt、tools 和 middleware。
4. 调用 `langchain.agents.create_agent(...)` 编译成独立 Runnable。

如果 spec 没有 `tools`，Deep Agents 在处理 spec 时继承父 Agent 的 tools（[graph.py:728](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:728)）。Open SWE 的 general-purpose spec 因此继承 `static_tools`，再获得 Deep Agents 自动添加的文件能力。

## 4. 父 Agent 什么时候选择子 Agent

父模型第一次调用时看到 `task` 工具的描述，其中包含：

```text
- general-purpose: 复杂搜索、研究和多步骤任务
- browser: 需要真实浏览器交互的任务（仅在工具可用时）
```

模型可以选择：

```python
task(
    subagent_type="general-purpose",
    description="搜索仓库中所有与 sandbox 重连相关的实现，并汇总文件和风险",
)
```

这不是 Python 代码直接调用 `_general_purpose_subagent`。该函数只在构图阶段执行一次，运行阶段是 `task` 工具根据 `subagent_type` 查找已经编译好的子图。

## 5. `task` 执行时传给子图什么

Deep Agents 的 `atask` 路径会：

1. 校验 `subagent_type` 是否存在。
2. 从父 state 拷贝允许传播的字段。
3. 把子图的 `messages` 重置为一条新的 `HumanMessage(description)`。
4. 调用 `await subagent.ainvoke(subagent_state, subagent_config)`。
5. 从子图最后一条有文本的 `AIMessage` 提取结果。
6. 包装为父图可见的 `ToolMessage`。

因此子 Agent 不是拿到父对话的完整 message 历史，而是拿到 task 的独立任务描述和必要的运行上下文。父 Agent 最终看到的是子任务结果，不是子图内部每一步的完整历史。

## 6. 子 Agent 的配置传播

LangGraph 会把父运行的 callbacks、tags、metadata 和 configurable 按运行配置规则传播给子图；Deep Agents 额外标记 `ls_agent_type="subagent"` 用于追踪。

但“配置传播”不等于“middleware 继承”。应区分：

| 会发生 | 不应假设会发生 |
| --- | --- |
| thread/configurable 等运行上下文可传播 | 父 `PrepareAgentRunMiddleware` 自动包住子图 |
| LangSmith tracing 可标记为 subagent | 父 Plan Mode 自动限制子 Agent |
| 子图可使用自己的 backend 和工具 | 父消息队列 middleware 自动注入子图 |
| 子图有自己的 timeout | 父 fallback 自动覆盖子模型调用 |

## 7. 一次委派的时序

```text
主图构建完成
  -> 用户 Run 开始
  -> 主 Agent 模型调用
  -> AIMessage(tool_calls=[task(...)])
  -> 父图 ToolNode 执行 task
  -> 选择 general-purpose 子图
  -> 子图 HumanMessage = task.description
  -> 子图：model -> read/execute/业务工具 -> model
  -> 子图返回最终摘要
  -> 摘要包装为父图 ToolMessage
  -> 主 Agent 再次调用模型
  -> 主 Agent 决定继续、验证或结束
```

子图内部可能多次调用模型，所以项目特意在 `_general_purpose_subagent` 中添加自己的 `ModelCallTimeoutMiddleware`。父图最后那份 timeout 只覆盖父图的 provider 调用。

## 8. 为什么计划模式排除 `task`

当前代码的注释指出：通用子 Agent 拥有独立工具和 middleware，不会自动继承主图的计划模式排除规则。

所以如果计划模式允许主 Agent 调用 `task`，主 Agent 可能把本应只读的工作委派给一个仍然拥有写入/执行能力的子图。项目采用简单明确的边界：计划模式禁用 `task`，避免委派绕过只读意图。

这不是说子 Agent 天生不安全，而是说安全策略必须在每张有副作用能力的图上显式安装，不能依赖父图的隐式继承。

## 9. “调用 `create_deep_agent` 后 Agent 就开始干活了吗？”

不会。准确顺序是：

```text
LangGraph Runtime 调用 traced_agent(config)
  -> traced_graph_factory 调用 get_agent(config)
  -> get_agent 调用 create_deep_agent(...)
  -> 返回绑定配置的 compiled graph
  -> Runtime 才用用户输入执行 graph
  -> before_agent / model / tools 节点开始工作
```

构图期间可能发生配置读取、模型对象初始化、sandbox backend 获取和可选工具加载；但这些不等于模型已经收到用户消息。模型网络请求发生在图执行到 model 节点时。

## 10. 最小本地验证

不触发真实 sandbox 和模型，可以只验证 `with_config` 的传播语义：

```python
from langchain_core.runnables import RunnableLambda

runnable = RunnableLambda(
    lambda value, config: {
        "value": value,
        "thread_id": config["configurable"]["thread_id"],
    }
)
bound = runnable.with_config({"configurable": {"thread_id": "thread-1"}})
result = bound.invoke("hello")
assert result["thread_id"] == "thread-1"
```

项目装配测试可以检查当前参数和 middleware 顺序：

```bash
uv run pytest -q \
  tests/agent/test_agent_assembly_context.py \
  tests/models/test_agent_subagent_models.py
```

真实执行分支需要有效的模型服务、LangGraph Runtime、thread 和 sandbox；本专题不自动触发这些外部资源。

## 本专题最终结论

```text
get_agent              = Open SWE 应用装配器
create_deep_agent      = Deep Agents 图编译器
SubAgent spec          = 子图声明
task                   = 父图调用子图的工具接口
middleware list        = 运行保护和生命周期策略
with_config(config)    = 绑定执行默认配置
ainvoke/astream        = 真正开始 Agent 工作
```


# LangGraph Pregel、RunnableConfig 与 traced graph 工厂

本文解释三个容易混在一起的概念：LangGraph 的 `Pregel` 运行时、`RunnableConfig` 运行配置，以及项目中 `traced_graph_factory()` 的入口包装逻辑。

源码依据：

- [`agent/utils/tracing.py`](../agent/utils/tracing.py)
- [`langgraph.json`](../langgraph.json)
- [`tests/reviewer/test_factory_config_isolation.py`](../tests/reviewer/test_factory_config_isolation.py)

## 先说结论

- `Pregel` 是 LangGraph 编译后图的执行运行时，负责节点调度、状态传递、并行执行、checkpoint 和中断等运行行为。
- `RunnableConfig` 是一次运行的配置字典，不是图的业务状态，也不是用户输入。
- `traced_graph_factory()` 把异步图工厂包装成 LangGraph 可加载的异步上下文管理器，让图执行期间的 LangSmith trace 使用指定项目。

## 一、Pregel 是什么

`Pregel` 借用了 Google Pregel 的 Bulk Synchronous Parallel（BSP）模型。LangGraph 把图中的节点和状态通道组织成一轮一轮的同步计算。

常见关系如下：

```text
StateGraph
    |
    +-- compile()
            |
            +-- CompiledStateGraph
                    |
                    +-- Pregel 运行时
```

当前 LangGraph 版本中，`CompiledStateGraph` 继承自 `Pregel`；`Pregel` 又实现了 LangChain 的 `Runnable` 接口。因此编译后的图可以使用 `invoke`、`ainvoke`、`stream` 和 `astream` 等方法。

### Pregel 的一轮执行

每个 super-step 大致包含三个阶段：

```text
1. Plan       选择本轮被更新通道触发的节点
2. Execute    执行节点，收集节点写入
3. Update     在步骤结束时统一提交通道更新
```

最重要的语义是：节点本轮执行时看不到其他节点本轮刚写入的数据。写入先缓冲，直到 `Update` 阶段才对下一轮可见。

例如：

```text
START
  |
  +--> load_data -----+
  |                   +--> merge_result
  +--> validate ------+
```

第一轮可以并行执行 `load_data` 和 `validate`，第二轮再执行 `merge_result`。

### Pregel 负责什么

- 根据 channel 更新调度节点；
- 并行执行当前轮次的节点；
- 在轮次边界合并状态更新；
- 管理 checkpoint、interrupt、retry 和缓存；
- 处理 `recursion_limit` 等运行上限；
- 提供同步和异步的 Runnable 调用接口。

所以，`Pregel` 不是某个业务节点，也不是用户传入的 state；它是整张图的执行引擎。

## 二、RunnableConfig 是什么

代码中使用：

```python
from langgraph.graph.state import RunnableConfig
```

这个类型实际来自 `langchain_core.runnables.config`，LangGraph 只是重新导出了它。它本质上是一个 `TypedDict`，字段都是可选的，因此可以只传需要的字段：

```python
config: RunnableConfig = {
    "recursion_limit": 25,
    "tags": ["agent"],
    "metadata": {"source": "slack"},
    "configurable": {
        "thread_id": "thread-123",
    },
}
```

它描述的是“这次运行如何执行”，不是“这次运行处理什么业务数据”。调用时通常单独传入：

```python
await graph.ainvoke(input_data, config=config)
```

### 常见字段

| 字段 | 作用 |
| --- | --- |
| `recursion_limit` | 限制图的递归/步骤数，防止无限循环 |
| `max_concurrency` | 限制并发 Runnable 调用 |
| `tags` | LangSmith trace 标签 |
| `metadata` | LangSmith trace 元数据 |
| `callbacks` | 回调处理器 |
| `run_name` | 本次运行名称 |
| `run_id` | 运行 ID |
| `configurable` | 应用自定义的运行时参数 |

`configurable` 不是固定字段，可以放项目自己的参数：

```python
config = {
    "configurable": {
        "thread_id": "thread-123",
        "github_login": "alice",
        "agent_model_id": "gpt-5",
    }
}
```

在 Open SWE 中，`thread_id`、用户身份、模型覆盖等上下文都会通过 `configurable` 传播。它和图的 state 是两条不同的数据通道：

```text
业务输入/state：这次任务处理什么数据
RunnableConfig：这次任务应该怎样执行
```

## 三、traced_graph_factory() 代码解释

源码如下：

```python
def traced_graph_factory(
    factory: Callable[[RunnableConfig], Awaitable[Pregel]],
    project_name: str,
) -> Callable[[RunnableConfig], contextlib.AbstractAsyncContextManager[Pregel]]:
    @contextlib.asynccontextmanager
    async def entrypoint(config: RunnableConfig) -> AsyncIterator[Pregel]:
        graph = await factory(config)
        with ls.tracing_context(project_name=project_name):
            yield graph

    return entrypoint
```

### 参数和返回值

```python
factory: Callable[[RunnableConfig], Awaitable[Pregel]]
```

要求传入一个异步图工厂，例如：

```python
async def get_agent(config: RunnableConfig) -> Pregel:
    ...
```

工厂接收 `RunnableConfig`，异步创建并返回一张 `Pregel` 图。

```python
project_name: str
```

这是 LangSmith 项目名，例如 `open-swe-agent` 或 `open-swe-review`。它不是图名，也不是 thread ID。

返回类型表示：调用包装函数后得到一个异步上下文管理器，进入上下文后才能拿到 `Pregel`：

```python
async with traced_agent(config) as graph:
    result = await graph.ainvoke(input_data, config=config)
```

### @asynccontextmanager 的作用

`entrypoint()` 是一个带 `yield` 的异步生成器。`@asynccontextmanager` 把它转换成异步上下文管理器：

- 进入上下文时，执行 `yield` 之前的代码；
- `yield graph` 把图交给调用方；
- 调用方退出上下文后，恢复执行 `yield` 之后的清理逻辑。

它不是直接返回 `Pregel`，而是返回一个可以管理图生命周期的入口对象。

## 四、这里的“生命周期管理”具体做了什么

这段代码的生命周期管理很窄，主要管理的是“图对象的创建时机”和“LangSmith tracing context 的作用范围”，不负责创建 sandbox、保存 checkpoint 或销毁模型。

完整流程如下：

```text
LangGraph Runtime
      |
      | 1. 调用 traced_agent(config)
      v
entrypoint(config)
      |
      | 2. await factory(config)
      v
创建 Pregel 图
      |
      | 3. 进入 ls.tracing_context(project_name=...)
      v
yield graph 给 Runtime
      |
      | 4. Runtime 在 tracing context 内执行图
      v
图执行结束/异常
      |
      | 5. 退出上下文，恢复之前的 tracing context
      v
entrypoint 结束
```

### 第一步：创建图

```python
graph = await factory(config)
```

这里调用真正的工厂函数，比如 `get_agent(config)`、`get_reviewer_agent(config)` 或 `get_analyzer(config)`。

这一步可能完成模型、工具、中间件和图结构的装配。它发生在 tracing context 之前，因此图“构建过程”本身不属于这个 `project_name` 的执行 trace。

### 第二步：建立 tracing context

```python
with ls.tracing_context(project_name=project_name):
```

LangSmith tracing context 会设置当前异步调用上下文中的项目路由。之后在这个上下文内创建的模型调用、工具调用、节点运行等 trace 会继承这个项目名。

它不会改变图的业务逻辑，也不会自动创建一条业务消息；它只是影响可观测性数据写到哪个 LangSmith project。

### 第三步：把图交给 Runtime

```python
yield graph
```

`yield` 之后函数暂时挂起，但 tracing context 仍然保持有效。LangGraph Runtime 拿到 `graph` 后，才真正调用图的 `ainvoke`、`astream` 等运行方法。

因此图执行期间仍处在：

```python
ls.tracing_context(project_name=project_name)
```

之内。

### 第四步：正常或异常退出

当 Runtime 执行结束，或者执行过程中抛出异常，异步上下文管理器都会退出 `with` 块，LangSmith 会恢复进入该上下文之前的 tracing 状态。

这避免了一个图的项目名污染后续其他图或其他请求。

需要注意：这里管理的是 Python 上下文，不是长期资源回收。它不会主动：

- 删除 sandbox；
- 删除 checkpoint；
- 关闭 LangGraph 服务；
- 关闭模型客户端；
- 清理 thread metadata。

这些资源由各自的 runtime、sandbox 和 checkpoint 组件管理。

## 五、项目中的实际入口链路

[`langgraph.json`](../langgraph.json) 注册了这些入口：

```json
{
  "agent": "agent.graphs.agent:traced_agent",
  "reviewer": "agent.graphs.reviewer:traced_reviewer_agent",
  "analyzer": "agent.graphs.analyzer:traced_analyzer",
  "chat": "agent.graphs.chat:traced_chat_agent"
}
```

以主 Agent 为例：

```text
LangGraph Server
    |
    +--> traced_agent(config)
            |
            +--> entrypoint(config)
                    |
                    +--> await get_agent(config)
                    |       |
                    |       +--> 返回 Pregel 图
                    |
                    +--> 进入 open-swe-agent tracing context
                    |
                    +--> Runtime 执行 Pregel
                    |
                    +--> 退出并恢复 tracing context
```

Reviewer 和 Analyzer 只是使用不同的图工厂或 LangSmith 项目名，它们共享同一套包装逻辑。

## 六、最容易搞混的边界

### Pregel 不是 state

`Pregel` 是执行引擎；state 是节点之间传递的业务数据。

### RunnableConfig 不是 state

`RunnableConfig` 是运行参数，通常包含 `thread_id`、模型选择、递归限制、trace metadata 等。

### tracing context 不是业务上下文

它只负责 LangSmith 可观测性路由，不负责给 Agent 注入 GitHub 仓库、用户权限或 sandbox。

### yield graph 不是函数返回

它是上下文管理器的交接点：图交给 Runtime，同时保留 tracing context；Runtime 退出后，函数才继续收尾。

### 工厂构建过程不在该 tracing context 内

当前顺序是：

```python
graph = await factory(config)
with ls.tracing_context(...):
    yield graph
```

所以只有 `yield` 期间的图执行被该上下文包住。当前代码的目标是给图运行过程做 project 路由，而不是追踪工厂内部的装配逻辑。

## 推荐阅读顺序

1. [`agent/utils/tracing.py`](../agent/utils/tracing.py)：看包装函数本身。
2. [`langgraph.json`](../langgraph.json)：看 LangGraph Runtime 实际加载哪些入口。
3. [`agent/server.py`](../agent/server.py)：看主 Agent 如何创建 Pregel 图。
4. [`agent/reviewer.py`](../agent/reviewer.py)：看 Reviewer 如何复用相同包装器。
5. [`tests/reviewer/test_factory_config_isolation.py`](../tests/reviewer/test_factory_config_isolation.py)：看工厂如何避免修改调用方配置。


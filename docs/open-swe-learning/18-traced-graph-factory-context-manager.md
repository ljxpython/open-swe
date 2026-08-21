# 第 18 章：`traced_graph_factory` 与异步上下文管理器

## 学习目标

已理解 `graph = await factory(config)` 后，本章只解释其余部分。读完应能回答：包装器返回的是什么、`yield graph` 为什么不是普通返回、trace 在何时生效与退出，以及 Runtime 应如何使用这个入口。

## 学习路线

1. 外层函数如何把两个参数固定为一个入口函数。
2. `@asynccontextmanager` 如何把异步生成器变成 `async with` 可用的对象。
3. `with ls.tracing_context(...)` 和 `yield graph` 如何共同确定 trace 的作用范围。
4. 正常退出与异常退出时发生什么。
5. 用一个不依赖 LangSmith 的最小示例验证顺序。

源码：[`agent/utils/tracing.py`](../../agent/utils/tracing.py)。实际导出点包括：

```python
traced_agent = traced_graph_factory(get_agent, AGENT_TRACING_PROJECT)
```

见 [`agent/server.py`](../../agent/server.py)。`reviewer`、`analyzer` 和 `chat` 也复用同一包装器；入口注册在 [`langgraph.json`](../../langgraph.json)。

## 1. 外层函数：生成一个“已绑定配置”的入口

```python
def traced_graph_factory(factory, project_name):
    ...
    return entrypoint
```

`traced_graph_factory(...)` 不运行图，也不返回图。它返回内部定义的 `entrypoint` 函数。

内部函数能读取外层的 `factory` 和 `project_name`，即 Python 闭包。因此下面这行执行一次后：

```python
traced_agent = traced_graph_factory(get_agent, "open-swe-agent")
```

`traced_agent` 已经记住了 `get_agent` 与 `"open-swe-agent"`；之后 Runtime 只需提供本次运行的 `config`：

```python
context_manager = traced_agent(config)
```

这里的 `context_manager` 还不是图。它必须由 `async with` 进入后才产生图。

## 2. `@asynccontextmanager`：把 `yield` 变成进入和退出协议

未加装饰器时，下面的 `entrypoint` 是异步生成器：调用它得到生成器，不能直接用于 `async with`。

```python
async def entrypoint(config):
    graph = await factory(config)
    with ls.tracing_context(project_name=project_name):
        yield graph
```

`@contextlib.asynccontextmanager` 将它转换为异步上下文管理器。固定对应关系是：

| 代码位置 | `async with` 生命周期 |
| --- | --- |
| `yield` 前 | 进入上下文时执行 |
| `yield graph` | 将 `graph` 绑定给 `as graph`，函数暂停 |
| `yield` 后 | 离开上下文时恢复执行 |

所以 Runtime 等价于这样使用入口：

```python
async with traced_agent(config) as graph:
    result = await graph.ainvoke(input_data, config=config)
```

不要写成 `graph = await traced_agent(config)`：返回值不是可等待的图，而是异步上下文管理器。

## 3. `with` 和 `yield` 确定 tracing 的范围

实际核心是这两行：

```python
with ls.tracing_context(project_name=project_name):
    yield graph
```

执行时序如下：

```text
async with traced_agent(config) as graph
    |
    |-- 进入 entrypoint
    |-- 已完成 graph = await factory(config)
    |-- 进入 ls.tracing_context("open-swe-agent")
    |-- yield graph 给 Runtime，entrypoint 在此暂停
    |       |
    |       `-- Runtime 调用 graph.ainvoke(...) / graph.astream(...)
    |
    `-- Runtime 的 with 块结束，entrypoint 恢复并退出 tracing_context
```

因此，`yield` 期间的图执行会继承指定的 LangSmith project；上下文退出后，之前的 tracing 状态会恢复。它只决定可观测性数据路由到哪个项目，**不会**改变图的业务逻辑、用户输入、`RunnableConfig`、sandbox 或 checkpoint。

特别注意顺序：工厂调用发生在 tracing context 之前。因此本包装器覆盖的是 Runtime 使用图的这段时间，并不保证 `factory(config)` 内部的装配过程归入该 project。

## 4. 为什么 `async def` 里使用普通 `with`

`async with` 用于异步上下文管理器，其进入和退出需要 `await`。`ls.tracing_context(...)` 是普通上下文管理器，进入/退出只设置并恢复当前上下文中的 trace 配置，不需要等待 I/O，所以这里正确写法是普通 `with`。

外层仍然必须是 `async def`，因为它要等待异步图工厂；`@asynccontextmanager` 则让这段“异步准备 + 同步上下文 + 交出图”的代码符合 LangGraph Runtime 所需的异步入口协议。

## 5. 退出与异常

当 `async with` 的正文正常结束时，`yield` 之后没有额外清理代码，随后 `with ls.tracing_context(...)` 退出并恢复原 trace 配置。

如果 `graph.ainvoke(...)` 抛出异常，异常会穿过 `yield` 返回 `entrypoint`；`with` 仍会退出，因此 trace 上下文不会泄漏到后续运行。该函数不捕获或转换异常，异常仍由 LangGraph Runtime 的上层处理。

它也不释放图相关的长期资源。sandbox、checkpoint、模型客户端分别由其他模块和运行时管理；这里的资源边界只有 tracing context。

## 6. 最小可运行示例

下面用标准库模拟 `tracing_context`，不需要 LangSmith 凭据，也不创建真实图。它验证的只有进入、交出对象和退出的顺序。

```python
import asyncio
from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncIterator


@contextmanager
def tracing_context(project_name: str):
    print(f"enter trace: {project_name}")
    try:
        yield
    finally:
        print(f"exit trace: {project_name}")


def traced_graph_factory(factory, project_name: str):
    @asynccontextmanager
    async def entrypoint(config: dict) -> AsyncIterator[str]:
        graph = await factory(config)
        with tracing_context(project_name):
            yield graph

    return entrypoint


async def build_graph(config: dict) -> str:
    return f"graph for {config['thread_id']}"


async def main() -> None:
    traced_agent = traced_graph_factory(build_graph, "open-swe-agent")
    async with traced_agent({"thread_id": "thread-123"}) as graph:
        print(f"run: {graph}")


asyncio.run(main())
```

运行：

```bash
uv run python path/to/example.py
```

预期输出：

```text
enter trace: open-swe-agent
run: graph for thread-123
exit trace: open-swe-agent
```

常见误区：把 `yield graph` 当作 `return graph`。前者会让函数暂停并保持 `tracing_context`；后者会立即退出 `with`，图的运行就不在该 trace 上下文内了。

## 本章边界与下一步

本章不展开 `factory` 如何创建 `Pregel`，也不展开 LangSmith 如何上传 trace。前者读第 2-1 章，后者可对照根目录的 [Pregel、RunnableConfig 与 tracing 工厂补充讲义](../langgraph-pregel-runnableconfig-tracing-guide-zh.md)。

# 静态图、动态分支与动态图工厂：怎样选择 LangGraph 的装配方式

## 学习路线

1. 先区分“图拓扑变化”和“运行参数变化”。
2. 看静态图如何通过条件边处理不同任务。
3. 看什么时候需要按请求创建图实例。
4. 用 Open SWE 的 `get_agent()` 和 `ai-agent-platform` 的目标形态做选择。
5. 用一张决策表落到具体开发工作。

## 1. 先把三个概念分开

这三个概念经常被混成“静态图和动态图”，其实不是一回事：

| 概念 | 图的节点/边是否在发布时确定 | 本次 Run 能否不同 | 例子 |
| --- | --- | --- | --- |
| 静态图 | 是 | 能 | 同一张 Agent 图，按项目选择模型和工具 |
| 静态图中的动态分支 | 是 | 能 | Router 根据意图走检索、审批或直接回答路径 |
| 动态图工厂 | 否，或每次重新装配实例 | 能 | 根据 thread 的 Sandbox、仓库和集成组装 Coding Agent |

所以“模型不同、Prompt 不同、工具权限不同”不等于必须动态建图。这些大多是运行参数或策略变化。

```text
图拓扑：有哪些节点、边、状态字段和中断点
运行策略：模型、Prompt、工具白名单、权限、限额
运行资源：thread、checkpoint、Sandbox、知识库连接
```

## 2. 静态图：拓扑固定，运行事实动态

静态图是指部署时就能列出节点和边。它并不要求每次执行走同一条路径，也不要求模型、Prompt 和工具固定。

下面是一个不调用模型的最小 LangGraph 路由示例。图只有 `classify`、`search`、`answer` 三个节点；每次运行由 `route` 决定走哪条边。

```python
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    question: str
    route: Literal["search", "answer"]
    result: str


def classify(state: State) -> dict[str, str]:
    route = "search" if "资料" in state["question"] else "answer"
    return {"route": route}


def search(_: State) -> dict[str, str]:
    return {"result": "检索到的资料摘要"}


def answer(_: State) -> dict[str, str]:
    return {"result": "直接回答"}


builder = StateGraph(State)
builder.add_node("classify", classify)
builder.add_node("search", search)
builder.add_node("answer", answer)
builder.add_edge(START, "classify")
builder.add_conditional_edges("classify", lambda state: state["route"])
builder.add_edge("search", END)
builder.add_edge("answer", END)
graph = builder.compile()

assert graph.invoke({"question": "给我找资料"})["result"] == "检索到的资料摘要"
assert graph.invoke({"question": "解释 thread"})["result"] == "直接回答"
```

运行方式：将示例保存为临时 Python 文件后执行 `uv run python <文件名>`。预期是两个断言都通过，不会访问模型或网络。常见误区是把条件边误判为动态图；这里两条边在 `compile()` 前已经确定，因此仍是静态图。

适合静态图的典型变化：

```text
同一 Agent，用户改模型             -> RuntimeOptions
同一 Agent，项目改系统提示词        -> RuntimeOptions
同一 Agent，角色可用工具不同        -> Capability Policy
同一 Agent，切换知识库             -> 受控资源选择
同一 Agent，任务意图不同            -> 条件边
```

## 3. 静态图为什么通常更适合生产平台

静态图的优势不在“写起来快”，而在于它把可变性限制在可审计的位置：

| 维度 | 静态图 + 动态 Context/Policy | 每次动态装配图 |
| --- | --- | --- |
| 测试 | 一套拓扑覆盖多组参数 | 要覆盖每种装配组合 |
| checkpoint 恢复 | 图版本明确 | 恢复时必须重建同一装配结果 |
| 观测 | 易比较同一图的模型/工具差异 | 需要额外记录每次图结构 |
| 灰度/回滚 | 切换版本化 Graph | 必须复现旧工厂的全部依赖 |
| 权限 | Policy 统一过滤工具 | 容易散落在多个工厂分支 |

对 `ai-agent-platform`，默认应是：一个 Assistant 对应一个版本化静态图，运行期由 `RuntimeContext`、`RuntimeOptions` 和 Middleware 决定它代表谁、允许什么、使用什么模型和资源。

```text
research_agent_v1
  固定：检索 -> 归纳 -> 回答 的拓扑
  变化：项目知识库、模型、Prompt、只读工具、Token 上限

sql_agent_v1
  固定：生成查询 -> 校验 -> 执行只读查询 -> 解释结果 的拓扑
  变化：数据源、schema、角色权限、模型
```

## 4. 动态图工厂：什么时候确实需要

动态工厂不是更高级的通用方案。它适合“图实例的资源或组件集合必须按 Run 装配，且不能仅靠条件边、Context 或策略解决”的情况。

Open SWE 的 [`get_agent`](../../agent/server.py:953) 是典型例子：它从 `RunnableConfig` 取 `thread_id`，再按线程解析 GitHub 身份、模型配置、Sandbox backend、仓库指令、集成工具和 Middleware，最后调用 `create_deep_agent()` 返回一张可执行图。

但还要看清一层：Open SWE 的 `get_agent()` 是**动态装配 Agent 实例**，并不意味着每次都创建完全不同的业务流程图。它的核心 Deep Agent 控制循环仍然相近，变化主要是模型、工具、backend 和运行资源。

下面是动态工厂的结构示意。它的变化点是每个 thread 必须先获得自己的 Workspace；若只是换模型或工具权限，不应写成这样的工厂。

```python
async def coding_agent_factory(config: RunnableConfig) -> Pregel:
    thread_id = config["configurable"]["thread_id"]
    workspace = await workspace_manager.connect_for_thread(thread_id)
    policy = await policy_service.resolve_for_thread(thread_id)

    return create_deep_agent(
        model=policy.model,
        tools=policy.allowed_tools,
        backend=workspace,
        middleware=[PrepareRunMiddleware(thread_id=thread_id)],
    )
```

这是架构示意，不是独立可运行代码：`workspace_manager`、`policy_service` 和 `PrepareRunMiddleware` 必须由目标项目实现并负责权限、幂等和恢复。它说明的是“资源装配确实决定 Agent 实例”这一使用场景，而不是给所有 Assistant 建工厂的模板。

适合使用工厂的情况：

```text
每个 thread 都必须连接自己的 Workspace/Sandbox
不同 Agent 类型的节点集合和中断点本质不同
按安装的插件决定是否挂载子图
图的 backend、工具实现或状态 schema 无法在运行期安全替换
```

不适合使用工厂的情况：

```text
只是用户选择了模型
只是项目切换 Prompt
只是角色变化导致工具权限不同
只是路由到已知的固定分支
只是切换一个已有的知识库/MCP 连接
```

## 5. `configurable`、`RuntimeContext` 与图选择的关系

`RunnableConfig["configurable"]` 是 LangGraph/LangChain 的通用运行配置通道，适合携带 `thread_id`、checkpoint 路由键和经过服务端校验的运行选项。它的“通用”来自类型宽松，不代表它是可信身份边界。

`RuntimeContext` 应保留给已验证的业务事实，例如 actor、tenant、project、role、permissions 和解析后的策略。即使调用链暂时只能通过 `configurable` 传输这些值，Runtime 也应在入口校验并重建类型化 Context，不能信任浏览器原样传来的字典。

```text
浏览器请求
  -> Platform API 鉴权
  -> 生成 RuntimeContext（可信身份）
  -> 生成 RuntimeOptions（白名单选项）
  -> 生成 RunnableConfig.configurable（thread/checkpoint 路由）
  -> 固定 Graph 执行
```

## 6. 针对本次迁移的选择

| 场景 | 推荐方式 | 原因 |
| --- | --- | --- |
| 普通生产 Assistant | 静态图 + Context/Policy | 版本、恢复、权限和观测最简单 |
| 同一 Assistant 的不同任务路径 | 静态图 + 条件边 | 运行路径可变，拓扑仍可测试 |
| 未来 Coding Agent | 专用静态 Graph 或受限工厂 | Sandbox/仓库能力独立，不污染通用 Runtime |
| 内部 `runtime-web` | 直连调试，不参与生产图选择 | 它不是线上权限或协议的依据 |

本次迁移不需要兼容旧服务，因此不必为旧接口或旧前端动态拼一张“万能图”。先交付版本化静态 Graph、统一 Run API、可恢复 SSE 和明确的 Capability Policy；只有真实需求证明拓扑必须变时，再为那个专用 Agent 引入工厂。

## 7. 最终决策表

先按下面顺序问问题：

```text
节点和边是否真的不同？
  否 -> 静态图
  是 -> 是否可预先登记为有限个版本化 Graph？
           是 -> 选择不同静态 Graph
           否 -> 是否因 thread 资源必须在运行期装配？
                    是 -> 使用受限动态工厂，并记录装配版本
                    否 -> 重新检查是否只是 Policy/条件边问题
```

不要用“动态图更通用”作为默认理由。生产系统真正需要的不是最大自由度，而是能恢复、能测试、能审计，并且能解释某个 Run 当时究竟执行了什么。

## 验证记录

已执行 `uv run python -c "from langgraph.graph import StateGraph; from langgraph.graph.state import RunnableConfig"`，确认当前项目环境可以导入本讲义示例依赖；没有调用模型、网络或 Sandbox。

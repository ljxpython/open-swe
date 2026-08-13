# `agent/runtime` 通俗说明

## 先说结论

`agent/runtime/` 是 Open SWE 的“小型运行时公共入口层”。它只做三件事：

1. 集中放 Agent 运行时默认值；
2. 判断当前调用是不是 LangGraph 真正执行图的阶段；
3. 给主 Agent、Reviewer、Analyzer 和聊天 Agent 提供统一的沙箱入口。

它不是一个单独启动的服务，也不是另一套 Agent 框架。真正的图构建在 `agent/server.py`、`agent/reviewer.py`、`agent/analyzer.py` 和 `agent/chat.py`；真正的沙箱实现主要在 `agent/utils/sandbox.py`、`agent/utils/sandbox_state.py` 以及 `agent/integrations/`。`agent/runtime` 只是把这些能力以稳定、短小的接口暴露给上层。

```text
server / reviewer / analyzer / chat / tools
                  |
                  v
             agent/runtime
       常量 + 执行态判断 + 沙箱入口
                  |
        +---------+----------+
        |                    |
   LangGraph 配置       server.py 的沙箱实现
                         utils / integrations
```

## 目录文件总览

| 文件 | 通俗作用 | 怎么被使用 |
|---|---|---|
| [`__init__.py`](../agent/runtime/__init__.py) | 对外统一出口，把其他文件里的常量和函数集中导出。 | 上层可以直接 `from agent.runtime import ...`，不用记住每个实现文件的位置。 |
| [`constants.py`](../agent/runtime/constants.py) | 集中保存模型默认值、最大 Token 数和 LangGraph 递归限制。 | `server.py`、`reviewer.py`、`analyzer.py`、`chat.py` 在创建图和模型时读取这些值。 |
| [`execution.py`](../agent/runtime/execution.py) | 判断当前配置是不是“正在真正执行图”，而不是 LangGraph 为了加载图而做的预览/构建调用。 | 各图的工厂函数先调用 `graph_loaded_for_execution()`；如果不是执行阶段，就返回一个不连接沙箱、不初始化模型的轻量图。 |
| [`sandbox.py`](../agent/runtime/sandbox.py) | 对 `agent.server` 中的沙箱函数做一层薄封装，提供统一的“按线程确保沙箱、取缓存 backend、配置 Git 身份”入口。 | `server.py`、`reviewer.py`、`analyzer.py` 和 `fetch_review_diff` 调用这里，避免各自直接依赖 server 内部实现。 |

## 一、`constants.py`：运行参数的总开关

文件里目前有 4 个公开常量：

| 常量 | 当前含义 | 用途 |
|---|---|---|
| `DEFAULT_LLM_MODEL_ID` | 默认模型 ID，来源是 Dashboard 的 `DEFAULT_MODEL_ID`。 | 没有按线程、用户或团队覆盖模型时，Analyzer 等图使用它。 |
| `DEFAULT_LLM_MAX_TOKENS` | `64_000`。 | 创建模型时限制一次回答最多使用多少输出 Token。不同 provider 的参数由 `utils/model.py` 再转换。 |
| `DEFAULT_RECURSION_LIMIT` | `9_999`。 | LangGraph 一次运行最多允许多少步递归，防止图无限循环。 |
| `MODEL_CALL_RECURSION_LIMIT` | `5_000`。 | Middleware 对模型调用次数使用的更低上限；它比整张图的上限更早刹车。 |

这里的默认值只负责“兜底”。实际运行时仍可能被 `configurable`、用户 profile、团队设置或专用 reviewer 配置覆盖。

### 谁在用

- `server.py`：创建主编码 Agent、设置 `recursion_limit`、给模型传 `max_tokens`。
- `reviewer.py`：创建只读 Reviewer，并设置 reviewer 的模型调用上限。
- `analyzer.py`：创建评审风格分析器。
- `chat.py`：创建只读 PR 聊天 Agent。

## 二、`execution.py`：区分“加载图”和“执行图”

公开函数是：

```python
graph_loaded_for_execution(config) -> bool
```

它检查 `config["configurable"]["__is_for_execution__"]` 是否为真。

通俗说，LangGraph 有时只是想“看看这张图长什么样、把入口加载出来”，有时才是真的要跑一次任务。加载阶段不需要：

- 创建远程沙箱；
- 恢复线程工作区；
- 连接模型供应商；
- 读取用户 Token；
- 做昂贵的仓库准备。

所以各图工厂一般按这个模式写：

```python
thread_id = configurable.get("thread_id")
if thread_id is None or not graph_loaded_for_execution(config):
    return create_deep_agent(system_prompt="", tools=[]).with_config(config)
```

这样做的好处是：开发工具、LangGraph Studio 或服务启动时可以安全加载图；只有真正收到运行请求时，才初始化模型和沙箱。

### 使用它的模块

| 模块 | 具体行为 |
|---|---|
| `agent/server.py` | 没有真实 `thread_id` 或不是执行阶段，返回空工具图；真正执行时才选择模型和沙箱。 |
| `agent/reviewer.py` | 评审图加载阶段不创建沙箱；真正评审时才恢复/创建 reviewer 沙箱。 |
| `agent/analyzer.py` | 加载阶段不读取分析器 Skill、不连接沙箱；执行阶段才准备工作目录和虚拟 Skill。 |
| `agent/chat.py` | PR 聊天 Agent 是只读的；加载阶段返回空图，执行阶段才创建聊天模型。 |

## 三、`sandbox.py`：沙箱的统一门面

这个文件只有 3 个主要函数，但很关键，因为上层模块不需要知道 server 内部怎么管理沙箱。

### `ensure_sandbox_for_thread()`

作用：根据 `thread_id` 确保这个线程有可用沙箱。

它本身不创建沙箱，而是延迟导入并转调 `agent.server.ensure_sandbox_for_thread()`。这样做有两个实际好处：

- 避免 `runtime` 和 `server` 在模块导入时互相循环引用；
- 上层统一从 `agent.runtime` 调用，未来 server 内部怎么调整不必改所有调用方。

可传参数包括：

- `thread_id`：把沙箱绑定到哪条 Agent 线程；
- `github_proxy_token`：给沙箱里的 GitHub 代理使用的 Token；
- `github_proxy_repositories`：代理允许访问的仓库范围；
- `repo`：本次工作关联的仓库；
- `allow_replacement`：是否允许沙箱不可达时替换。主 Agent 通常不允许，Reviewer 因为每次会重新 checkout，可以允许。

### `get_cached_sandbox_backend()`

作用：取当前进程中已经缓存的线程沙箱 backend。

缓存命中时可以直接使用，减少重复连接；缓存没有时，可以传 `reconnect` 回调，让调用方定义如何从线程元数据恢复沙箱。

主要调用者：

- `server.py`：主 Agent 的工具 backend；
- `reviewer.py`：Reviewer 的只读/代码检查 backend；
- `analyzer.py`：分析器的默认 backend；
- `agent/tools/fetch_review_diff.py`：把当前 reviewer diff 写入沙箱。

### `configure_git_identity()`

作用：给沙箱中的 Git 设置 bot 的用户名和邮箱。

主 Agent 每次使用或恢复沙箱时都可能重新设置它，因为远程沙箱的全局 Git 配置可能丢失。这样 Agent 提交代码时，GitHub 和 Vercel 才能识别提交作者。

## 四、`__init__.py`：为什么要有统一导出

`__init__.py` 把下面这些名称集中暴露：

```python
from agent.runtime import (
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_RECURSION_LIMIT,
    MODEL_CALL_RECURSION_LIMIT,
    graph_loaded_for_execution,
    ensure_sandbox_for_thread,
    get_cached_sandbox_backend,
    configure_git_identity,
)
```

因此 `reviewer.py`、`analyzer.py`、`chat.py` 可以从一个地方拿完运行时依赖。它不是注册插件，也没有启动逻辑；只是 Python 包的公共 API 清单。

## 关键调用链

### 1. 主 Agent 创建

```text
LangGraph 调用 agent.server:get_agent(config)
        |
        +--> constants.DEFAULT_RECURSION_LIMIT
        +--> execution.graph_loaded_for_execution(config)
        |       |
        |       +--> 非执行阶段：返回轻量空图
        |       |
        |       +--> 执行阶段：继续初始化
        |
        +--> runtime.sandbox.ensure_sandbox_for_thread()
        |       |
        |       +--> server.ensure_sandbox_for_thread()
        |       +--> utils/sandbox_state 管理缓存、元数据和生命周期
        |
        +--> utils.model.make_model(... max_tokens=DEFAULT_LLM_MAX_TOKENS)
        +--> create_deep_agent(...)
```

### 2. Reviewer 创建和获取 PR diff

```text
agent.reviewer:get_reviewer_agent(config)
  -> graph_loaded_for_execution()
  -> runtime.sandbox.get_cached_sandbox_backend()
  -> reviewer 的 reconnect 回调
  -> ensure_sandbox_for_thread(... allow_replacement=True)
  -> reviewer 准备 PR checkout
  -> fetch_review_diff 工具再次取 cached backend
  -> 在沙箱中物化当前 diff
```

Reviewer 的沙箱可以允许替换，是因为它每次都会重新把 PR checkout 到正确的 commit；主 Agent 的沙箱保存未提交工作，不能随便换空沙箱。这个差异由 `server.py` 的实现决定，`runtime/sandbox.py` 只是把参数透传过去。

### 3. Analyzer 创建

```text
agent.analyzer:get_analyzer(config)
  -> graph_loaded_for_execution()
  -> ensure_sandbox_for_thread()
  -> resolve_sandbox_work_dir()
  -> 配置 GitHub proxy
  -> 读取 /skills/ 虚拟 Skill
  -> make_model(DEFAULT_LLM_MODEL_ID, max_tokens=...)
```

### 4. PR Chat 创建

```text
agent.chat:get_chat_agent(config)
  -> 设置 DEFAULT_RECURSION_LIMIT
  -> graph_loaded_for_execution()
  -> 非执行阶段返回无工具轻量图
  -> 执行阶段解析聊天模型
  -> make_model(... max_tokens=DEFAULT_LLM_MAX_TOKENS)
  -> 创建只读 PR chat Agent
```

PR Chat 没有沙箱，它的 PR 上下文通过 `configurable` 传入；因此它只使用 `constants.py` 和 `execution.py`，不需要 `runtime/sandbox.py`。

## 它和 `agent/utils` 的区别

| 目录 | 主要定位 | 例子 |
|---|---|---|
| `agent/runtime/` | 给 Agent 图和工具提供少量、稳定的运行时入口。 | 默认递归上限、执行态判断、线程沙箱入口。 |
| `agent/utils/` | 具体基础设施能力的实现层，内容更广。 | 模型构造、沙箱 provider、GitHub Token、线程操作、URL 安全、checkpoint。 |
| `agent/integrations/` | 各种外部供应商的具体适配器。 | LangSmith、Daytona、Modal、E2B 等沙箱创建器。 |
| `agent/server.py` | 主 Agent 的组装和沙箱生命周期真正实现。 | `get_agent()`、`ensure_sandbox_for_thread()`、middleware 和工具列表。 |

最容易搞混的是 `runtime/sandbox.py` 和 `utils/sandbox_state.py`：

- `runtime/sandbox.py`：短小的公共门面，负责把调用转到 server；
- `utils/sandbox_state.py`：真正管理线程缓存、恢复、代理 backend 和不可达保护；
- `utils/sandbox.py`：真正按 provider 创建或重连底层沙箱；
- `integrations/*`：每家沙箱供应商的具体 SDK 适配。

## 常见问题

### 为什么不直接从 `server.py` 导入沙箱函数？

因为 reviewer、analyzer 和工具都需要沙箱，但直接依赖 server 会让模块关系变重，甚至形成循环导入。`runtime/sandbox.py` 给它们一个稳定、窄的入口，内部通过延迟导入避开这个问题。

### `DEFAULT_RECURSION_LIMIT` 是模型调用次数吗？

不是。它是整张 LangGraph 图允许的递归/步骤上限；模型调用次数还有 `MODEL_CALL_RECURSION_LIMIT` 这个更低的 middleware 限制。

### `graph_loaded_for_execution()` 为什么不检查 `thread_id` 就够了？

实际调用方通常两个条件一起检查：没有 `thread_id`，或者配置不是执行阶段，都会返回轻量图。这样既避免没有线程上下文时初始化资源，也避免 LangGraph 仅加载图时做副作用操作。

### 这里有没有数据库？

没有。`runtime` 本身只读常量和转发函数。沙箱、线程元数据、Token、运行状态的缓存/持久化分别由 `server.py`、`agent/utils`、LangGraph Store、外部 provider 或 Dashboard 存储层负责。

## 推荐阅读顺序

1. [`agent/runtime/__init__.py`](../agent/runtime/__init__.py)：看对外 API。
2. [`agent/runtime/constants.py`](../agent/runtime/constants.py)：看默认运行参数。
3. [`agent/runtime/execution.py`](../agent/runtime/execution.py)：看为什么加载图时不初始化资源。
4. [`agent/runtime/sandbox.py`](../agent/runtime/sandbox.py)：看统一沙箱入口。
5. [`agent/server.py`](../agent/server.py)：继续追真正的沙箱生命周期和主 Agent 组装。
6. [`agent/reviewer.py`](../agent/reviewer.py)、[`agent/analyzer.py`](../agent/analyzer.py)、[`agent/chat.py`](../agent/chat.py)：对比三个图如何使用同一套 runtime 入口。


# 第 12 章：LangGraph SDK 与 Open SWE 的使用方式

## 学习目标

读完本章，你应该能回答：

1. `langgraph`、`langgraph_sdk`、`@langchain/langgraph-sdk` 和 `@langchain/react` 分别负责什么。
2. Open SWE 的 Python 后端和 React 前端各自通过哪些 SDK 接口访问 LangGraph。
3. `threads`、`runs`、`store`、`crons`、`assistants` 什么时候使用。
4. 为什么 Open SWE 没有直接把所有请求交给 `runs.create`，而是增加了 durable dispatch、Dashboard 代理和消息队列。

## 先看课程大纲

| 章节 | 学习目标 |
| --- | --- |
| 1. SDK 分层 | 区分图构建库、服务端客户端、浏览器客户端和 React 流式层 |
| 2. Python SDK | 学会创建客户端并操作 thread、run、Store、cron |
| 3. Open SWE 后端改造 | 看清 webhook、Dashboard、调度器如何接入 SDK |
| 4. Open SWE 前端改造 | 看清 `Client`、`StreamProvider`、SSE、消息队列的关系 |
| 5. 开发常用接口 | 用最小代码覆盖日常开发的主要 API 和常见误区 |

---

## 一、LangGraph 相关 SDK 到底有几层

“LangGraph SDK”在项目里不是一个单独的包，而是几层能力叠在一起：

| 包/模块 | 类型 | 主要职责 | Open SWE 是否使用 |
| --- | --- | --- | --- |
| `langgraph` | Python 图框架 | `StateGraph`、`Pregel`、Runtime、checkpoint、Store 抽象 | 是，Agent 工厂和 middleware 使用 |
| `langgraph_sdk` | Python API 客户端 | 从 Python 调用 LangGraph Server 的 threads/runs/store/crons 等接口 | 是，后端大量使用 |
| `@langchain/langgraph-sdk` | TypeScript API/协议客户端 | 浏览器端 HTTP、命令、SSE transport、状态读取 | 是，Agents 和 Review Chat 使用 |
| `@langchain/react` | React 集成层 | `StreamProvider`、`useStreamContext`、消息/工具/生命周期投影 | 是，聊天 UI 使用 |
| `langgraph-cli[inmem]` | CLI/本地运行时 | `langgraph dev`，启动本地 LangGraph API | 是，开发依赖 |
| `langsmith` | 追踪与观测 SDK | tracing、项目、运行追踪 | 是，但它不是 LangGraph API 客户端 |

### 1.1 `langgraph` 不是“远程调用 SDK”

`langgraph` 负责定义和执行图。例如主工厂导入：

    from langgraph.graph.state import RunnableConfig
    from langgraph.pregel import Pregel

这些类型描述当前进程里的图和运行配置；它们不会替代 `langgraph_sdk` 去调用远程线程或运行。

源码入口见 [agent/server.py](../../agent/server.py:20)。

### 1.2 `langgraph_sdk` 是后端的控制面客户端

当前 Python SDK 版本是 `0.4.2`。`get_client()` 返回异步 `LangGraphClient`，顶层对象暴露：

    client.assistants
    client.threads
    client.runs
    client.crons
    client.store

它的实现位于当前虚拟环境：

    .venv/lib/python3.11/site-packages/langgraph_sdk/_async/client.py

也有同步入口 `get_sync_client()`，但 Open SWE 按照异步约束统一使用 `get_client()`。

项目实际导入的 Python SDK 模块只有四类：

| 导入 | 用途 | 代表位置 |
| --- | --- | --- |
| `langgraph_sdk.get_client` | 创建异步 API 客户端 | [agent/dispatch.py](../../agent/dispatch.py:30) |
| `langgraph_sdk.client.LangGraphClient` | 给 helper、函数参数和返回值做类型标注 | [agent/dispatch.py](../../agent/dispatch.py:31) |
| `langgraph_sdk.schema` | `Run`、`Thread`、`ThreadState`、`Config__ 等结构类型 | [agent/utils/json_types.py](../../agent/utils/json_types.py:8) |
| `langgraph_sdk.errors.NotFoundError` | 区分 LangGraph 的 404 与普通异常 | [agent/review/findings.py](../../agent/review/findings.py:26) |

项目没有在业务代码中自行实现 HTTP 请求协议，也没有复制一套 thread/run 客户端；这些都由 SDK 的子客户端提供。

### 1.3 浏览器端是两个包配合

前端依赖定义在 [ui/package.json](../../ui/package.json:20)：

    {
      "@langchain/langgraph-sdk": "^1.9.21",
      "@langchain/react": "^1.0.22"
    }

锁文件当前解析到 `@langchain/langgraph-sdk 1.9.29` 和 `@langchain/react 1.0.22`。

- `@langchain/langgraph-sdk` 负责请求、命令和 SSE transport。
- `@langchain/react` 负责把 transport 交给 React 组件，并投影出 `stream.messages`、`stream.toolCalls`、`stream.isLoading` 等状态。

不要把 `StreamProvider` 当成另一个后端 Agent；它只是浏览器侧的运行观察和提交控制器。

---

## 二、Python SDK 的客户端和资源模型

### 2.1 创建客户端

最小异步示例：

    from langgraph_sdk import get_client

    async def inspect_server() -> None:
        async with get_client(
            url="http://localhost:2024",
            api_key=None,
        ) as client:
            threads = await client.threads.search(limit=1)
            print(threads)

运行方式：

    uv run python -c 'import langgraph_sdk; print(langgraph_sdk.__version__)'

预期输出当前项目版本 `0.4.2`。上面的 `inspect_server()` 只有在本地 `langgraph dev` 正在运行时才会真正请求服务器。

常见误区：`get_client()` 返回的是 HTTP 客户端，不是某个具体 graph；`assistant_id` 要在创建 run 时传入。

`url` 有两个重要语义：

    get_client(url="https://.../")
      -> HTTP 调用远程 LangGraph Server

    get_client()  # url=None
      -> 尝试当前 LangGraph Server 的进程内 ASGI transport

Open SWE 的 Dashboard 模块在服务进程内部使用无 URL 版本；Webhook 和统一 dispatch 则显式使用 `LANGGRAPH_URL`，保证调用目标明确。

### 2.2 `threads`：持久化对话身份和 metadata

Thread 是长期身份，不等于一次模型调用。常用接口：

| 接口 | 作用 |
| --- | --- |
| `threads.create()` | 创建 thread，可指定 `thread_id`、metadata、TTL |
| `threads.get()` | 读取 thread metadata 和状态 |
| `threads.update()` | 增量更新 metadata |
| `threads.search()` | 按 metadata、status、分页条件检索 |
| `threads.get_state()` | 读取最新 checkpoint 状态 |
| `threads.get_history()` | 查看历史 checkpoint |
| `threads.update_state()` | 直接写入图状态，需谨慎使用 |
| `threads.delete()` | 删除 thread 及其关联运行状态 |

最小示例：

    thread = await client.threads.create(
        thread_id="demo-thread",
        metadata={"source": "tutorial", "owner": "alice"},
        if_exists="do_nothing",
    )
    thread = await client.threads.update(
        "demo-thread",
        metadata={"title": "SDK demo"},
    )
    state = await client.threads.get_state("demo-thread")

Open SWE 用 thread metadata 保存来源、用户、仓库、模型、sandbox、最新 run 等索引字段；真正的图状态仍由 LangGraph checkpoint 管理。

相关代码：

- [agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:1118)：Dashboard 创建并写入 thread metadata。
- [agent/webhooks/common.py](../../agent/webhooks/common.py:750)：Webhook 入口查找或创建 thread。
- [agent/review/findings.py](../../agent/review/findings.py:352)：Reviewer 将 findings 索引写回 thread metadata。

### 2.3 `runs`：在 thread 上启动、观察和停止一次执行

最常用接口：

| 接口 | 作用 |
| --- | --- |
| `runs.create()` | 在指定 thread 上启动一次 graph run |
| `runs.list()` | 查询某个 thread 的 runs，可按状态分页 |
| `runs.get()` | 读取单个 run |
| `runs.cancel()` | 取消一个 run |
| `runs.cancel_many()` | 批量取消一个 thread 的 pending/running runs |
| `runs.join()` | 等待并取得完成结果 |
| `runs.stream()` | 直接消费传统 run stream；新 UI 通常使用 React v2 stream |
| `runs.wait()` | 创建并等待一次运行完成 |

最小启动示例：

    run = await client.runs.create(
        "demo-thread",
        "agent",
        input={"messages": [{"role": "user", "content": "列出项目入口文件"}]},
        config={"configurable": {"thread_id": "demo-thread"}},
        stream_mode=["values", "messages"],
        stream_resumable=True,
    )
    print(run["run_id"])

这里的 `"agent"` 不是随便写的字符串，它来自 [langgraph.json](../../langgraph.json:6) 的 graph key：

    "agent": "agent.graphs.agent:traced_agent"

Open SWE 没有让各个 webhook 自己拼 `runs.create` 参数，而是集中到 [agent/dispatch.py](../../agent/dispatch.py:113) 的 `create_durable_run()`：

    {
        "multitask_strategy": "interrupt",
        "durability": "sync",
        "stream_resumable": True,
        "webhook": COMPLETION_WEBHOOK_URL,
    }

这几个改造点分别解决：后续消息中断并恢复当前 run、每一步 checkpoint、允许稍后接入的 UI 回放事件、运行完成后回调 FastAPI。

### 2.4 `store`：跨 thread、跨 run 的长期键值数据

Store 不保存某个图的完整状态；它保存应用级长期数据，例如团队设置、Profile、用户映射、计划、队列和调度记录。

常用接口：

| 接口 | 作用 |
| --- | --- |
| `store.get_item(namespace, key)` | 读取一条记录 |
| `store.put_item(namespace, key, value)` | 写入或覆盖记录 |
| `store.search_items(namespace_prefix, filter=...)` | 按 namespace/filter 分页搜索 |
| `store.delete_item(namespace, key)` | 删除记录 |
| `store.list_namespaces()` | 查看 namespace |

最小示例：

    namespace = ["tutorial", "users"]
    await client.store.put_item(namespace, "alice", {"effort": "medium"})
    item = await client.store.get_item(namespace, "alice")
    await client.store.delete_item(namespace, "alice")

Open SWE 的真实调用示例：

    team_settings.py  -> ["team_settings"] / "default"
    profiles.py       -> ["profiles"] / github_login
    user_mappings.py  -> ["user_mappings"] / normalized key
    plan_store.py     -> ["plans"]、["plan_comments", thread_id]
    schedules.py      -> ["schedules"]、["schedule_run_state"]

Store namespace 是应用约定，不是 LangGraph 自动推断出来的表名；生产物理存储由 runtime 决定。

### 2.5 `crons`：让运行时按计划触发 graph

常用接口：

| 接口 | 作用 |
| --- | --- |
| `crons.create()` | 创建周期性计划 |
| `crons.create_for_thread()` | 为现有 thread 创建唤醒计划 |
| `crons.search()` | 查询计划 |
| `crons.update()` | 修改 schedule、enabled 等字段 |
| `crons.delete()` | 删除计划 |

Open SWE 的 Dashboard 调度器把业务记录写入 Store，再把运行时 cron ID 写回记录：

- [agent/dashboard/schedules.py](../../agent/dashboard/schedules.py:188)：读写 schedule 和 run state。
- [agent/dashboard/schedules.py](../../agent/dashboard/schedules.py:308)：创建 analyzer/scheduler cron。
- [agent/tools/schedule_thread_wakeup.py](../../agent/tools/schedule_thread_wakeup.py:129)：为 thread 创建唤醒计划。

这是一种“业务记录 + 运行时计划”的双层模型：Store 记录用户可见字段，`crons` 负责实际触发。

### 2.6 `assistants`：已部署 graph 的配置入口

`assistants` 管理 graph 的版本化配置、schema 和可用 graph：

| 接口 | 作用 |
| --- | --- |
| `assistants.search()` | 搜索已注册 assistant |
| `assistants.get()` | 读取 assistant 配置 |
| `assistants.get_graph()` | 查看 graph 结构 |
| `assistants.get_schemas()` | 查看输入/输出 schema |
| `assistants.get_versions()` | 查看版本 |
| `assistants.set_latest()` | 设置当前版本 |

Open SWE 目前没有在业务代码中直接调用 `client.assistants.*`。graph 的注册由 `langgraph.json` 完成，run 只传 `"agent"`、`"reviewer"`、`"analyzer"` 等 assistant ID。

---

## 三、Open SWE 后端是如何改造 SDK 的

### 3.1 一次触发的统一入口

Slack、Linear、GitHub、Dashboard 和定时任务最终都汇聚到 durable dispatch：

    Webhook / Dashboard / Cron
        -> 确定性 thread_id
        -> thread metadata
        -> dispatch_agent_run()
        -> create_durable_run()
        -> client.runs.create()
        -> agent graph

`source` 只用于日志和 metadata；`assistant_id` 才决定运行哪个 graph。主 Agent 使用 `agent`，Reviewer 使用 `reviewer`。

### 3.2 为什么 Dashboard 不直接暴露 LangGraph API

Dashboard 的 `/dashboard/api/threads/...` 是安全代理层，不是简单反向代理：

1. 通过 session 确认 GitHub login/email。
2. 读取 thread metadata，判断 readable/owner/admin 权限。
3. 根据 Profile、团队默认和 UI override 重建可信 `configurable`。
4. 强制写入 assistant ID、stream modes、`stream_resumable` 和版本 metadata。
5. 再调用 LangGraph `/threads/{id}/commands` 或 `/stream/events`。

主要实现位于 [agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:2044) 和 [agent/dashboard/routes.py](../../agent/dashboard/routes.py:1798)。

因此浏览器不能靠修改 JSON 自己切换用户身份、仓库权限或任意模型。

### 3.3 忙碌线程的后续消息走 Store 队列

Open SWE 的前端发送逻辑位于 [useSubmitAgentMessage.ts](../../ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts:59)：

    stream.isLoading == true
        -> POST /messages
        -> 写入 Store 的 pending_messages
        -> check_message_queue_before_model
        -> 下一个模型调用前注入新消息

空闲线程才提交新的 `stream.submit()` / `run.start`。这样避免多个并发 `runs.create` 争抢同一 thread，也让 Slack、Linear、GitHub 的中途消息复用同一套机制。

### 3.4 SDK 客户端的 URL 选择

后端代码有两种写法：

    # LangGraph API 进程内部：使用 ASGI transport
    client = get_client()

    # Webhook/dispatch：明确请求部署的 LangGraph URL
    client = get_client(url=os.environ["LANGGRAPH_URL"])

这不是两个不同的协议，而是同一 Python SDK 的两种 transport。项目中的 `get_client()` 辅助函数通常只负责把这一选择封装起来。

---

## 四、Open SWE 前端是如何改造 SDK 的

### 4.1 Agents 页面：一个长期存在的 `StreamProvider`

[AgentThreadStreamProvider.tsx](../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx:80) 创建：

    const client = new Client({
      apiUrl: agentStreamApiUrl,
      apiKey: null,
      onRequest: dashboardRequest,
    })

    <StreamProvider
      apiUrl={agentStreamApiUrl}
      assistantId="agent"
      client={client}
      threadId={threadId ?? undefined}
      onCreated={onCreated}
      onCompleted={onCompleted}
    >

这里有三个关键改造：

- `apiUrl` 指向 Dashboard 代理，而不是让浏览器直连内部 LangGraph 服务。
- `credentials: "include"` 携带 session cookie；`apiKey: null` 防止浏览器误用服务端 API key。
- Provider 放在整个 `/agents` 布局层，切换 thread 时复用 controller，而不是每个页面重复建立连接。

### 4.2 `stream.submit()` 只是命令入口

发送消息的核心形状是：

    void stream.submit(
      {
        messages: [{
          type: "human",
          content: [{ type: "text", text: prompt }],
        }],
      },
      { config: { configurable: { agent_model_id, agent_effort } } },
    )

SDK 会把它转换为 `run.start` 命令。Open SWE 刻意不 `await`：Promise 通常等到整个 run 结束才 resolve，等待它会锁死输入框，无法继续排队消息。

### 4.3 SSE 事件由 SDK 聚合，UI 只消费投影

`StreamProvider` 建立事件订阅后，组件通过：

    const stream = useStreamContext()
    stream.messages
    stream.toolCalls
    stream.isLoading

读取运行状态。`AgentThreadView.tsx` 再将它们交给 [streamMessagesToUi.ts](../../ui/src/features/agents/lib/streamMessagesToUi.ts)，映射为聊天气泡、推理块、工具卡片和子 Agent 状态。

项目保留 `values`、`messages`、`tools`、`lifecycle`、`checkpoints` 等频道，以支持首次 hydration、实时消息、工具状态和断线恢复。

### 4.4 Review Chat 使用独立的 Provider

Review Chat 在 [ReviewChat.tsx](../../ui/src/features/reviews/components/ReviewChat.tsx:814) 使用独立 `StreamProvider`，其 `apiUrl` 由 PR owner/repo/number 生成，assistant ID 对应 Reviewer Chat graph。

这意味着 Agents 编码对话和 PR Review Chat 的 thread、权限和 graph 不应互相复用，尽管两者都使用相同的 React SDK。

---

## 五、LangGraph 开发最常用的 SDK 接口

下面按日常开发频率排序，而不是按 SDK 类定义排序。

### 5.1 第一组：`get_client`、`threads.create/get/update`

适合：建立会话身份、保存 owner/repo/model 等 metadata、读取当前 thread。

    client = get_client(url="http://localhost:2024")
    await client.threads.create(
        thread_id=thread_id,
        metadata={"owner": login, "repo": "acme/demo"},
        if_exists="do_nothing",
    )
    thread = await client.threads.get(thread_id)
    await client.threads.update(thread_id, metadata={"latest_run_status": "pending"})

### 5.2 第二组：`runs.create`、`runs.list`、`runs.cancel`

适合：启动 graph、查看执行状态、停止任务。

    run = await client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": prompt}]},
        config={"configurable": configurable},
        durability="sync",
        multitask_strategy="interrupt",
        stream_resumable=True,
    )

    active = await client.runs.list(thread_id, status="running")
    await client.runs.cancel(thread_id, run["run_id"], wait=False)

### 5.3 第三组：`store.get_item/put_item/search_items`

适合：跨运行共享的设置、索引、队列和业务记录。

    namespace = ["my_feature"]
    await client.store.put_item(namespace, "key", {"enabled": True})
    item = await client.store.get_item(namespace, "key")
    page = await client.store.search_items(namespace, limit=100)

不要把用户配置塞进 Python 全局字典：多 worker 或重启后会丢失，而且无法与 Dashboard、Webhook 共享。

### 5.4 第四组：`threads.get_state/get_history`

适合：恢复 UI、调试 checkpoint、查看图状态。它们读的是 thread 的执行状态，不是 Store 的任意 key-value。

    state = await client.threads.get_state(thread_id)
    history = await client.threads.get_history(thread_id, limit=20)

除非明确理解 checkpoint 语义，不要直接用 `threads.update_state()` 修改生产状态。

### 5.5 第五组：`crons.create/delete`

适合：周期调度和 thread 唤醒。

    cron = await client.crons.create(
        "scheduler",
        schedule="*/15 * * * *",
        input={"kind": "sweep"},
    )
    await client.crons.delete(cron["cron_id"])

业务配置仍应保存在 Store，cron ID 只作为运行时句柄记录下来。

### 5.6 浏览器侧：`Client`、`StreamProvider`、`useStreamContext`

普通 React Agent 页面最小形状：

    const client = new Client({
      apiUrl: "/dashboard/api/langgraph",
      apiKey: null,
      onRequest: (_url, init) => ({ ...init, credentials: "include" }),
    })

    <StreamProvider client={client} assistantId="agent">
      <Chat />
    </StreamProvider>

    function Chat() {
      const stream = useStreamContext()
      return <button onClick={() => void stream.submit({ messages })}>
        Send
      </button>
    }

Open SWE 在此基础上增加了绝对 URL 转换、Dashboard 代理、lazy thread、断线 hydration、React Query cache invalidation 和忙碌线程队列。

---

## 六、最容易犯的错误

### 6.1 把 `thread_id` 和 `run_id` 混用

- `thread_id`：长期会话身份，可有多个 runs。
- `run_id`：一次执行身份，只属于一个 thread。

### 6.2 把 `runs.stream()`、`stream.submit()` 和 SSE 混成一个接口

    runs.create / stream.submit -> 触发执行
    runs.stream                -> 传统 run 流
    StreamProvider              -> React v2 controller + SSE 投影
    threads.get_state           -> 读取 checkpoint 快照

### 6.3 忽略 `assistant_id`

`assistant_id` 决定执行哪个注册 graph。它必须对应 `langgraph.json` 中的 key，而不是 Python 函数名或任意模型名。

### 6.4 忽略 URL 和认证边界

- 后端进程内调用：可以使用 `get_client()`。
- 后端跨进程/生产调用：使用显式 `LANGGRAPH_URL` 和 API key/headers。
- 浏览器调用：使用 Dashboard session cookie，不能把服务端 API key 打包进前端。

### 6.5 忽略可恢复性参数

生产 Agent 通常至少需要评估：

    durability="sync"
    stream_resumable=True
    multitask_strategy="interrupt"  # 或按业务选择 enqueue/rollback

这些不是装饰参数，而是决定崩溃恢复、断线回放和同一 thread 并发行为的运行策略。

### 6.6 把 LangGraph Store 当成图 state

Store 适合配置、索引和跨运行业务数据；图 state/checkpoint 适合当前 Agent 执行上下文。Open SWE 同时使用两者，不能互相替代。

---

## 七、开发时的推荐顺序

实现一个新的 LangGraph 应用时，建议按下面顺序接入：

1. 用 `langgraph` 定义 graph，并在 `langgraph.json` 注册 assistant。
2. 用 `threads.create/get/update` 建立持久 thread 和最小 metadata。
3. 用 `runs.create` 验证一次最小执行，再决定是否需要 `durability`、`stream_resumable` 和多任务策略。
4. 需要跨 run 数据时再使用 `store`，先设计 namespace/key，不要先写全局缓存。
5. 需要 UI 实时体验时使用 `@langchain/langgraph-sdk` + `@langchain/react`，让 SDK 管理 SSE、重连和消息投影。
6. 最后加权限代理、取消、cron、webhook 和前端缓存失效。

Open SWE 的顺序也基本如此，只是把第 3 步集中封装成 `create_durable_run()`，把第 5 步放到 Dashboard 代理之后，以满足多来源触发、权限隔离和断线恢复。

## 八、问题驱动解答：thread、context、run、流式和 Store

这一节专门回答实际开发中最容易混淆的几个问题。先记住一句话：**thread 是长期容器，run 是容器中的一次执行，checkpoint/state 是执行历史，Store 是跨执行的业务数据库，SSE 是观察执行的传输通道。**

### 8.1 `threads` 是不是一条完整对话记录？

可以把 thread 理解为“一条长期对话的身份和容器”，但不能把 `threads.get()` 返回的对象等同于完整聊天 transcript：

| 数据 | 存的是什么 | 是否等于消息历史 |
| --- | --- | --- |
| `thread_id` | 长期会话的唯一键 | 否 |
| thread `metadata` | 用户、仓库、来源、模型、sandbox、最新状态等索引 | 否 |
| checkpoint / graph state | 每次图步骤保存的状态，通常包含消息及中间结果 | 是，完整历史应从这里读取 |
| `run_id` | 某一次执行的唯一键 | 否 |

一个 thread 可以有很多 runs。例如用户先发“分析代码”，再发“修复这个问题”，这两次运行仍然可以共享同一个 `thread_id` 和之前的 checkpoint。Open SWE 创建 thread 时写入业务 metadata，读取聊天内容时使用 `threads.get_state()` 或 StreamProvider 的 hydration，而不是把 metadata 当 transcript。

对应代码：

- Dashboard 创建 thread：[agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:1118)
- Webhook 查找或创建 thread：[agent/webhooks/common.py](../../agent/webhooks/common.py:750)
- 读取图状态：`client.threads.get_state(thread_id)`

### 8.2 `thread_id`、`metadata` 和 `context_schema` 有什么不同？

`thread_id` 与 `metadata` 属于服务端 thread 资源；`context_schema` 属于图开发者定义的“单次运行上下文类型”。三者不是同一层的东西：

| 概念 | 生命周期 | 谁定义 | 节点如何使用 | 默认是否持久化 |
| --- | --- | --- | --- | --- |
| `thread_id` | 长期，多次 run | SDK/API 调用方 | 作为 checkpoint/thread 查找键 | 是 |
| thread `metadata` | 长期，可更新 | 业务层 | 通过 thread 查询、权限和路由使用 | 是 |
| `context_schema` | 一次 `invoke`/run | `StateGraph` 开发者 | `runtime.context` 读取 | 否，除非业务主动写入 Store/metadata |
| graph state | 一次 run 的步骤间 | 图 schema | 节点参数 `state` | 由 checkpoint 持久化 |

最小图示例：

    class Context(TypedDict):
        r: float

    graph = StateGraph(State, context_schema=Context)

    def node(state: State, runtime: Runtime[Context]):
        scale = runtime.context.get("r", 1.0)
        ...

    compiled.invoke({"x": 0.5}, context={"r": 3.0})

这里的 `{"r": 3.0}` 只服务于本次调用；它不是 thread metadata，也不会自动变成下一次 run 的输入。若某个值需要跨 run 使用，就把它放入 thread metadata、Store，或者显式写入 graph state。另一个容易混淆的点是：`RunnableConfig.configurable`（例如 `thread_id`、模型 override）也是运行配置，但它仍不等于 `runtime.context` 的 `context_schema`。

当前 Open SWE 的 graph 工厂没有直接声明自定义 `StateGraph(..., context_schema=...)`；它主要通过 `RunnableConfig` 的 `configurable` 读取线程、模型和用户信息，再由 middleware 和 Store 完成业务注入。

### 8.3 `runs` 是不是“运行起来了”？前端点击终止后怎么继续？

是。`runs.create()` 或 React SDK 的 `stream.submit()` 会在指定 thread 上启动一次 graph run。run 有自己的 `run_id` 和状态（pending、running、success、error、interrupted 等），但它依附于 thread。

Open SWE 把启动参数集中在 [agent/dispatch.py](../../agent/dispatch.py:113) 的 `create_durable_run()`：

    multitask_strategy="interrupt"
    durability="sync"
    stream_resumable=True

- `multitask_strategy="interrupt"`：同一 thread 的后续输入可以中断当前 run，再处理新输入。
- `durability="sync"`：步骤完成后同步 checkpoint，进程故障时有可恢复依据。
- `stream_resumable=True`：运行事件保留，之后接入的客户端可以回放。

#### 点击停止时发生什么

Agents 页面停止按钮调用 Dashboard 的：

    POST /dashboard/api/threads/{thread_id}/cancel

路由见 [agent/dashboard/routes.py](../../agent/dashboard/routes.py:1845)，实现见 [agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:1520)。服务端会查找该 thread 的活动 runs，并调用：

    client.runs.cancel_many(
        thread_id=thread_id,
        run_ids=...,
        action="interrupt",
    )

它不会删除 thread，不会删除 checkpoint，也不会创建新 thread；metadata 只会被更新为类似 `latest_run_status="interrupted"` 的摘要。取消是异步的，刚点击后短时间仍显示 busy 属于状态传播延迟，前端应以服务端状态为准。

#### 停止后继续的正确操作

1. 保留原来的 `thread_id`。
2. 等待服务端状态变为 interrupted/idle。
3. 用户重新发送消息，前端再次调用 `stream.submit(...)`；SDK 发送新的 `run.start`，服务端从同一 thread 的 checkpoint/history 继续上下文。

不要为了“继续”新建 thread，否则历史上下文、权限 metadata 和 sandbox 关联都会丢掉。若线程其实还在运行，Open SWE 不会再创建并发 run，而是把消息发送到 `/messages` 队列；[useSubmitAgentMessage.ts](../../ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts:59) 根据 `stream.isLoading` 分流，`check_message_queue_before_model` 在下一次模型调用前把队列消息注入当前 run。

### 8.4 前端如何和 LangGraph 流式交互？

不要把“提交命令”和“SSE 观察”当成同一个接口：

    stream.submit(...)
        -> SDK 生成 run.start
        -> Dashboard 代理/权限检查
        -> LangGraph 在 thread 上创建 run
        -> POST /threads/{id}/stream/events 建立 SSE 观察
        -> SDK 聚合事件
        -> stream.messages / toolCalls / isLoading

Open SWE 的 Provider 位于 [AgentThreadStreamProvider.tsx](../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx:80)：

    const client = new Client({
        apiUrl: agentStreamApiUrl,
        apiKey: null,
        onRequest: dashboardRequest,
    })

    <StreamProvider
        client={client}
        assistantId="agent"
        threadId={threadId ?? undefined}
    />

浏览器请求先到 Dashboard 代理，使用 session cookie 鉴权，不把 LangGraph 服务端 API key 暴露给浏览器。Provider 负责订阅事件和 hydration，页面只消费 `useStreamContext()` 暴露的聚合状态。

### 8.5 断线、重试和重复事件怎么处理？

这里有两层“重试”，不能混为一谈：

| 层 | 处理对象 | Open SWE 的处理方式 |
| --- | --- | --- |
| HTTP/SSE 层 | 浏览器断网、代理断开、SSE 连接超时 | 用同一个 `thread_id` 重新订阅；可带最后事件 ID/序号恢复，不要重新创建 run |
| Agent 执行层 | 模型超时、工具临时失败、模型不可用 | middleware 的模型 fallback、tool retry、timeout 等策略处理 |

LangGraph 的 resumable stream 允许客户端在断线后回放已有事件。项目后端还提供 `threads.join_stream(thread_id, last_event_id=...)`，代理层通过 `/threads/{thread_id}/stream/events` 转发字节流，相关代码在 [agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:2024) 和 [agent/dashboard/thread_api.py](../../agent/dashboard/thread_api.py:2205)。

前端处理建议：

1. 连接断开时保留 `thread_id` 和最后收到的 event id/seq。
2. 重新调用 hydration/订阅，要求服务端从该位置继续或回放。
3. 事件可能被重放，按 event id/seq 去重；不要把每次重连收到的消息直接 append 成新消息。
4. 只有确认 run 根本没有创建（例如请求在 HTTP 建连前失败）时才考虑重新提交；否则重复 `stream.submit` 可能执行两次。
5. UI 的 `isLoading` 只能表示当前客户端观察到的状态，最终状态应通过 thread/run 查询确认。Open SWE 还用定时 thread 状态刷新作为服务端真相心跳。

因此，SSE 重连通常是“重新观察同一个 run”，不是“再启动一个 run”。模型 fallback 或工具 retry 则发生在服务端 run 内部，前端不应因为看到一次网络错误就自行复制提交。

### 8.6 `store` 在当前项目怎么用，数据存在哪里？

Open SWE 通过 `client.store.get_item/put_item/search_items/delete_item` 保存跨 thread、跨 run 的业务数据。典型 namespace/key 包括：

| 业务 | namespace/key 示例 | 代码 |
| --- | --- | --- |
| 团队默认模型 | `["team_settings"]` / `"default"` | [team_settings.py](../../agent/dashboard/team_settings.py) |
| 用户 Profile | `["profiles"]` / GitHub login | [profiles.py](../../agent/dashboard/profiles.py) |
| 用户映射 | `["user_mappings"]` / normalized key | [user_mappings.py](../../agent/dashboard/user_mappings.py) |
| 计划和评论 | `["plans"]`、`["plan_comments", thread_id]` | [plan_store.py](../../agent/dashboard/plan_store.py) |
| 调度与运行状态 | `["schedules"]`、`["schedule_run_state"]` | [schedules.py](../../agent/dashboard/schedules.py) |
| 运行中待处理消息 | `pending_messages` | [check_message_queue.py](../../agent/middleware/check_message_queue.py:188) |

Store 和 thread checkpoint 的关系可以这样记：

    Store:             namespace + key -> 跨运行业务值
    Thread checkpoint: thread_id + checkpoint -> 图执行状态/消息历史
    Thread metadata:   thread_id -> 业务索引、权限、运行摘要

业务代码不直接写 SQL；Store 的物理后端由 LangGraph runtime 选择：

- `langgraph dev` 的 in-memory runtime 主要保存在进程内；当前运行时默认还会把本地数据刷到 `.langgraph_api/store.pckl` 和 `.langgraph_api/store.vectors.pckl`。它适合开发调试，不适合多进程高可用。
- 生产部署通常使用 PostgreSQL runtime（通过 `DATABASE_URI` 配置），threads、runs、checkpoints 和 Store 都由运行时持久化到 PostgreSQL；Redis 负责队列/协调等运行时能力。Open SWE 的业务模块只调用 SDK，不负责数据库表和 SQL。

所以“Store 存在哪里”不能只看 `team_settings.py`：那个文件只决定 namespace/key；真正的落点由当前 LangGraph Server 的 runtime 配置决定。切换 dev 到生产时，必须检查 runtime、`DATABASE_URI`、备份和多 worker 配置，否则把开发环境的内存 Store 当成生产数据库，重启就会丢数据。

### 8.7 Agent 如何获取 thread metadata？

`RunnableConfig` 中通常只有 `thread_id`，它是查询键，不是用户、仓库或 sandbox 内容本身。Agent 获取业务 metadata 的过程是：

    config["configurable"]["thread_id"]
        -> client.threads.get(thread_id)
        -> thread["metadata"]
        -> 使用 owner/repo/sandbox/model 等字段

Open SWE 的实际读取方式包括：

- `ensure_sandbox_for_thread()` 根据 `thread_id` 读取 metadata 中的 `sandbox_id`，决定复用、重连还是创建 sandbox：[agent/server.py](../../agent/server.py:430)。
- `check_message_queue.py` 通过 `client.threads.get()` 读取 metadata 中的模型：[agent/middleware/check_message_queue.py](../../agent/middleware/check_message_queue.py:42)。
- Reviewer 的 `get_thread_metadata()` 封装了严格的 thread 查询，读取 PR、head SHA、findings 等字段：[agent/review/findings.py](../../agent/review/findings.py:334)。

因此三者的职责是：

    thread_id       = 找到哪一个 thread
    threads.get()   = 从 runtime 查询 thread
    metadata        = 用户、仓库、sandbox、模型等业务索引

Agent 工厂读取 `thread_id` 后负责装配图；LangGraph runtime 负责根据同一个 `thread_id` 恢复 checkpoint。metadata 查询和 checkpoint 恢复是两条相关但独立的路径。

### 8.8 前端“新建对话”是否等于创建 thread？

逻辑上等于，但 Open SWE 的 Agents 页面采用延迟创建（lazy create）：新对话页面初始没有 `threadId`，Provider 允许 `threadId` 为空；用户第一次调用 `stream.submit()` 时，SDK 才生成 thread ID、创建 thread 并启动第一个 run。

    Agents 首页（threadId = null）
        -> 第一次 stream.submit(message)
        -> LangGraph 创建 thread
        -> 启动第一个 run
        -> onCreated 得到 thread_id
        -> 前端更新缓存并跳转到 /agents/{thread_id}

实现见 [AgentThreadStreamProvider.tsx](../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx:80) 和 [AgentsHome.tsx](../../ui/src/features/agents/components/AgentsHome.tsx:92)。所以“点击新建”不一定立即调用 `threads.create()`；第一次发送消息才是持久化 thread 的时刻。已有 thread 再发送消息时，只创建新的 run，不创建新的 thread。

### 8.9 checkpoint 是如何持久化和恢复的？

Checkpoint 由 LangGraph runtime 的 checkpointer 管理，业务代码不需要手动把每条消息写数据库。一次 run 的过程可以简化为：

    run.start(thread_id)
        -> checkpointer 读取最新 checkpoint
        -> 恢复 graph state
        -> Pregel 执行一个 step
        -> checkpointer 保存新 checkpoint
        -> 继续下一个 step

Checkpoint 通常包含消息、节点状态、channel 值、待执行节点（`next`）、父 checkpoint 标识和写入 metadata。`threads.get_state(thread_id)` 读取最新快照，`threads.get_history(thread_id)` 读取历史快照。

Open SWE 在 [agent/dispatch.py](../../agent/dispatch.py:113) 的 durable run 中设置：

    durability="sync"

它要求步骤 checkpoint 同步完成后再推进下一步，因此进程崩溃或 worker 重启时，可以从最近一次已提交的 checkpoint 恢复。`thread_id` 不变是恢复上下文的前提；换一个 thread 就会得到另一份 state/history。

存储位置由 LangGraph Server runtime 决定：

- `langgraph dev` 使用 in-memory runtime，checkpoint 主要在进程内存中；本地 runtime 可能将运行数据写入 `.langgraph_api` 等文件，具体文件由 CLI/runtime 版本决定，不应当当作生产数据库。
- 生产通常使用 PostgreSQL runtime，通过 `DATABASE_URI` 连接数据库；threads、runs、checkpoints 和 Store 由 runtime 的数据库实现保存。Open SWE 只调用 SDK，不直接写 checkpoint 表或 SQL。

`langgraph.json` 中的 `checkpointer.ttl` 只控制 checkpoint 的过期清理，不负责选择数据库。可以这样区分：

    RunnableConfig  -> 本次运行的配置和 thread_id
    checkpoint      -> thread 的图状态和消息历史
    thread metadata -> 用户、仓库、sandbox、模型等业务索引

## 本章小结

一句话记忆：**`langgraph` 负责造图，Python `langgraph_sdk` 负责从后端控制图，TypeScript SDK 负责浏览器协议，`@langchain/react` 负责把运行流变成 React 状态。**

在 Open SWE 中，`threads` 管长期身份，`runs` 管一次执行，`store` 管跨运行数据，`crons` 管定时触发，`assistants` 管已注册 graph 的版本化配置；Dashboard 和 durable dispatch 则是在这些基础接口之上补齐权限、恢复、队列和业务元数据。

## 代码索引

- [图注册](../../langgraph.json)
- [统一 durable dispatch](../../agent/dispatch.py)
- [Dashboard thread/run 代理](../../agent/dashboard/thread_api.py)
- [Dashboard Store 配置](../../agent/dashboard/team_settings.py)
- [Dashboard 调度](../../agent/dashboard/schedules.py)
- [Agents StreamProvider](../../ui/src/features/agents/lib/AgentThreadStreamProvider.tsx)
- [Agents 消息提交](../../ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts)
- [SDK 命令与 SSE 详解](./10-langgraph-sdk-command-and-sse.md)

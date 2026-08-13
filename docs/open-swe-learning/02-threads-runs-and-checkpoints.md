# 第 2 章：线程、Run 与持久化

## 学习目标

读完本章后，你应能沿源码追踪一次 Dashboard 消息如何启动 Agent，并能区分线程、Run、LangGraph checkpoint、sandbox 和 Git 工作区快照。

## 五个容易混淆的对象

| 对象 | 含义 | 生命周期 | 本项目的用途 |
| --- | --- | --- | --- |
| `thread_id` | 一段任务/对话的稳定身份 | 跨多次请求 | 关联历史、元数据和 sandbox |
| `run_id` | 一次具体执行 | 单次执行 | 展示与取消当前执行 |
| `configurable` | 本次执行的运行参数 | 单次 Run | 传递线程、来源、仓库、模型等 |
| LangGraph checkpoint | 图状态快照 | 由 TTL 管理 | 从中断或故障处恢复图执行 |
| sandbox + Git ref | 代码工作区和本轮起点 | 每线程/每用户消息 | 让 Agent 改代码，并向 UI 展示文件 diff |

`langgraph.json` 将 checkpoint 默认 TTL 配置为 43,200 秒，即 12 小时；清扫任务每 60 分钟运行一次。它不是 sandbox 中仓库文件的备份。

## Dashboard 的启动链路

```text
UI 发出 run.start
  -> POST /threads/{thread_id}/commands
  -> proxy_dashboard_thread_commands
  -> _enrich_run_start_command
  -> LangGraph /threads/{thread_id}/commands
  -> traced_agent -> get_agent(config)
  -> PrepareAgentRunMiddleware
  -> sandbox、模型、工具和中间件
  -> SSE 事件回到 UI
```

首次 `run.start` 可以针对一个尚不存在的客户端生成的 `thread_id`。Dashboard 会先创建线程记录，写入所有者、仓库、标题、解析后的模型和计划模式，然后将重建后的 `configurable` 转发给 LangGraph。

对应源码：

- [配置入口](../../langgraph.json)
- [Dashboard 命令代理](../../agent/dashboard/thread_api.py)
- [主 Agent 工厂](../../agent/server.py)
- [FastAPI 路由](../../agent/dashboard/routes.py)

![Dashboard 启动链路时序图](architecture/premium/png/02-dashboard-run-sequence.png)

图中要特别看两条分开的线：上半段命令请求只拿到 `run_id`，下半段 `/stream/events` 才持续接收模型、工具和生命周期事件。

## FastAPI 路由：Dashboard 的协议边界

这里的“FastAPI 路由”容易被误解成“路由函数里直接运行 Agent”。实际结构是一个三层适配器：

```text
LangGraph 配置
  -> agent.webapp:app
  -> agent.api.app:create_app()
  -> app.include_router(dashboard_router)
  -> router(prefix="/dashboard/api")
  -> 路由函数（鉴权、参数校验、转发/业务查询）
```

源码事实：`langgraph.json` 的 `http.app` 指向 `agent.webapp:app`；`agent/webapp.py` 只是兼容入口，真正的 `FastAPI` 实例在 `agent/api/app.py` 创建。`create_app()` 把 Dashboard router 挂载进去，而 `agent/dashboard/routes.py` 又给它统一加上 `/dashboard/api` 前缀。因此源码中的 `@router.post("/threads/{thread_id}/commands")`，浏览器实际访问的是：

```text
POST /dashboard/api/threads/{thread_id}/commands
```

### 1. 路由先做什么：身份、来源和资源边界

`router = APIRouter(prefix="/dashboard/api", dependencies=[Depends(require_same_origin_for_mutations)])` 有一个全局写保护：`GET/HEAD/OPTIONS` 放行，`POST/PUT/PATCH/DELETE` 必须通过同源/允许来源检查，避免带 cookie 的跨站请求伪造。

绝大多数接口还声明 `session: dict = _SESSION_DEP`。`_SESSION_DEP` 最终调用 `require_session()`，从 `osw_session` HttpOnly cookie 解出 JWT；没有 cookie 就是 `401 not authenticated`。这一步只说明“你是谁”，不代表你能读写任意线程。

线程访问再由 `thread_api.py` 细分：

| 权限函数 | 规则 | 典型接口 |
| --- | --- | --- |
| `_assert_thread_readable` | 已登录组织成员可读 surfaced source（dashboard/github/slack/linear/schedule）线程 | `GET /threads/{id}`、`/state`、`/stream/events` |
| `_assert_thread_owner` | 只有线程 owner login/email 可写 | `messages`、`resolve`、`cancel`、`delete` |
| `_session_is_admin` / `_ADMIN_DEP` | 管理员额外权限 | `/admin/threads/{id}/cancel`、`?all=true` |

读取权限和写入权限故意分开：同事可以打开 Slack 产生的线程查看运行结果，但不能冒充 owner 删除或取消它；写入别人的线程时，代理会把内容加上 `@login:` 归因前缀。

### 2. 路由按职责分成五组

| 组 | 端点 | 是否直达 LangGraph | 作用 |
| --- | --- | --- | --- |
| 列表/摘要 | `GET /threads`、`/sidebar`、`/page`、`/{id}` | 否，使用 SDK 查询并整理 metadata | 给侧边栏和线程详情页 |
| 启动/控制 | `POST /{id}/commands`、`POST /{id}/runs/{run}/cancel` | 是，代理到 `/commands` 或 `/cancel` | 启动 run、发协议命令、停止执行 |
| 观察/恢复 | `POST /{id}/stream/events`、`GET /{id}/stream`、`GET /{id}/state`、`POST /{id}/history` | 是，代理或 SDK join | SSE 事件、状态 hydration、断线恢复 |
| 忙碌线程交互 | `POST /{id}/messages` | 否，写入 Store 队列 | 不创建第二个 run，把追问交给当前 run |
| 代码结果 | `GET /{id}/turn-diff`、`/pr-diff`、`/recovery.patch` | 部分 | 从 Git ref、GitHub API 或 sandbox 生成文件变化 |

关键边界：`/commands` 和 `/stream/events` 虽然都由同一个 FastAPI router 暴露，但前者是“控制面”，后者是“观察面”。命令响应通常很快返回 `run_id`；事件流才承载模型 token、工具调用、checkpoint 和生命周期状态。

### 3. 一个 `run.start` 的真实请求

UI 的 `StreamProvider` 使用 `ProtocolSseTransportAdapter`，会把 `stream.submit(...)` 转换为 LangGraph v2 命令协议。概念化后的请求如下（字段名对应实际协议，省略 SDK 生成的非关键字段）：

```json
{
  "method": "run.start",
  "params": {
    "input": {
      "messages": [{"role": "human", "content": "检查这个项目的测试"}]
    },
    "config": {
      "configurable": {
        "agent_model_id": "openai:gpt-5.6-terra",
        "agent_effort": "medium",
        "repo": {"owner": "ljxpython", "name": "demo"}
      }
    }
  }
}
```

请求进入 `api_thread_commands()` 后，执行顺序是：

1. `require_session` 和 router 级 CSRF 检查先挡住未登录/跨站写请求。
2. `proxy_dashboard_thread_commands()` 校验 `Content-Type: application/json`，解析 JSON 对象。
3. 用 `langgraph_client().threads.get(thread_id)` 判断线程是否存在。首次 `run.start` 允许懒创建；其它命令打不存在的线程直接 `404`。
4. `_enrich_run_start_command()` 调用 `_ensure_dashboard_github_token()`，解析模型、effort、计划模式和图片输入。
5. 首次运行调用 `_create_dashboard_thread_record()` 写入 owner、仓库、标题、模型和时间；随后 `_build_dashboard_configurable()` 从服务端 metadata 重建可信配置。客户端提供的 `repo` 只是创建提示，不会未经检查原样成为最终运行身份。
6. 代理强制补入 `assistant_id="agent"`、完整 `_DASHBOARD_STREAM_MODES` 和 `stream_resumable=true`，再向 `LANGGRAPH_URL/threads/{id}/commands` 发 HTTP 请求。
7. 收到 LangGraph 返回的 `run_id` 后，更新线程 metadata 的 `latest_run_id/latest_run_status=pending`，最后把状态码、响应体和 `Content-Type` 返回 SDK。

可以把它压缩成下面的接线图：

```text
Browser cookie
    |
    v
FastAPI /commands -- session + CSRF + thread ACL --> thread_api
    |                                                   |
    |                                                   +--> threads.create/update（首次）
    |                                                   +--> enrich config（服务端重建）
    v
LangGraph /threads/{id}/commands ---------------------> run_id
```

注意：FastAPI 没有在这里调用 `get_agent()`。LangGraph 接收到 `assistant_id="agent"` 和 `configurable.thread_id` 后，才进入图工厂，最终由 `agent.server:get_agent` 创建本次运行使用的 Agent。

### 4. 为什么事件流必须是单独的路由

`POST /threads/{id}/stream/events` 先在 generator 外执行可失败的检查：JSON 类型、线程可读性；这样错误会以正常 HTTP `4xx/5xx` 返回，而不是已经开始 SSE 后才变成难处理的半截响应。

通过检查后，`_stream_thread_events()` 使用 `httpx.AsyncClient.stream()` 请求 LangGraph 的同名端点，并逐块 `yield` 上游字节。上游错误会被转换成：

```text
event: error
data: {"status": 401, "detail": "..."}
```

正常事件不在 FastAPI 中重新解释，尽量保持 LangGraph v2 event schema；UI 的 `@langchain/react` 再按 `messages`、`tools`、`lifecycle`、`checkpoints` 等频道消费。这就是“路由代理做协议边界，React Provider 做事件解释”。

`GET /threads/{id}/stream` 是另一条 join 流：服务端调用 `threads.join_stream(last_event_id=...)`，把 SDK part 包装成带 `id` 的 SSE `data:` 行，用于已有线程的恢复和重连。它和 `POST /stream/events` 都是 SSE，但来源不同：前者是 join 已有流，后者是按 v2 命令协议订阅当前运行。

### 5. 忙碌时的 `/messages` 为什么不启动新 Run

`useSubmitAgentMessage()` 发现 `stream.isLoading` 时调用 `POST /messages`。`send_dashboard_message()` 会确认线程可读、线程确实 active，然后把消息写入 `queue_message_for_thread()` 的 `("queue", thread_id)` Store 命名空间。下一次模型调用前，`check_message_queue_before_model` 以 FIFO 读取并删除队列，再把它注入当前 Agent 的消息状态。

```text
POST /messages
  -> get_thread_active_status == active
  -> queue_message_for_thread(("queue", thread_id))
  -> current run continues
  -> before_model reads + deletes queue
  -> new HumanMessage enters model context
```

所以“发送追问”不是“再开一个线程”，也不是“把消息直接塞进已经发出的 HTTP 请求”；它是当前 Run 的协作输入通道。

### 6. 状态和 diff 接口的区别

- `GET /state` 调 `client.threads.get_state()`，主要服务 SDK hydration。运行刚启动但最新 checkpoint 仍显示 `next=[]` 时，代码会在 pending/running 状态下移除 `next`，让 `StreamProvider` 把线程判断为 active 并打开事件订阅。
- `GET /turn-diff` 读取 metadata 中的 `turn_checkpoints`，调用 `read_turn_diff()` 比较 `refs/open-swe/turns/<key>` 与下一个 ref/当前 worktree。它不是 LangGraph 状态 diff，而是 Git 文件 diff。
- `GET /pr-diff` 使用用户自己的 GitHub OAuth token 请求 GitHub API；`GET /recovery.patch` 在 sandbox 中生成并下载 patch，属于代码工件接口，不属于 LangGraph 事件协议。

## FastAPI 路由的伪代码

```python
@router.post("/threads/{thread_id}/commands")
async def route(thread_id, request, session=requires_session):
    body = await request.body()
    status, data, media_type = await proxy_dashboard_thread_commands(
        thread_id, session["sub"], body, email=session.get("email")
    )
    return Response(data, status_code=status, media_type=media_type)

async def proxy_dashboard_thread_commands(...):
    command = parse_json(body)
    thread = await langgraph.threads.get(thread_id)
    check_read_or_owner_acl(thread, command["method"])
    command = await enrich_run_start(command, trusted_session, thread.metadata)
    response = await httpx.post(LANGGRAPH_URL + "/threads/.../commands", json=command)
    if command["method"] == "run.start":
        await update_latest_run_metadata(response)
    return response
```

伪代码省略了 FastAPI 类型声明，但保留了真实控制流：路由只负责会话和 HTTP 适配；代理负责授权、metadata/configurable 重建和转发；图工厂负责真正装配 Agent；SSE 由 UI Provider 消费。

## Webhook 的启动链路

Slack、Linear、GitHub 入口通过 `dispatch_agent_run` 汇聚到 `create_durable_run`。后者调用 LangGraph SDK 的 `runs.create`，并统一使用：

| 参数 | 值 | 作用 |
| --- | --- | --- |
| `multitask_strategy` | `interrupt` | 新任务可中断同线程旧 Run，随后从状态继续 |
| `durability` | `sync` | 每个步骤前同步 checkpoint |
| `stream_resumable` | `True` | UI 可补接或回放已有 Run 的事件流 |
| `prepare_run_id` | UUID | 区分同线程内一次准备阶段 |

这条路径的核心源码是 [agent/dispatch.py](../../agent/dispatch.py)。

## 执行前准备

`get_agent` 只有拿到 `configurable.thread_id` 且运行时标记为执行模式时，才返回带 sandbox 的完整 Agent。它解析模型、构造 backend、工具、子 Agent 和 middleware。

随后 `PrepareAgentRunMiddleware` 会：

1. 解析当前线程的 GitHub 凭据。
2. 为 `thread_id` 创建或连接 sandbox。
3. 取得工作目录并把当前 Git 状态记为 `refs/open-swe/turns/<message-id>`。
4. 更新线程元数据，例如模型、来源、计划模式和该轮 checkpoint ref。
5. 生成包含仓库与用户指令的系统提示词。

这里的 Git ref 是为了 diff；LangGraph checkpoint 是为了恢复图状态。两者解决的问题不同，不能互相替代。

![状态对象生命周期](architecture/premium/png/05-state-lifecycle.png)

## 运行中如何插话

当线程忙碌时，Dashboard 的 `POST /threads/{thread_id}/messages` 不会创建第二个 Run。它将消息存入 LangGraph Store 的 `("queue", thread_id)` 命名空间。

`check_message_queue_before_model` 是主 Agent 的 before-model middleware。下一次模型调用前，它读取并先删除该队列，再把消息按 FIFO 顺序追加为新的用户消息。这避免同一条追问被重复消费。

## 最小本地验证

```bash
uv run pytest -vvv tests/agent/test_dispatch.py
uv run pytest -vvv tests/middleware/test_check_message_queue.py
```

已验证结果：前者 10 项通过，验证 durable Run 默认参数；后者 6 项通过，验证 Dashboard 消息入队与 before-model 注入。两者都没有调用模型或外部服务。

## 常见误区

1. `thread_id` 不是 `run_id`。前者是一段持续上下文，后者是其中一次执行。
2. `durability="sync"` 不等于工作区自动备份。它保存图状态，代码变更的可视化依赖 Git ref。
3. `configurable` 不是消息状态。它是本次执行的参数通道；消息历史由 LangGraph 状态和 checkpoint 管理。

## 扩展边界

本章尚未深入 LangGraph 的 checkpoint 后端实现、跨 12 小时 TTL 的长期记忆策略，以及生产环境的外部持久化配置。这些会在部署和可观测性章节补充。

## 检查题

1. 为什么同一用户在同一线程连续发送两句话时，应保留相同的 `thread_id`？
2. 为什么忙碌线程的 Dashboard 追问要进队，而不是直接创建一个新的 Run？
3. 在 `PrepareAgentRunMiddleware` 中，为什么要同时记录 LangGraph 状态与 Git 工作区 ref？

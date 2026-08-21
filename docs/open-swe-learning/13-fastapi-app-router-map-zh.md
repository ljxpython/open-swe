# 第 13 章：`agent/api/app.py` FastAPI Router 说明

## 先说结论

`app.include_router(...)` 的作用是把一个 `APIRouter` 注册到 FastAPI 应用中，让其中声明的路径进入最终路由表。

它只表示“接口可以被调用”，不表示应用启动时会自动调用每个接口。实际是否收到请求，取决于调用方和部署配置：

```text
app.include_router(router)
    -> 应用启动时注册路由
    -> 浏览器、GitHub、Slack、Linear、LangGraph Server 或负载均衡器发请求
    -> 对应 endpoint 函数才执行
```

当前应用入口是 [agent/api/app.py](../../agent/api/app.py:32)：

```python
app.include_router(dashboard_router)
app.include_router(plan_router)
app.include_router(workflow_approval_router)
app.include_router(linear_webhook_router)
app.include_router(slack_webhook_router)
app.include_router(health_router)
app.include_router(github_webhook_router)
```

## 一张表看清七个 Router

| Router | 主要路径 | 主要调用方 | 用途 | 是否一定会收到请求 |
| --- | --- | --- | --- | --- |
| `dashboard_router` | `/dashboard/api/*` | 浏览器 Dashboard、OAuth 回调 | 登录、用户配置、thread、run、SSE、调度、管理 API | 只有使用 Dashboard 或管理功能时 |
| `plan_router` | `/dashboard/api/plan/*` | Dashboard Plan UI | 读取/编辑计划、评论、批准和拒绝计划 | 只有 Agent 进入 Plan 流程时 |
| `workflow_approval_router` | `/dashboard/api/workflow-approval/*` | Dashboard 工作流审批 UI | 查看、批准、拒绝 workflow 文件 push | 只有 Agent 产生待审批 push 时 |
| `linear_webhook_router` | `/webhooks/linear` | Linear Webhook 服务 | 接收 Linear 评论并触发 Agent | 配置 Linear Webhook 后才会 |
| `slack_webhook_router` | `/webhooks/slack*` | Slack Events API、Slack Interactivity | 接收 Slack 消息、反应和 Block Kit 操作 | 配置 Slack App 后才会 |
| `health_router` | `/health`、`/webhooks/run-complete` | 负载均衡器、LangGraph Server | 健康检查、Run 完成回调 | `/health` 通常会；回调取决于 webhook 配置 |
| `github_webhook_router` | `/webhooks/github` | GitHub Webhook | 接收 Issue、PR、Review、Push 事件 | 配置 GitHub Webhook 后才会 |

## 1. `dashboard_router`：Dashboard 主 API

定义位置：[agent/dashboard/routes.py](../../agent/dashboard/routes.py:239)。它的前缀是：

```text
/dashboard/api
```

这是整个 Web Dashboard 的主要后端入口，接口数量最多，包含几类能力：

| 能力 | 典型路径 | 作用 |
| --- | --- | --- |
| OAuth/session | `/dashboard/api/auth/login`、`callback`、`logout`、`/me` | GitHub 登录、session 建立和当前用户信息 |
| Profile 与指令 | `/profile`、`/me/instructions` | 用户模型偏好、默认仓库和用户级指令 |
| Thread | `/threads`、`/threads/{id}` | thread 列表、详情、删除、标记已读/解决 |
| Run 控制 | `/threads/{id}/cancel`、`/threads/{id}/stream` | 停止运行、观察事件流、获取状态 |
| LangGraph 代理 | `/threads/{id}/commands`、`/stream/events` | 浏览器不直连内部 LangGraph，而由 Dashboard 做鉴权和参数重建 |
| 计划/调度 | `/schedules`、`/review-style` 等 | 创建、更新和触发自动化任务 |
| 管理配置 | team settings、enabled repos、credentials | 管理员配置团队默认模型、仓库和集成凭据 |

前端调用证据：

- Agents 页面通过 [ui/src/features/agents/lib/api.ts](../../ui/src/features/agents/lib/api.ts:125) 调用 `/dashboard/api` 下的 thread、run、SSE 和调度接口。
- Review、Settings、Admin 页面通过 `ui/src/lib/api.ts`、`ui/src/lib/dashboard-fetch.ts` 等封装调用。
- `AgentThreadStreamProvider` 将 LangGraph SDK 的 `apiUrl` 指向 Dashboard 代理，而不是内部 LangGraph 地址。

因此 `dashboard_router` 不是某一个单独功能，而是浏览器 Dashboard 的总 API 入口。只要启用 Web Dashboard，其中一部分接口就会被调用；没有打开某个页面或功能时，对应 endpoint 可能不会执行。

## 2. `plan_router`：Agent 计划审查 API

定义位置：[agent/dashboard/plan_api.py](../../agent/dashboard/plan_api.py:52)，前缀是：

```text
/dashboard/api/plan
```

主要接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/{thread_id}` | 获取计划正文、状态、用户和权限信息 |
| `PUT` | `/{thread_id}` | 更新计划正文 |
| `GET` | `/{thread_id}/comments` | 获取计划评论 |
| `POST` | `/{thread_id}/comments` | 添加评论 |
| `DELETE` | `/{thread_id}/comments/{comment_id}` | 删除评论 |
| `POST` | `/{thread_id}/approve` | 批准计划并启动后续 Agent run |
| `POST` | `/{thread_id}/reject` | 拒绝/退回计划 |

前端调用封装在 [ui/src/lib/plan.ts](../../ui/src/lib/plan.ts:52)。

重要区别：Agent 生成计划时通常通过工具和 Store 写入计划；Plan UI 再通过 `plan_router` 读取、评论和审批。也就是说，计划内容的产生和计划 API 的展示/审批是两个入口。

## 3. `workflow_approval_router`：workflow 文件 push 审批

定义位置：[agent/dashboard/workflow_approval_api.py](../../agent/dashboard/workflow_approval_api.py:18)，前缀是：

```text
/dashboard/api/workflow-approval
```

接口包括：

```text
GET  /{thread_id}
POST /{thread_id}/{fingerprint}/approve
POST /{thread_id}/{fingerprint}/reject
```

它处理的是 Agent 想要 push GitHub Actions workflow 文件时的高风险审批，不是普通 Plan 审批。`fingerprint` 用来锁定某一次具体的 workflow 修改，避免用户批准了错误版本。

Dashboard 前端通过 [ui/src/features/agents/lib/api.ts](../../ui/src/features/agents/lib/api.ts:274) 请求这些接口，`WorkflowApprovalCard` 展示审批卡片。

Slack 中的按钮审批走 `/webhooks/slack/interactivity`，但最终会复用相同的 workflow approval service。两个入口不同：

```text
Dashboard 按钮 -> workflow_approval_router
Slack Block Kit -> slack_webhook_router -> workflow approval service
```

## 4. `linear_webhook_router`：Linear 外部事件入口

定义位置：[agent/webhooks/linear_routes.py](../../agent/webhooks/linear_routes.py:7)。路径为：

```text
POST /webhooks/linear
GET  /webhooks/linear
```

### `POST /webhooks/linear`

Linear 服务在评论创建时调用它。路由会：

1. 验证 `Linear-Signature`。
2. 只处理 `Comment` + `create` 事件。
3. 忽略机器人自己的评论。
4. 检查评论是否提到 `@open-swe`。
5. 解析仓库、用户和 Linear issue。
6. 通过 `BackgroundTasks` 调度 `process_linear_issue`。
7. 最终由 durable dispatch 在对应 thread 上启动 Agent run。

### `GET /webhooks/linear`

返回 endpoint active 状态，主要用于 webhook 配置或人工探活。它不是 Linear 业务事件入口。

如果没有在 Linear 后台配置 webhook，这个 router 虽然已注册，但不会收到真实 POST 请求。

## 5. `slack_webhook_router`：Slack 消息和交互入口

定义位置：[agent/webhooks/slack_routes.py](../../agent/webhooks/slack_routes.py:7)。它包含三个路径：

```text
POST /webhooks/slack
POST /webhooks/slack/interactivity
GET  /webhooks/slack
```

### `POST /webhooks/slack`

接收 Slack Events API 事件，主要处理：

- `app_mention`；
- 私信；
- 符合规则的线程回复；
- 反馈 reaction；
- 事件去重和 Slack 重试；
- 用户、频道和 thread 上下文解析。

通过校验后，路由不会同步执行完整 Agent，而是把任务加入 FastAPI background task，再由 Slack service 和 dispatch 继续处理。

### `POST /webhooks/slack/interactivity`

接收 Slack Block Kit 按钮、下拉框等交互，当前包括：

- workflow push approve/reject；
- Plan approve/cancel；
- Plan revise 或其他 Open SWE 选项。

它负责验签、解析 action payload、检查用户是否有权操作，然后触发审批或重新派发 Agent。

### `GET /webhooks/slack`

返回 endpoint active 状态，主要是配置验证或人工检查，不处理聊天消息。

## 6. `health_router`：健康检查和运行完成回调

定义位置：[agent/api/health.py](../../agent/api/health.py:9)。它虽然叫 `health_router`，实际包含两类完全不同的接口。

### `GET /health`

返回：

```json
{"status": "healthy"}
```

通常由负载均衡器、容器编排平台或监控系统调用。它只证明 FastAPI 进程能够响应，不代表模型、数据库、sandbox 或 LangGraph worker 全部正常。

### `POST /webhooks/run-complete`

LangGraph Server 在 run 完成或失败后回调这个接口。它会：

1. 校验 query 参数中的 completion token。
2. 解析 run 完成 payload。
3. 调用 `handle_run_completion()`。
4. 更新 thread/run 状态，并向 Slack、Linear、GitHub 等来源发送必要的完成或失败回复。

该接口只有在 `COMPLETION_WEBHOOK_URL` 和 `RUN_COMPLETE_WEBHOOK_SECRET` 配置正确时，才会被 `agent/dispatch.py:create_durable_run()` 附加到 run。否则路由虽存在，但不会收到 LangGraph 回调。

## 7. `github_webhook_router`：GitHub Issue/PR 事件入口

定义位置：[agent/webhooks/github_routes.py](../../agent/webhooks/github_routes.py:7)，路径为：

```text
POST /webhooks/github
```

GitHub Webhook 调用后，路由会：

1. 验证 `X-Hub-Signature-256`。
2. 根据 `X-GitHub-Event` 判断事件类型。
3. 过滤不支持的 action、仓库和用户场景。
4. 处理 Issue 评论、PR 评论、Review、PR opened/ready_for_review、push 等事件。
5. 将可处理事件放入 background task。
6. 由后续 service 生成确定性 `thread_id`，再调用 durable dispatch。

不同 GitHub 事件的后续 Agent 不完全相同：

```text
Issue / PR comment       -> 主 Agent
PR opened / ready        -> Reviewer auto-review
PR push                  -> Reviewer watch 更新
review finding reply     -> Reviewer 线程继续处理
```

所以 GitHub router 是“事件门禁和分流层”，不是 Agent 本身。

## 8. 一次请求从哪里进入？

### Dashboard 浏览器对话

```text
浏览器
  -> dashboard_router
  -> thread/run 权限和参数代理
  -> LangGraph commands / stream/events
  -> Agent graph
```

### Slack / Linear / GitHub

```text
外部平台 Webhook
  -> 对应 webhook_router
  -> 验签和事件过滤
  -> BackgroundTasks
  -> 确定性 thread_id
  -> dispatch_agent_run()
  -> client.runs.create()
```

### LangGraph 完成回调

```text
LangGraph Server
  -> POST /webhooks/run-complete
  -> health_router
  -> handle_run_completion()
  -> 更新状态并回源通知
```

### 运行探活

```text
Load Balancer / Kubernetes / Monitor
  -> GET /health
  -> {"status": "healthy"}
```

## 9. 如何判断接口“实际被调用了”？

不要根据 `include_router` 判断调用次数。应从以下几层确认：

1. 看外部配置：GitHub、Slack、Linear webhook URL 是否指向当前服务。
2. 看前端代码：是否存在对应 `fetch` 或 SDK 请求。
3. 看 LangGraph dispatch：是否配置了 completion webhook。
4. 看 FastAPI access log、反向代理日志和 endpoint 内部日志。
5. 用 `app.routes` 查看注册结果，用测试客户端发送受控请求。

可以使用下面的只读检查查看最终注册路径。使用 OpenAPI 路径表比直接遍历 `app.routes` 更可靠：当前 FastAPI 版本可能把被挂载的 router 保存在内部 `_IncludedRouter` 中，顶层列表不会直接展开所有子路由。

```python
from agent.api.app import app

for path, operations in sorted(app.openapi()["paths"].items()):
    methods = ",".join(sorted(operations))
    print(methods, path)
```

这只能证明路由已注册并能生成 OpenAPI 描述，不能证明生产流量实际访问过它。真实调用需要结合 access log、外部平台投递记录和对应业务日志判断。

## 10. Router 与 LangGraph SDK 的具体接口、调用链和代码行号

先区分两种 SDK：

```text
浏览器端：@langchain/langgraph-sdk / @langchain/react
服务端：  Python langgraph_sdk.get_client()
```

FastAPI Router 本身通常只是 HTTP 入口；真正的 Python SDK 调用可能在 `thread_api.py`、`dispatch.py` 或 webhook service 中。

### 10.1 `dashboard_router`

挂载位置：[agent/api/app.py:51](../../agent/api/app.py:51)，前缀定义：[agent/dashboard/routes.py:239](../../agent/dashboard/routes.py:239)。代表性接口如下：

| HTTP 接口 | Router 行号 | SDK/代理实现 | SDK 行号和操作 |
| --- | --- | --- | --- |
| `GET /dashboard/api/threads/{thread_id}` | [routes.py:1746](../../agent/dashboard/routes.py:1746) | `thread_api` | [thread_api.py:1021](../../agent/dashboard/thread_api.py:1021) `client.threads.get()` |
| `GET /dashboard/api/threads/{thread_id}/state` | [routes.py:1870](../../agent/dashboard/routes.py:1870) | `get_dashboard_thread_state` | [thread_api.py:1670](../../agent/dashboard/thread_api.py:1670) `client.threads.get_state()` |
| `POST /dashboard/api/threads/{thread_id}/cancel` | [routes.py:1845](../../agent/dashboard/routes.py:1845) | `cancel_dashboard_thread` | [thread_api.py:1505](../../agent/dashboard/thread_api.py:1505) `runs.list()`；[1513](../../agent/dashboard/thread_api.py:1513) `runs.cancel_many()` |
| `POST /dashboard/api/threads/{thread_id}/runs/{run_id}/cancel` | [routes.py:1826](../../agent/dashboard/routes.py:1826) | `proxy_dashboard_thread_run_cancel` | [thread_api.py:2158](../../agent/dashboard/thread_api.py:2158) 通过 HTTP 代理；成功后 [2178](../../agent/dashboard/thread_api.py:2178) `threads.update()` |
| `POST /dashboard/api/threads/{thread_id}/stream/events` | [routes.py:1878](../../agent/dashboard/routes.py:1878) | `proxy_dashboard_thread_stream_events` | [thread_api.py:2004](../../agent/dashboard/thread_api.py:2004)；事件字节流使用 `httpx` 转发，不是 SDK stream |
| `POST /dashboard/api/threads/{thread_id}/commands` | [routes.py:1899](../../agent/dashboard/routes.py:1899) | `proxy_dashboard_thread_commands` | [thread_api.py:2044](../../agent/dashboard/thread_api.py:2044)；[2070](../../agent/dashboard/thread_api.py:2070) `threads.get()`，[2127](../../agent/dashboard/thread_api.py:2127) `threads.update()`，命令本身用 `httpx` 转发 |
| `POST /dashboard/api/threads/{thread_id}/history` | [routes.py:1916](../../agent/dashboard/routes.py:1916) | `proxy_dashboard_thread_history` | [thread_api.py:2140](../../agent/dashboard/thread_api.py:2140) 使用 `httpx` 转发 |
| `GET /dashboard/api/threads/{thread_id}/stream` | [routes.py:1933](../../agent/dashboard/routes.py:1933) | `stream_dashboard_thread` | [thread_api.py:2205](../../agent/dashboard/thread_api.py:2205) `threads.join_stream()` |

因此 Dashboard 不能简单归类为“全部使用 SDK”：

```text
状态/权限/metadata/checkpoint -> Python SDK
commands、stream/events、history -> 部分使用 SDK，主体是 httpx 代理
stream -> Python SDK threads.join_stream
```

### 10.2 `plan_router`

挂载位置：[agent/api/app.py:52](../../agent/api/app.py:52)，路由定义：[plan_api.py:52](../../agent/dashboard/plan_api.py:52)。

| HTTP 接口 | Router 行号 | SDK 调用链 |
| --- | --- | --- |
| `GET /dashboard/api/plan/{thread_id}` | [plan_api.py:80](../../agent/dashboard/plan_api.py:80) | `_thread_metadata()` -> [plan_api.py:69](../../agent/dashboard/plan_api.py:69) `get_client()` -> [71](../../agent/dashboard/plan_api.py:71) `threads.get()`；计划内容由 [plan_store.py:58](../../agent/dashboard/plan_store.py:58) `store.get_item()` 读取 |
| `PUT /dashboard/api/plan/{thread_id}` | [plan_api.py:102](../../agent/dashboard/plan_api.py:102) | `save_plan_content()` -> [plan_store.py:86](../../agent/dashboard/plan_store.py:86) `store.put_item()`；随后写入 sandbox |
| `GET/POST/DELETE .../comments` | [plan_api.py:137](../../agent/dashboard/plan_api.py:137)、[148](../../agent/dashboard/plan_api.py:148)、[165](../../agent/dashboard/plan_api.py:165) | `plan_store` 使用 [181](../../agent/dashboard/plan_store.py:181) `store.search_items()`、[86](../../agent/dashboard/plan_store.py:86) `put_item()`、[216](../../agent/dashboard/plan_store.py:216) `delete_item()` |
| `POST /dashboard/api/plan/{thread_id}/approve` | [plan_api.py:184](../../agent/dashboard/plan_api.py:184) | [plan_api.py:336](../../agent/dashboard/plan_api.py:336) `dispatch_agent_run()` -> [dispatch.py:146](../../agent/dispatch.py:146) `client.runs.create()` |
| `POST /dashboard/api/plan/{thread_id}/reject` | [plan_api.py:222](../../agent/dashboard/plan_api.py:222) | 同样通过 [plan_api.py:336](../../agent/dashboard/plan_api.py:336) 派发 follow-up run |

### 10.3 `workflow_approval_router`

挂载位置：[agent/api/app.py:53](../../agent/api/app.py:53)，前缀定义：[workflow_approval_api.py:18](../../agent/dashboard/workflow_approval_api.py:18)。

| HTTP 接口 | Router 行号 | SDK 调用链 |
| --- | --- | --- |
| `GET /dashboard/api/workflow-approval/{thread_id}` | [workflow_approval_api.py:26](../../agent/dashboard/workflow_approval_api.py:26) | `_thread_metadata()` 查询 thread；`get_workflow_push_approvals()` 在 [workflow_approval.py:37-38](../../agent/dashboard/workflow_approval.py:37) 调用 `threads.get()` |
| `POST .../{fingerprint}/approve` | [workflow_approval_api.py:42](../../agent/dashboard/workflow_approval_api.py:42) | [workflow_approval.py:182](../../agent/dashboard/workflow_approval.py:182) `threads.update()`；随后 `_dispatch_followup()` -> `client.runs.create()` |
| `POST .../{fingerprint}/reject` | [workflow_approval_api.py:63](../../agent/dashboard/workflow_approval_api.py:63) | [workflow_approval.py:182](../../agent/dashboard/workflow_approval.py:182) `threads.update()`；不创建新 run |

### 10.4 三类 Webhook Router

Webhook route 负责验签和事件过滤，SDK 调用通常在后续 service/common 层。

| Router/接口 | Router 行号 | 后续 SDK 调用 |
| --- | --- | --- |
| `POST /webhooks/linear` | [linear_routes.py:11](../../agent/webhooks/linear_routes.py:11) | `process_linear_issue()` 在 [linear.py:241](../../agent/webhooks/linear.py:241) 调用 `dispatch_agent_run()`；最终 [dispatch.py:146](../../agent/dispatch.py:146) `runs.create()` |
| `GET /webhooks/linear` | [linear_routes.py:166](../../agent/webhooks/linear_routes.py:166) | 只返回验证状态，不调用 SDK |
| `POST /webhooks/slack` | [slack_routes.py:11](../../agent/webhooks/slack_routes.py:11) | Slack service [slack.py:114](../../agent/webhooks/slack.py:114) `threads.get()`；[slack.py:149](../../agent/webhooks/slack.py:149) 派发 run；忙碌消息还会进入 Store 队列 |
| `POST /webhooks/slack/interactivity` | [slack_routes.py:181](../../agent/webhooks/slack_routes.py:181) | 审批动作调用 workflow approval 的 `threads.update()`，Plan/继续执行动作调用 dispatch 的 `runs.create()` |
| `GET /webhooks/slack` | [slack_routes.py:391](../../agent/webhooks/slack_routes.py:391) | 只返回验证状态，不调用 SDK |
| `POST /webhooks/github` | [github_routes.py:11](../../agent/webhooks/github_routes.py:11) | 代表性事件在 [github.py:186](../../agent/webhooks/github.py:186)、[307](../../agent/webhooks/github.py:307)、[858](../../agent/webhooks/github.py:858) 派发 run；thread 创建/查询/更新集中在 [common.py:652](../../agent/webhooks/common.py:652) 及其后续 helper |

三类 webhook 的共同 SDK 入口是：

```text
Webhook route
    -> service/common
    -> dispatch_agent_run()
    -> create_durable_run()
    -> client.runs.create(thread_id, assistant_id, ...)
```

### 10.5 `health_router`

挂载位置：[agent/api/app.py:56](../../agent/api/app.py:56)。

| HTTP 接口 | Endpoint 行号 | SDK 情况 |
| --- | --- | --- |
| `GET /health` | [health.py:12](../../agent/api/health.py:12) | 不调用 SDK，只返回 `{"status": "healthy"}` |
| `POST /webhooks/run-complete` | [health.py:17](../../agent/api/health.py:17) | `handle_run_completion()` 在 [completion.py:212](../../agent/completion.py:212) 调用 `threads.get()`，在 [completion.py:233](../../agent/completion.py:233) 调用 `threads.update()` |

`run-complete` 还会调用回源通知和 Reviewer 结算逻辑；只有配置 `COMPLETION_WEBHOOK_URL` 与 `RUN_COMPLETE_WEBHOOK_SECRET` 后，LangGraph Server 才会实际投递这个接口。

## 11. 再讲一次：为什么创建 Run 有 SDK 和 HTTPX 两条路径？

先把最容易混淆的五个词拆开：

| 名词 | 它是什么 | 会不会创建 Run |
| --- | --- | --- |
| `run` | Agent 在一个 thread 上的一次执行记录 | 它是被创建出来的结果 |
| `client.runs.create()` | Python SDK 的“创建 Run”方法 | 会 |
| `run.start` | LangGraph 命令协议中的“启动 Run”命令 | 会 |
| `httpx` | Python 的底层 HTTP 客户端 | 自己不会，只是把请求送到 LangGraph Server |
| SSE `/stream/events` | Run 的事件观察通道 | 不会，只负责看正在发生什么 |

因此，下面两条路径最终创建的是**同一种 LangGraph Run**，差别只在“谁发起”和“如何把启动请求送到同一台 LangGraph Server”。

### 11.1 路径 A：后端自己决定启动 Agent

例如 GitHub、Slack、Linear、Schedule 或 Plan 审批发生时，浏览器并没有参与。Open SWE 后端已经拿到了完整业务信息，因此可以直接用 Python SDK：

```text
GitHub Webhook / Schedule / Plan approve
    -> Open SWE 后端 service
    -> create_durable_run(...)
    -> client.runs.create(...)
    -> LangGraph Server
    -> 创建 Run
```

对应代码：

```python
# agent/dispatch.py:146
run = await client.runs.create(thread_id, assistant_id, **create_kwargs)
```

`create_durable_run()` 相当于后端的统一开关。它把后端创建 Run 时必须一致的策略集中起来：

```python
multitask_strategy="interrupt"
durability="sync"
stream_resumable=True
webhook=COMPLETION_WEBHOOK_URL
```

这条路径适合“系统替用户发起任务”。例如 GitHub 发来评论，系统决定在该 GitHub thread 上启动 Agent；没有必要先绕到浏览器，再让浏览器提交一条命令。

### 11.2 路径 B：浏览器用户点击发送

Dashboard 中，用户点击发送后，前端 React SDK 调用的是：

```text
stream.submit(message)
    -> run.start 命令
```

它不会进入 Python 函数 `create_durable_run()`。实际路径是：

```text
浏览器
    -> TypeScript LangGraph SDK
    -> POST /dashboard/api/threads/{thread_id}/commands
    -> Dashboard 校验登录用户和 thread 权限
    -> Dashboard 补齐可信 model/repo/owner/config
    -> httpx POST /threads/{thread_id}/commands
    -> LangGraph Server 处理 run.start
    -> 创建 Run
```

这里的 `httpx` 不是第二个 Agent，也不是另一个运行时。它只是 FastAPI 后端用来发 HTTP 请求的工具，类似 Python SDK 内部也会做的网络请求。

```python
# agent/dashboard/thread_api.py:2090
url = f"{langgraph_url().rstrip('/')}/threads/{thread_id}/commands"

# agent/dashboard/thread_api.py:2097
response = await client.post(url, content=outgoing, headers=headers)
```

真正创建 Run 的仍然是同一个 LangGraph Server；只是这一次它收到的不是 Python SDK 的 `runs.create(...)`，而是 HTTP 命令 `run.start`。

### 11.3 为什么浏览器路径不直接调用 `create_durable_run()`？

技术上可以把浏览器输入翻译成 `create_durable_run()` 调用，但当前设计没有这么做，原因是 Dashboard 要兼容 LangGraph React SDK 的命令和事件协议。

浏览器除 `run.start` 外，还可能需要发送或处理：

```text
input.respond
interrupt
resume
事件 ID
SSE 重连和回放
```

若 Dashboard 把所有命令都重新翻译成 Python SDK 调用，就需要自己实现一遍命令语义、事件边界和错误格式。这容易出现“前端 SDK 认为 A，后端翻译成 B”的协议错配。

当前做法是：

```text
浏览器负责生成 LangGraph v2 command
Dashboard 负责安全检查和补齐可信参数
httpx 负责尽量原样转发 command/SSE 字节流
LangGraph Server 负责解释 command 并创建/控制 Run
```

这也是 `/stream/events` 使用 `httpx` 的原因。SSE 有 `id`、`event`、`data`、心跳和断线恢复等细节，Dashboard 直接透传字节流，避免解析后重新组装导致前端 SDK 的协议被改变。

### 11.4 SDK 在 Dashboard 路径中完全没有用吗？

不是。Dashboard 路径是“SDK 做资源操作，HTTPX 做协议透传”的混合模式：

```text
threads.get()/update()/get_state()/join_stream()
    -> Python SDK

/threads/{id}/commands
/threads/{id}/stream/events
/threads/{id}/history
    -> HTTPX 转发 LangGraph v2 协议
```

例如 `/commands` 在转发前用 SDK 查询 thread 是否存在：

```python
# agent/dashboard/thread_api.py:2070
thread = await langgraph_client().threads.get(thread_id)
```

命令成功后，再用 SDK 更新本项目的 thread metadata：

```python
# agent/dashboard/thread_api.py:2127
await langgraph_client().threads.update(...)
```

而 `/stream` 是后端自己消费流，因此可以直接使用 SDK：

```python
# agent/dashboard/thread_api.py:2205
stream = await langgraph_client().threads.join_stream(thread_id, ...)
```

### 11.5 最关键的理解方式

把它想成“同一栋楼的两个入口”：

```text
后端事件入口
    -> Python SDK runs.create
    -> LangGraph Server

浏览器交互入口
    -> HTTPX 转发 run.start
    -> LangGraph Server
```

两条路径最终都进入 LangGraph Server，也都创建同一种 `run_id`、绑定同一个 `thread_id`、写同一套 checkpoint。差别不是数据存在哪里，而是入口的协议不同。

### 11.6 当前代码需要留意的统一性问题

后端 `create_durable_run()` 明确设置 `durability="sync"`、`multitask_strategy="interrupt"`、`stream_resumable=True`。Dashboard 的命令补全代码目前明确设置的是：

```python
# agent/dashboard/thread_api.py:1392-1395
params["assistant_id"] = _ASSISTANT_ID
params.setdefault("stream_mode", list(_DASHBOARD_STREAM_MODES))
params.setdefault("stream_resumable", True)
```

该段代码没有显式写入 `durability` 和 `multitask_strategy`。这不自动证明浏览器路径行为错误，因为 LangGraph Server 可能有默认策略；但代码没有在 Dashboard 这一层保证它们与 `create_durable_run()` 完全一致。

如果未来要统一，正确目标不是强迫所有请求都调用同一个 Python 函数，而是让两条入口共享同一份 Run 策略：

```text
统一的 Run Contract
    thread_id
    assistant_id
    durability
    multitask_strategy
    stream_resumable
    metadata
    幂等键

后端入口 -> create_durable_run 使用该 Contract
浏览器入口 -> /commands 补全同一份 Contract 后再转发
```

## 12. 最终记忆

```text
dashboard_router
    浏览器 Dashboard 的总 API

plan_router
    Plan 展示、评论和审批

workflow_approval_router
    高风险 workflow 文件 push 审批

linear_webhook_router
    Linear -> Agent

slack_webhook_router
    Slack 消息/交互 -> Agent

github_webhook_router
    GitHub Issue/PR/Review/Push -> Agent 或 Reviewer

health_router
    健康探活 + LangGraph run 完成回调
```

一句话总结：**`include_router` 是路由注册；真正的调用方分为浏览器、外部 Webhook、LangGraph 回调和基础设施探活。接口是否执行，要看对应入口是否配置并产生请求。**

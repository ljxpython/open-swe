# `agent/webhooks` 通俗说明

## 一句话理解

`agent/webhooks/` 是 Open SWE 的“接线员”。GitHub、Linear、Slack 有新事件时，都会先打到这里；它验证消息真伪、过滤无关事件、找到对应仓库和用户，然后在后台启动或续接一个 LangGraph Agent 运行。

它不直接写业务代码，也不直接让模型回答。它负责把外部平台的一次通知，翻译成 Agent 看得懂的上下文和配置。

```text
GitHub / Linear / Slack
          |
          v
FastAPI webhook 路由（验签、快速过滤）
          |
          v
渠道处理器（整理上下文、选择仓库和用户）
          |
          v
dispatch_agent_run(...)
          |
          v
LangGraph 主 Agent / Reviewer
          |
          v
回复原来的 GitHub PR、Linear Issue 或 Slack Thread
```

## 目录里每个文件做什么

| 文件 | 通俗作用 | 怎么被使用 |
|---|---|---|
| [`__init__.py`](../agent/webhooks/__init__.py) | Python 包标记文件，目前没有业务逻辑。 | 让其他模块可以用 `agent.webhooks.xxx` 导入子模块。 |
| [`common.py`](../agent/webhooks/common.py) | 三个平台共用的“后勤中心”：环境配置、验签、稳定线程 ID、仓库选择、白名单、线程元数据、GitHub Token 缓存、Reviewer 辅助等。 | 6 个路由/处理器文件都通过 `common.xxx` 调用它。把共享能力集中起来，避免 GitHub、Linear、Slack 各写一遍。 |
| [`github_routes.py`](../agent/webhooks/github_routes.py) | GitHub 的 HTTP 门卫。 | FastAPI 把 `POST /webhooks/github` 交给它；它验签、检查事件类型和 `@open-swe` 提及，再把真正工作放进后台任务。 |
| [`github.py`](../agent/webhooks/github.py) | GitHub 的业务处理器。 | 由 `github_routes.py` 的后台任务调用；负责 Issue/PR 评论、自动评审、评审回复、push 和 PR 状态变化。 |
| [`linear_routes.py`](../agent/webhooks/linear_routes.py) | Linear 的 HTTP 门卫。 | FastAPI 把 `POST /webhooks/linear` 交给它；只接受用户新建、且提及 `@openswe` 的评论。 |
| [`linear.py`](../agent/webhooks/linear.py) | Linear 的业务处理器。 | 由 `linear_routes.py` 的后台任务调用；补齐 issue 内容和评论，拼提示词后启动主 Agent。 |
| [`slack_routes.py`](../agent/webhooks/slack_routes.py) | Slack Event API 和交互按钮的 HTTP 门卫。 | FastAPI 把 `POST /webhooks/slack` 和 `POST /webhooks/slack/interactivity` 交给它；处理 mention、私信、表情反馈、计划审批和工作流审批。 |
| [`slack.py`](../agent/webhooks/slack.py) | Slack 的业务处理器。 | 由 `slack_routes.py` 的后台任务调用；读取线程上下文、图片和用户身份，决定“立即打断运行”还是“排队跟进”，再启动 Agent。 |

## 为什么拆成“路由文件 + 处理器文件”

例如 GitHub 有 `github_routes.py` 和 `github.py` 两个文件，Slack、Linear 也一样。它们不是重复，而是职责不同：

```text
github_routes.py
  收 HTTP 请求
  -> 验签
  -> 判断是否关心这个事件
  -> 立刻返回 accepted
  -> 投递后台任务

github.py
  后台慢活
  -> 查询 PR / 评论 / 用户身份
  -> 拼 Agent 提示词
  -> 更新 LangGraph thread 元数据
  -> 启动主 Agent 或 Reviewer
```

外部平台一般要求 webhook 很快回复 HTTP 200。若在路由函数里等待模型、克隆仓库或查很多 API，平台可能超时重试，导致重复执行。因此路由只做必要校验，耗时工作交给 FastAPI 的 `BackgroundTasks`。

## `common.py`：三种渠道共用的底座

`common.py` 不是一个独立服务，而是其余文件的共享工具集合。主要包含下面几类能力。

| 能力 | 关键内容 | 解决的问题 |
|---|---|---|
| 配置读取 | `GITHUB_WEBHOOK_SECRET`、`SLACK_SIGNING_SECRET`、`LINEAR_WEBHOOK_SECRET`、仓库白名单、LangGraph URL 等。 | 不把密钥和部署差异散落在每个渠道文件里。 |
| 验签 | `verify_github_signature`、`verify_slack_signature`、`verify_linear_signature`。 | 防止任何人伪造 HTTP 请求，冒充 GitHub/Slack/Linear 让 Agent 执行任务。 |
| 稳定线程 ID | `generate_thread_id_from_issue()`、`generate_thread_id_from_github_issue()`、`generate_reviewer_thread_id()`，Slack 则复用 `utils/thread_ids.py`。 | 同一个 Issue、PR 或 Slack Thread 的后续消息永远路由回同一个 LangGraph thread。 |
| 仓库选择 | 从文本提取 `repo:owner/name`，再按个人默认仓库、Linear 团队/项目映射、团队默认仓库逐级回退。 | 用户没有明确说仓库时，仍能尽量找到正确目标。 |
| 权限门禁 | `_is_repo_allowed()`、自动评审开关、公开仓库组织成员门禁。 | 避免任何仓库、任何外部用户都能触发具有代码权限的 Agent。 |
| 线程元数据 | `upsert_agent_thread_owner_metadata()`、PR 状态更新、plan mode 读写。 | 让 Dashboard、后续 webhook 和工具知道这条线程来自哪里、属于谁、对应什么仓库/PR。 |
| 调度 | `_trigger_or_queue_run()`、`dispatch_agent_run`。 | 把已整理好的消息和 `configurable` 送到 LangGraph；需要时处理正在运行的线程。 |
| Reviewer 支持 | reviewer thread 元数据、PR 元数据读取、check run、finding 交互辅助。 | GitHub 自动评审和人工回复能复用同一份线程和结果状态。 |

## GitHub：Issue、PR 评论、自动评审和反馈

### 入口：`github_routes.py`

FastAPI 在 [`agent/api/app.py`](../agent/api/app.py) 注册了它的路由：

```text
POST /webhooks/github
```

收到请求后，`github_webhook()` 按下面顺序处理：

1. 读取原始 body，用 `X-Hub-Signature-256` 校验 GitHub 签名；失败直接返回 401。
2. 读取 `X-GitHub-Event`，不支持的事件立刻忽略。
3. 解析 JSON，识别它是 Issue、Issue 评论、PR 评论、PR review、PR 状态，还是 push。
4. 检查允许仓库、自动评审开关、公开仓库成员门禁，以及是否出现 `@open-swe` / `@openswe`。
5. 满足条件后用 `BackgroundTasks` 调用 `github.py` 的处理函数，并立即返回 `accepted`。

注意：不是所有 GitHub 事件都要求提及。已开启自动评审的仓库，在 PR `opened` / `ready_for_review` 时可直接触发 reviewer；PR close/reopen、push 也会用来维护 reviewer watch 或判断是否需要重新评审。

### 处理器：`github.py`

| 主要函数 | 通俗作用 |
|---|---|
| `trigger_pr_review_from_ref()` | 根据 `owner/repo`、PR 编号或分支，创建/继续一个专用 reviewer thread。Dashboard 的 Review API 和 `request_pr_review` 工具也能直接调用它。 |
| `process_github_pr_ready()` | PR 刚打开或标记为 ready 时，满足仓库开关和权限条件则自动启动首次评审。 |
| `process_github_pr_close()` | PR 关闭、重开、转换 draft 等状态变化时，更新 reviewer watch / 线程状态。 |
| `process_github_push_event()` | 有 push 时查看已被 reviewer 关注的 PR，必要时评估是否该再评审。 |
| `process_github_pr_comment()` | 用户在 PR 主评论、行内评论或 review 中提及 Open SWE 时，把 PR、差异和评论上下文整理为主 Agent 的新任务。 |
| `process_github_review_finding_reply()` | 人类回复某条 reviewer finding 时，把回复记录为 finding 交互，而不是误当成普通 Agent 任务。 |
| `process_github_issue()` | 用户在普通 GitHub Issue 或 Issue 评论中提及 Open SWE 时，创建/续接主 Agent 线程。 |

### GitHub 完整链路

```text
用户在 PR 评论：@open-swe 修复这个测试
       |
       v
POST /webhooks/github
       |
       v
github_routes.github_webhook()
  验签、事件过滤、提及检查、权限门禁
       |
       v
BackgroundTasks: github.process_github_pr_comment()
  拉取 PR/评论上下文，生成稳定 thread_id
  写入 repo、PR、触发用户等 thread metadata
       |
       v
dispatch_agent_run(..., source="github")
       |
       v
主 Agent 在对应沙箱中处理，最后回复 PR
```

自动评审的链路不同：`pull_request opened/ready_for_review` -> `process_github_pr_ready()` -> `trigger_pr_review_from_ref()` -> `reviewer` 图。Reviewer 是只读的评审 Agent，不是主编码 Agent。

## Linear：从评论创建编码任务

### 入口：`linear_routes.py`

路由是：

```text
POST /webhooks/linear
GET  /webhooks/linear  （安装时的健康验证）
```

`linear_webhook()` 只接受以下情况：

- 签名正确；
- 事件类型是 `Comment`；
- 动作是用户新建评论 `create`；
- 不是机器人评论，也不是 Open SWE 自己发的回执；
- 评论中出现 `@openswe`。

仓库的选择优先级是：评论显式仓库 -> 用户 Dashboard 默认仓库 -> Linear 团队/项目映射 -> 团队默认仓库。找不到仓库或仓库不在允许列表，就不启动 Agent。

### 处理器：`linear.py`

`process_linear_issue()` 做的是“把 Linear ticket 变成一份可执行任务单”：

1. 给触发评论加 👀，表示已经收到。
2. 用 issue ID 生成稳定的 LangGraph `thread_id`。
3. 补拉完整 issue，读取标题、描述、项目、团队和评论。
4. 只保留上次 Agent 回复后的用户新评论，避免反复把旧对话送进模型。
5. 提取描述/评论里的图片；当前模型不支持视觉时，切换到预设视觉模型。
6. 从 Linear 用户邮箱映射 GitHub login，让 PR 尽量以触发用户身份创建。
7. 写入线程元数据并调用 `dispatch_agent_run()`。
8. 在 Linear issue 回写 LangSmith trace 链接，方便排查本次运行。

```text
Linear 评论：@openswe 修复登录失败，repo:acme/app
       |
       v
linear_routes.py 验签和过滤
       |
       v
linear.py:process_linear_issue()
  补全 Issue、评论、图片、GitHub 用户、仓库
       |
       v
主 Agent thread（source=linear）
       |
       v
Agent 通过 linear_comment / PR 工具反馈结果
```

## Slack：提及、私信、连续对话和审批

### 入口：`slack_routes.py`

Slack 有三个入口：

| 路由 | 用途 |
|---|---|
| `POST /webhooks/slack` | Slack Event API：`app_mention`、私信、线程跟进消息、表情反馈。 |
| `POST /webhooks/slack/interactivity` | Slack 按钮/下拉选项：计划批准、工作流文件 push 批准、快捷选项。 |
| `GET /webhooks/slack` | 安装时的健康验证。 |

`slack_webhook()` 先验证 `X-Slack-Signature` 和时间戳，支持 Slack 的 `url_verification` challenge。之后它会：

- 收集 reviewer finding 的指定表情，放到后台同步反馈；
- 只处理 `app_mention`、与机器人私信、计划待批准时的自然语言回复，或双方线程中的无提及跟进；
- 忽略机器人消息和自己发出的消息，防止自我触发死循环；
- 识别 `docs-plz` 渠道并进行专用转交；
- 找到 Slack 线程对应的仓库配置，然后后台调用 `process_slack_mention()`。

`slack_interactivity()` 不让任何人随便审批：它会根据 Slack channel + thread timestamp 算出 thread ID，再验证点击者是否是这个线程的请求人，才允许批准计划或 workflow push。

### 处理器：`slack.py`

`process_slack_mention()` 是 Slack 侧的总入口，真正逻辑在 `_process_slack_mention_impl()`。它会：

1. 生成稳定 thread ID：同一个 Slack thread 永远进入同一个 Agent 对话。
2. 读取整个 Slack thread，截取合适的上下文，而不是只拿最后一句。
3. 查询触发用户的 Slack profile，关联邮箱和 GitHub login。
4. 解析线程里的 Slack 链接和图片，必要时使用视觉 fallback 模型。
5. 检查该用户是否拥有可用 GitHub OAuth Token；不是 bot-only 部署时，未绑定/授权失效会引导用户去 Dashboard 重新登录。
6. 将 Slack thread、仓库、用户、plan mode 等信息写入 `configurable` 和 thread metadata。
7. 决定如何续接运行：显式 `@mention`、私信和首条消息会立即派发；未提及的跟进消息若 Agent 正在忙，则进入队列，等待下一次模型调用前合并读取。

### Slack 完整链路

```text
Slack Thread 中 @Open SWE：修复这个报错
       |
       v
slack_routes.slack_webhook()
  验签、过滤机器人事件、检查是否应处理
       |
       v
BackgroundTasks: slack.process_slack_mention()
  读取线程上下文、用户、图片、仓库、授权
       |
       +--> 忙且是普通跟进：queue_message_for_thread()
       |
       +--> 否则：dispatch_agent_run(..., source="slack")
       |
       v
主 Agent 运行并通过 Slack 工具回到原线程
```

这里“排队”和“立即打断”是刻意区分的：用户显式 @ 机器人通常是紧急的新指令，应该 interrupt 当前运行；普通连续对话先排队，减少不断打断导致的任务抖动。

## 三个平台最终都如何启动 Agent

三条入口最终都会准备两个东西：

```text
thread_id
  一个稳定的会话/任务编号。
  同一个外部对象的后续事件复用它。

configurable
  本次运行的上下文配置，例如：
  repo、github_login、user_email、source、slack_thread、linear_issue、PR 信息、模型覆盖等。
```

然后调用：

```python
dispatch_agent_run(thread_id, content_blocks, configurable, source=...)
```

`content_blocks` 是给模型看的任务内容，可同时包含文本和图片；`configurable` 是程序读取的结构化运行参数。两者分开，模型上下文和系统配置就不会混成一团。

## 这层做了哪些安全和稳定性保护

| 保护 | 在哪里 | 为什么需要 |
|---|---|---|
| 请求验签 | 三个 `*_routes.py` | 防止伪造 webhook 触发 Agent。 |
| 快速返回 + 后台处理 | 三个 `*_routes.py` | 防止外部平台超时后重试，造成重复任务。 |
| 事件和动作白名单 | GitHub/Linear/Slack 路由 | 忽略无关事件，降低误触发面。 |
| `@open-swe` 提及检查 | GitHub、Linear、Slack | 默认不监听所有内容，只有明确请求才执行。Slack 私信和受控跟进是例外。 |
| 仓库允许列表/自动评审开关 | `common.py` | 限制能被操作或自动评审的仓库。 |
| 公开仓库组织成员门禁 | `common.py` | 公开 PR 不能让陌生人随意触发具有权限的 Agent。 |
| 忽略机器人消息 | Linear/Slack 路由 | 防止 Agent 发出的回复再次触发 Agent。 |
| 用户身份和 Token 检查 | `linear.py`、`slack.py`、`common.py` | 确保需要以用户身份创建 PR 的任务有对应 GitHub 授权。 |
| 审批者校验 | `slack_routes.py` | 只有原请求人可以批准计划或 workflow 文件 push。 |
| 稳定线程 ID | `common.py`、`utils/thread_ids.py` | 同一任务续接同一工作区和上下文，而不是每条消息新开一份。 |

## 读代码时的推荐顺序

1. [`agent/api/app.py`](../agent/api/app.py)：先确认这些路由如何挂到 FastAPI。
2. 任意一个 `*_routes.py`：看 HTTP 事件怎么被验签和筛选。
3. 对应的 `github.py`、`linear.py` 或 `slack.py`：看事件如何变成提示词和 Agent 运行。
4. [`common.py`](../agent/webhooks/common.py)：回头查共享的线程、仓库、权限和调度逻辑。
5. [`agent/dispatch.py`](../agent/dispatch.py)：继续看 `dispatch_agent_run()` 怎样向 LangGraph 发起真正的运行。

## 常见误解

1. Webhook 文件不是 Agent 本体。它们只是接收和转换外部事件，真正推理和编码发生在 `agent/server.py` 生成的主图，评审发生在 `agent/reviewer.py`。
2. `BackgroundTasks` 不是新的 Agent 运行时。它只是 FastAPI 用来把慢操作挪到 HTTP 响应之后执行；真正的长任务仍由 LangGraph run 承担。
3. Slack 的每句话不一定都会新开运行。同一个线程使用稳定 thread ID；忙碌时的普通跟进会排队，显式提及才会打断。
4. GitHub 的自动评审不依赖 `@open-swe`。仓库开启自动评审后，PR 打开或 ready for review 就可能触发；普通编码任务评论仍需明确提及。
5. `common.py` 名字很泛，但它不是无关杂物堆。它承载的是三条 webhook 链路都必须一致执行的安全、身份、线程和仓库决策。


# 第 7 章：GitHub、Slack、Linear Webhook 与确定性 thread_id

## 学习目标

本章解释外部事件如何进入 Open SWE，以及为什么不同平台的事件最终可以复用同一个 LangGraph thread 和同一个 durable dispatch contract。读完后，你应该能够：

- 追踪 GitHub、Slack、Linear webhook 的签名验证、事件过滤和后台任务分发。
- 解释三个平台如何把外部会话映射为稳定的 `thread_id`。
- 区分 Agent 线程、Reviewer 线程和平台回复上下文。
- 说明 `dispatch_agent_run()` 的 `interrupt`、`durability="sync"` 和 `stream_resumable=True` 各解决什么问题。
- 理解 Slack 为什么同时存在“显式 @mention 立即 interrupt”和“未标记追问进入 Store queue”两条路径。

本章只做本地源码和 mock/fake client 测试，不发送真实 GitHub、Slack、Linear webhook，也不调用外部模型或生产服务。

## 1. 外部事件的共同骨架

三个入口虽然 payload 不同，但都遵循同一条骨架：

```text
外部平台 POST
  -> 原始 body + 签名校验
  -> 事件类型/动作/机器人/仓库门禁
  -> 解析用户、仓库、会话上下文
  -> 生成确定性 thread_id
  -> 写入 thread metadata
  -> dispatch_agent_run(thread_id, content, configurable)
  -> LangGraph durable run
  -> Agent 工具/模型循环
  -> 回源平台回复或 Dashboard 观察
```

这里有两个重要边界：

1. **Webhook route 不执行 Agent。** route 只做快速校验和 `BackgroundTasks.add_task()`，避免平台因为等待模型而超时。
2. **thread_id 是路由键，不是用户可见标题。** 标题、仓库、触发人和 Slack permalink 都放在 metadata/configurable 中，ID 负责把后续事件送回同一条线程。

## 2. 入口时序图

![GitHub、Slack、Linear Webhook 到 Agent](architecture/premium/png/03-webhook-sequence.png)

[打开可编辑 Draw.io 源文件](architecture/premium/03-webhook-sequence.drawio) · [打开自包含 HTML 查看器](architecture/premium/html/03-webhook-sequence.html)

图从左向右看参与者，从上向下看时间。实线箭头表示同步调用，虚线箭头表示快速确认或异步回复。图中“确定性 thread_id + user context”是所有平台汇合的关键节点；后续的 LangGraph Runtime 不关心事件来自哪个平台，只读取 `source` 和各平台的 configurable。

### 图元素到源码的映射

| 图元素 | 源码位置 | 关键符号 | 图中行为 |
| --- | --- | --- | --- |
| GitHub route | `agent/webhooks/github_routes.py:11-155` | `github_webhook` | 验证签名、过滤 event/action/tag、挂后台任务 |
| Slack route | `agent/webhooks/slack_routes.py:11-167` | `slack_webhook` | 验证 Slack 签名，处理 challenge、mention、DM 和 reaction |
| Linear route | `agent/webhooks/linear_routes.py:11-157` | `linear_webhook` | 验证签名，只接受 Comment/create/@openswe 事件 |
| GitHub thread ID | `agent/utils/github_comments.py:84-90`、`agent/webhooks/github.py:656-680` | `get_thread_id_from_branch` | 优先复用分支内 UUID，否则用 owner/repo/pr 的 UUID5 |
| GitHub issue ID | `agent/webhooks/common.py:490-496` | `generate_thread_id_from_github_issue` | SHA-256(`github-issue:<issue_id>`) 转 UUID 形式 |
| Slack thread ID | `agent/utils/thread_ids.py:5-9` | `generate_thread_id_from_slack_thread` | MD5(`channel_id:thread_ts`) 转 UUID |
| Linear issue ID | `agent/webhooks/common.py:474-487` | `generate_thread_id_from_issue` | SHA-256(`linear-issue:<issue_id>`) 转 UUID 形式 |
| 统一 dispatch | `agent/dispatch.py:113-181` | `create_durable_run`、`dispatch_agent_run` | `interrupt`、`sync`、`stream_resumable`，统一创建/恢复 Run |
| Slack busy 分支 | `agent/webhooks/slack.py:124-157,584-630` | `_dispatch_or_queue_slack_run` | 显式 mention interrupt，未标记追问进入 Store queue |

### 短接线图

```text
GitHub: X-Hub-Signature-256
Slack:  X-Slack-Signature + timestamp
Linear: Linear-Signature
          |
          v
route -> event gate -> thread_id -> metadata -> dispatch_agent_run
                                                    |
                    +-------------------------------+----------------+
                    |                                                |
             interrupt + sync                              Slack untagged follow-up
                    |                                                |
             current Run resumes                         Store queue -> before_model
```

## 3. 签名和事件门禁：先拒绝，再排队

### 3.1 GitHub

`github_webhook()` 读取原始 body 和 `X-Hub-Signature-256`，使用 HMAC-SHA256 计算 `sha256=<digest>`，通过 `hmac.compare_digest()` 比较。`GITHUB_WEBHOOK_SECRET` 缺失也会拒绝，不会因为“开发方便”而放行未签名请求。

事件门禁分两层：

- 支持的 event 类型限定为 `issue_comment`、`issues`、`pull_request`、`pull_request_review_comment`、`pull_request_review`、`push`。
- action、仓库 allowlist、评论中是否包含 `@openswe`/`@open-swe`、公开仓库组织门禁分别过滤。

PR 的 `opened`/`ready_for_review` 可能进入 reviewer auto-review；PR comment 则进入主 Agent；review finding 回复会计算 reviewer thread ID 并进入 reviewer graph。也就是说，同一个 GitHub webhook endpoint 内部已经存在多个 graph 选择分支。

### 3.2 Slack

Slack 使用签名基串 `v0:<timestamp>:<raw_body>`，并检查时间戳防重放。route 还处理 Slack URL verification challenge、reaction feedback、bot message、DM、app mention 和 plan reply。普通频道里不满足这些条件的消息直接返回 ignored，不会创建 thread。

Slack route 将事件放入后台任务：

```text
slack_webhook()
  -> verify_slack_signature()
  -> normalize event
  -> get_slack_repo_config()
  -> BackgroundTasks.add_task(process_slack_mention, event_data, repo_config)
  -> HTTP 立即返回 accepted
```

### 3.3 Linear

Linear route 使用 `Linear-Signature` 做 HMAC-SHA256 校验，只处理 `type=Comment`、`action=create`、非 bot、正文包含 `@openswe` 的事件。仓库解析优先看评论中的 `repo:owner/name`，再看用户 profile、team/project 映射和团队默认仓库；没有仓库或不在 allowlist 中就 ignored。

## 4. 确定性 thread_id：让“同一个外部会话”回到同一条线程

### 4.1 GitHub PR 分支

Open SWE 创建的分支通常携带 UUID，因此评论事件优先调用 `get_thread_id_from_branch()` 从 branch name 提取 UUID。对不是 Open SWE 创建、但需要接管的 PR，代码使用：

```text
uuid5(NAMESPACE_URL, "<owner>/<repo>/pr/<number>")
```

并把 `branch_name` 写回 thread metadata。这样后续评论即使分支名没有 Open SWE UUID，也能稳定回到同一个 PR thread。

### 4.2 GitHub Issue 与 Linear Issue

两者使用带来源前缀的 SHA-256，避免不同系统恰好使用同一个外部 ID 时碰撞：

```python
github_issue_thread = uuid_from_sha256(f"github-issue:{issue_id}")
linear_issue_thread = uuid_from_sha256(f"linear-issue:{issue_id}")
```

这不是安全哈希用途，而是稳定命名空间用途；真正的 webhook 身份校验仍由 HMAC 完成。

### 4.3 Slack channel + thread timestamp

Slack 的稳定键是 `channel_id:thread_ts`，再把 MD5 hex 解析成 UUID。根消息的 `ts` 被当作 `thread_ts`，回复则使用原始 `thread_ts`，所以整个 Slack 对话共享一个 thread ID。Slack 的 thread timestamp 是平台会话语义，不能只用 user ID，否则一个用户的多条任务会错误合并。

### 4.4 为什么不能每次随机创建 UUID

如果每条评论都生成随机 ID，会产生三个问题：

1. 每条消息都创建新 sandbox/thread，Agent 看不到之前的上下文。
2. Dashboard 无法把外部来源聚合成一条可恢复会话。
3. completion webhook、run mapping、GitHub token cache 和 Slack trace reply 无法通过一个稳定键关联。

确定性 ID 让“事件重试”成为幂等路由问题，而不是重复创建资源的问题。

## 5. Thread metadata：ID 只是索引，metadata 才是上下文

`upsert_agent_thread_owner_metadata()` 会把以下信息写入 LangGraph thread metadata：

| 字段 | 用途 |
| --- | --- |
| `source` | `github`、`slack`、`linear`，供 Dashboard 和日志展示 |
| `github_login` / `triggering_user_email` | 解析用户 token、归属线程、审计来源 |
| `repo`、`repo_owner`、`repo_name` | 选择 sandbox 仓库和默认 repo |
| `title` | Dashboard 首次展示标题，已有标题不会被后续事件覆盖 |
| `source_context` | PR、Issue、Slack channel/thread、Linear issue URL 等平台上下文 |
| `pr_url` / `pr_state` | PR 追踪与 Dashboard 状态 |
| `latest_run_id` / `latest_run_status` | 显示当前 Run 和跨 UI/平台查询 |

metadata 的关键特点是“第一次创建 + 后续增量更新”：标题通常 first message wins，`updated_at_ms` 每次更新，source context 允许补充 Slack permalink 或 PR 状态。这也是 Dashboard 能列出由 webhook 创建的 thread 的原因。

## 6. 统一 dispatch contract：interrupt、sync、resumable

`dispatch_agent_run()` 把所有平台的输入规范化为：

```python
input = {"messages": [{"role": "user", "content": content}]}
config = {"configurable": configurable}
metadata = agent_version_metadata
```

随后 `create_durable_run()` 固定传给 LangGraph：

| 参数 | 语义 | 解决的问题 |
| --- | --- | --- |
| `multitask_strategy="interrupt"` | 同 thread 的新输入中断正在运行的 Run，再从 checkpoint 恢复 | 外部追问不需要进程内锁，也避免并行修改 sandbox |
| `durability="sync"` | 每一步先同步 checkpoint | worker 崩溃/回收后能从最近步骤恢复 |
| `stream_resumable=True` | 保留可重连事件流 | Dashboard 晚些打开也能看到 webhook Run 正在运行 |
| `if_not_exists="create"` | thread 不存在时创建 | 首个外部事件可以懒创建 thread |
| `prepare_run_id` | 每次运行生成唯一准备阶段 ID | middleware 运行快照和 turn 追踪不冲突 |

这就是外部 webhook 与 Dashboard 命令代理的主要区别：Dashboard 走 `/commands`，由服务端重建 command schema；Webhook 已在 Python 内部构建 configurable，直接走 durable dispatch helper。

## 7. Slack 的特殊分支：显式中断，未标记合并

Slack 不是简单地“所有消息都 interrupt”。`_dispatch_or_queue_slack_run()` 的规则是：

| 消息类型 | busy thread 时行为 | 原因 |
| --- | --- | --- |
| 显式 `@open-swe` mention | `dispatch_agent_run()`，立即 interrupt | 用户明确要求 Agent 立刻处理 |
| DM | 立即 interrupt | DM 天然只指向 bot |
| 首次 mention | 启动 Run | thread 尚不存在 |
| 未标记双人 thread 追问 | Store queue | 连续补充信息可合并，减少 Run 抖动 |
| plan ready 的 owner 回复 | 先尝试审批 plan | 这是 workflow approval，不是普通对话 |

未标记追问进入和 Dashboard 相同的 `queue_message_for_thread()`，由 `check_message_queue_before_model()` 在下一次模型调用前 FIFO 注入。显式 mention 则不排队，利用 `interrupt` 的 checkpoint 恢复语义。这个混合策略同时满足低延迟和消息合并。

## 8. 三个平台的完整调用链

### 8.1 GitHub PR comment

1. GitHub POST `/webhooks/github`。
2. route 验证 HMAC，检查 event/action 和 `@open-swe` 标签。
3. `process_github_pr_comment()` 提取 repo、PR、branch、登录名和 comment。
4. branch 中有 UUID 就复用；否则使用 owner/repo/pr 的 UUID5。
5. 读取/刷新线程 GitHub token，给评论加 👀 reaction。
6. 拉取最近一次 Open SWE 标签之后的评论，构造 PR prompt。
7. `upsert_agent_thread_owner_metadata()` 写入 source/repo/user/PR metadata。
8. `dispatch_agent_run()` 启动或 interrupt 当前 thread。
9. Agent 通过 `slack_thread_reply`、GitHub comment 工具或 PR 工具回源。

### 8.2 Slack mention

1. Slack POST `/webhooks/slack`。
2. route 验签、过滤 bot/event，确定 `channel_id` 和根 `thread_ts`。
3. `get_slack_repo_config()` 按 thread metadata、频道描述、用户 profile、团队默认值解析 repo。
4. `process_slack_mention()` 生成 `md5(channel_id:thread_ts)` thread ID。
5. 拉取 thread history，构造带上下文的 prompt；解析用户 GitHub login/token。
6. 写入 Slack source context 和 owner metadata。
7. 显式 mention 走 `dispatch_agent_run()`；未标记 busy follow-up 走 Store queue。
8. completion webhook 或 Agent 工具把回复发回同一 Slack thread，并保留 Dashboard URL。

### 8.3 Linear comment

1. Linear POST `/webhooks/linear`。
2. route 验签，只接受 Comment/create/@openswe 且非 bot。
3. 解析 repo 配置，读取完整 issue、评论历史、图片 URL 和触发人。
4. `generate_thread_id_from_issue(issue_id)` 生成稳定 ID。
5. 构建 `linear_issue` configurable，按用户模型能力选择图片 fallback。
6. 写入 source/repo/issue/user metadata。
7. `dispatch_agent_run()` 发送 issue prompt。
8. `post_linear_trace_comment()` 将 thread/run 关联回 Linear，后续评论继续落到同一 ID。

## 9. 与源码一致的伪代码

### 9.1 Route 层：快速验签和后台任务

```python
async def github_webhook(request, background_tasks):
    body = await request.body()
    if not verify_github_signature(body, header, secret=GITHUB_WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid signature")

    payload = json.loads(body)
    if event_type not in SUPPORTED_EVENTS:
        return {"status": "ignored"}
    if not tag_or_action_gate(payload):
        return {"status": "ignored"}

    background_tasks.add_task(process_github_pr_comment, payload, event_type)
    return {"status": "accepted"}
```

### 9.2 统一 durable dispatch

```python
async def dispatch_agent_run(thread_id, content, configurable, *, source):
    return await client.runs.create(
        thread_id,
        "agent",
        input={"messages": [{"role": "user", "content": content}]},
        config={"configurable": with_prepare_run_id(configurable)},
        metadata=AGENT_VERSION_METADATA,
        multitask_strategy="interrupt",
        durability="sync",
        if_not_exists="create",
        stream_resumable=True,
    )
```

### 9.3 Slack busy 分支

```python
async def dispatch_or_queue_slack(thread_id, content, configurable, *, explicit, first):
    if not explicit and not first and await thread_is_busy(thread_id):
        await queue_message_for_thread(thread_id, content)
        return None
    return await dispatch_agent_run(
        thread_id, content, configurable, source="slack"
    )
```

## 10. 最小可运行验证

优先运行稳定 ID、dispatch 参数和 webhook 过滤相关测试：

```bash
uv run pytest -q tests/agent/test_dispatch.py
uv run pytest -q tests/slack/test_slack_context.py
uv run pytest -q tests/webhooks/test_linear_webhook_author.py
uv run pytest -q tests/slack/test_slack_webhook_errors.py
uv run pytest -q tests/github/test_github_feedback.py
uv run pytest -q tests/github/test_repo_extraction.py
```

这些测试使用 fake LangGraph client、mock webhook payload 和 monkeypatch，不会访问真实平台。重点观察：相同外部键反复计算得到同一个 `thread_id`；签名错误返回 401；不支持 event/action/tag 被 ignored；dispatch 参数包含 `interrupt`、`sync` 和 `stream_resumable`。

## 11. 常见误区与反例

1. **Webhook route 里直接 `await dispatch_agent_run()` 并等待模型完成。** 平台会因 HTTP 超时重试，造成重复事件；route 应快速验签并把处理放到后台任务。
2. **只用外部 issue number 生成 thread ID。** 不同仓库或不同平台会发生碰撞，必须带来源和命名空间。
3. **把 Slack `event.ts` 当作所有消息的 thread ID。** 回复消息的 `event.ts` 每次都不同，应使用根消息的 `thread_ts`。
4. **所有 Slack 追问都 interrupt。** 未标记的连续补充会造成频繁中断；项目只对显式 mention/DM 立即 interrupt。
5. **Webhook 事件没有写 thread metadata。** Run 虽然执行成功，但 Dashboard 无法按 owner/source/repo 列出和授权这个线程。
6. **把 `stream_resumable` 当作 Dashboard 专属配置。** Webhook Run 也要保留事件流，否则用户稍后打开 UI 时看不到 running 生命周期。
7. **用随机 UUID 解决重复事件。** 重试会创建多个 thread 和 sandbox；确定性 ID 才能让重试回到原会话。
8. **相信外部评论里的指令标签。** GitHub/Linear/Slack 文本是 untrusted data，必须经过 prompt wrapper/sanitization，不能把评论内容当系统指令。

## 12. 检查题与改造练习

1. 手算 `generate_thread_id_from_slack_thread("C123", "1700.1")` 两次，说明为什么第二次 Slack retry 不会新建 thread。
2. 阅读 GitHub route，列出一个 `issue_comment` 被 ignored 的三种原因，并指出分别在哪一层判断。
3. 修改一个 fake dispatch client，断言 GitHub、Linear 和 Slack 的 Run 创建都携带 `multitask_strategy="interrupt"`。
4. 为 Slack 未标记 busy follow-up 写测试，证明它调用 Store queue 而不是 `runs.create`。
5. 设计一个新的 webhook 来源：列出稳定键、签名算法、metadata 字段和 graph/assistant 选择，不修改现有 dispatch contract。

## 13. 扩展边界与下一步

项目当前已经覆盖 GitHub、Slack、Linear 三种 webhook，但没有把所有平台协议统一成一个公共事件 schema；它只统一了 thread/run dispatch contract。项目外常见扩展包括：

- 使用事件去重表记录 provider event ID，进一步抵抗平台重复投递。
- 使用队列/消息代理替代 FastAPI `BackgroundTasks`，应对多实例和长时间 webhook 处理。
- 为每个平台加入 rate limit、重试退避和 dead-letter queue。
- 将签名验证、租户 allowlist 和内容不可信包装抽成独立 ingress middleware。

这些扩展只有在 webhook 量、多实例部署或审计要求提高时才值得加入；当前代码优先保持入口简单、thread ID 稳定和 dispatch 行为一致。

## 已覆盖与下一步

本章已覆盖三平台入口、签名/事件门禁、确定性 thread ID、thread metadata、统一 durable dispatch，以及 Slack 的 interrupt/queue 混合策略。真实平台重试、签名轮换、生产消息代理和外部 rate limit 未验证。

下一章进入 **Reviewer、Analyzer 与 CI 自动修复**：重点比较主 Agent、只读 Reviewer、风格 Analyzer 三张图的输入输出边界，以及 CI 失败如何通过 `ci_autofix` 找回原始 Agent thread。

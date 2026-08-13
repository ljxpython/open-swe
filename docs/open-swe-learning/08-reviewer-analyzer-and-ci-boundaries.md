# 第 8 章：Reviewer、Analyzer 与 CI 自动修复边界

## 学习目标

本章比较 Open SWE 中三类图的职责：主 Agent、只读 Reviewer、仓库风格 Analyzer，并追踪 Reviewer 的 PR 检查状态、Analyzer 的 bootstrap/continual 两种模式和当前工作树中的 CI 自动修复边界。读完后，你应该能够：

- 解释 Reviewer 为什么拥有独立 graph、工具集、sandbox 和 thread ID。
- 追踪 Reviewer 从 PR payload 到 diff、finding、publish_review、GitHub check 的链路。
- 解释 Analyzer 如何把历史 review 样本转成 repository-specific style prompt，并由 nightly cron 持续更新。
- 理解 `REVIEWER_THREAD_KIND`、`review_check_run_id` 和 `current_reviewer_run_id` 等 metadata 的作用。
- 判断当前 checkout 是否真的包含 CI auto-fix 调度器，避免把 AGENTS.md 中的历史描述当作当前源码事实。

本章不执行真实 GitHub review、PR check 更新、cron 注册或模型调用；验证使用本地单元测试和 graph 装配检查。

## 1. 三张图，不是一个“大 Agent 加几个开关”

| 图 | 输入 | 工具/权限 | 输出 | thread 特征 |
| --- | --- | --- | --- | --- |
| 主 Agent | Dashboard、GitHub、Slack、Linear 任务 | 读写文件、执行命令、提交、开 PR、回源渠道 | 代码修改和业务回复 | 按任务/来源复用 sandbox |
| Reviewer | PR opened/ready/re-review/finding reply | 读 diff、`add_finding`、`publish_review`、只读网络工具 | inline findings、review summary、check conclusion | 一条 PR 一个 canonical reviewer thread |
| Analyzer | 历史 PR review 样本、finding outcomes | `read_finding_outcomes`、`save_review_style_prompt` | repository-specific review style prompt | 一条 repo 一个 analyzer thread |

Reviewer 和主 Agent 的关键边界不是 prompt 文案，而是 graph factory 中的工具列表和 middleware。Reviewer 没有 commit、push、open PR 工具；Analyzer 更窄，只保存风格提示词，不执行代码修改。

## 2. 架构图：Reviewer 与 Analyzer 的职责分层

![主 Agent、Reviewer、Analyzer 职责关系](architecture/premium/png/07-reviewer-analyzer.png)

[打开可编辑 Draw.io 源文件](architecture/premium/07-reviewer-analyzer.drawio) · [打开自包含 HTML 查看器](architecture/premium/html/07-reviewer-analyzer.html)

从左向右看输入和 graph，从上向下看状态落点。紫色区域是 Agent 执行，绿色区域是 thread/store/checkpoint，琥珀色是只读或决策边界。图中 Reviewer 与主 Agent 共用 sandbox 生命周期 helper，但 Reviewer 允许替换过期 sandbox，因为它每次都会重新 checkout PR；Analyzer 也复用 sandbox 基础设施，但不共享主任务的创作工作树语义。

### 图元素到源码映射

| 图元素 | 源码位置 | 关键符号 | 图中行为 |
| --- | --- | --- | --- |
| Reviewer graph factory | `agent/reviewer.py:1324-1444` | `get_reviewer_agent` | 解析 reviewer model，装配只读工具、subagent、middleware |
| Reviewer sandbox | `agent/reviewer.py:929-967` | `_ensure_reviewer_sandbox_for_thread` | GitHub App token、PR repo checkout、`allow_replacement=True` |
| Reviewer prepare | `agent/reviewer.py:970-1080` | `PrepareReviewerRunMiddleware._prepare` | 准备 diff、skills、finding context 和 check metadata |
| Analyzer graph factory | `agent/analyzer.py:165-210` | `get_analyzer` | CompositeBackend、skills route、风格工具和低调用上限 |
| Bootstrap launcher | `agent/dashboard/review_style_jobs.py:76-153` | `start_bootstrap_analysis` | 收集样本、建立 repo thread、启动 analyzer run |
| Continual cron | `agent/dashboard/analyzer_cron.py:34-57` | `ensure_continual_cron` | 幂等注册每日 repo cron |
| Reviewer check | `agent/webhooks/github.py:270-280`、`agent/review/publish.py:437-474` | `review_check_run_id`、`settle_review_check_run` | 创建并在完成/失败时结算 GitHub check |
| Auto-fix opt-out | `agent/dashboard/autofix_state.py:19-50` | `is_pr_autofix_disabled` | 保存每个 PR 的 `autofix on/off` 状态 |

### 短接线图

```text
PR opened/ready
  -> generate_reviewer_thread_id(owner, repo, pr)
  -> reviewer metadata + review_check_run_id
  -> dispatch_agent_run(..., assistant_id="reviewer")
  -> prepare_review_repo + fetch diff
  -> add/update finding -> publish_review
  -> settle_review_check_run

bootstrap repo analysis
  -> collect_review_samples
  -> analyzer thread + analyzer_mode=bootstrap
  -> read historical feedback
  -> save_review_style_prompt
  -> register continual cron
```

## 3. Reviewer：把“审查”建模成只读、可演进 finding 图

### 3.1 Canonical reviewer thread

Reviewer thread ID 使用 `generate_reviewer_thread_id(owner, repo, pr_number)`：

```text
uuid5(NAMESPACE_URL, "owner/repo/pr/<number>/reviewer")
```

同一个 PR 的 opened、ready_for_review、push re-review、review finding reply 都回到这条 thread。它与主 Agent 的工作 thread 分开，避免 Reviewer 看到主 Agent 未提交修改，也避免主 Agent 的工具误触发发布 review。

### 3.2 Reviewer graph factory

`get_reviewer_agent()` 首先要求 `thread_id` 和真实 graph execution context；仅做构图检查时返回空 agent，避免创建外部 sandbox。真实运行时它：

1. 解析 `reviewer_model_id`/`reviewer_reasoning_effort`，否则读取 team reviewer defaults。
2. 单独解析 reviewer subagent model。
3. 通过 `_ensure_reviewer_sandbox_for_thread()` 获取 backend。
4. 装配 `fetch_review_diff`、`add_finding`、`update_finding`、`list_findings`、`publish_review`、finding thread 回复和只读网络工具。
5. 注入 Reviewer middleware：prepare、输入清洗、模型调用上限、错误处理、GitHub proxy refresh、消息队列、超时、tool-call 修复和 check settle。

Reviewer 工具列表没有主 Agent 的 `write_file`、`execute`、`open_pull_request` 等创作工具。它可以读取 sandbox 中 checkout 的代码和 diff，但不能直接修改 PR 工作树。

### 3.3 Reviewer sandbox 为什么允许替换

`_ensure_reviewer_sandbox_for_thread()` 传 `allow_replacement=True`。原因是 Reviewer sandbox 每次通过 `prepare_review_repo()` 从 base/head SHA 重新 checkout，sandbox 中没有用户未提交创作；一个 PR thread 可能活得比 provider sandbox retention 更久，拒绝替换会永久卡住后续 review。

这与主 Agent 的 `allow_replacement=False` 正好相反：主 Agent 的 sandbox 有未提交工作，静默替换会丢上下文；Reviewer 的 sandbox 可由 PR SHA 重建。

### 3.4 PrepareReviewerRunMiddleware

prepare 阶段完成四类工作：

- 取得 scoped GitHub App installation token，并刷新 sandbox GitHub proxy。
- 调用 `prepare_review_repo()` checkout `head_sha` 与 `base_sha`。
- 从 trusted base ref materialize review skills。
- 获取 diff、已有 findings、last reviewed SHA 和 finding reply context，形成 system/user prompt。

prepare fingerprint 包含 `pr_number`、base/head SHA、last reviewed SHA、reviewer event 和 finding reply ID。这样重复 prepare 不会把不同 PR 状态误认为同一次运行。

## 4. Finding 生命周期：不是“每个问题一个永久对象”

Reviewer 使用 single-evolving-findings 模型：

```text
add_finding
  -> finding store
  -> update_finding / reconcile
  -> publish_review
  -> review outcome / human reply
  -> next re-review
```

PR 新提交后，Reviewer 不会简单重复发布所有旧评论，而是用新的 diff 和已有 finding 进行 reconcile：

- 仍然存在的问题保留并更新。
- 已修复的问题标记 resolved/dismissed。
- 新 diff 中的新问题追加 finding。
- 人类回复通过 `process_github_review_finding_reply()` 找到 parent comment 对应的 finding，再以 `reviewer_event=finding_reply` 启动 reviewer graph。

这就是为什么 reviewer thread 的 metadata 需要保存 PR、SHA、finding 和当前 run 信息，而不是只存一个 `thread_id`。

## 5. GitHub Check：把 Reviewer 状态暴露给仓库 UI

首次 PR review dispatch 前，GitHub webhook 会调用 `create_review_check_run()` 并把返回的整数 ID 写入 reviewer thread metadata 的 `review_check_run_id`。Review tool 发布结果后，`settle_review_check_run()` 读取 pending conclusion，更新 GitHub Check 为 success、neutral 或 failure，并清理 metadata。

如果 graph 在发布 review 前失败，`agent/completion.py` 的 `_settle_failed_reviewer_check()` 会用 `review_check_pending_result` 或默认 neutral 结论关闭遗留 check，避免 PR 永远显示“检查中”。

这是一条“平台状态同步”链，不等同于 CI 自动修复：Check 记录 Reviewer 是否完成，不能证明 CI workflow 通过。

## 6. Analyzer：从历史 review 学出仓库风格

### 6.1 Bootstrap 模式

`start_bootstrap_analysis(full_name, github_token, created_by)`：

1. `collect_review_samples()` 扫描仓库历史 merged PR review。
2. `format_samples_for_analyzer()` 形成样本文本。
3. `generate_review_style_thread_id(owner, repo)` 建立稳定 analyzer thread。
4. metadata 记录样本数量、top reviewers 和分析状态。
5. `create_durable_run()` 启动 analyzer graph，configurable 设置 `analyzer_mode="bootstrap"`。
6. Analyzer 读取 `bootstrap-repo-analysis` skill，调用 `save_review_style_prompt` 保存 repo-specific prompt。

### 6.2 Continual 模式

Bootstrap 成功后，`ensure_continual_cron()` 为每个 repo 幂等注册每日 cron。cron 运行时使用同一个确定性 analyzer thread ID，但输入是新的 run，避免历史消息无限堆积；它调用 `read_finding_outcomes`，根据 Reviewer finding 被接受、修复、忽略的结果细化风格提示词。

`PrepareAnalyzerRunMiddleware` 会：

- 连接 analyzer thread 对应 sandbox。
- 解析 `review_style_full_name` 和 `analyzer_mode`。
- 取得 GitHub token 并配置 proxy。
- 通过 `skill_path_for_mode(mode)` 选择 bootstrap/continual skill。
- 渲染 system prompt 和 repo sample context。

### 6.3 Analyzer 为什么不是 Reviewer 的 middleware

Analyzer 的结果是“审查风格规则”，不是一次 PR finding。把它做成独立 graph 有三个好处：

- 可以按 repo 定期运行，不依赖某一个 PR thread。
- 可以独立限制模型调用和费用。
- Reviewer 只在 prepare prompt 时读取已经保存的 style prompt，职责清晰。

## 7. 当前工作树的 CI 自动修复边界

这里必须以当前文件系统为准：`agent/ci_autofix.py` 当前不存在，因此不能把仓库说明中描述的 `sweep_open_prs`、check_run dispatch、loop cap 和 human commit skip 当作本章已验证源码。

当前能确认的相关部件只有：

| 当前源码 | 已存在行为 | 不是它负责的事情 |
| --- | --- | --- |
| `agent/dashboard/autofix_state.py` | 保存 `owner/repo#pr` 的 autofix disabled 状态 | 不创建 CI fix Run |
| `agent/dashboard/profiles.py` | profile 中有 `auto_fix_ci` 字段 | 不执行 CI sweep |
| `agent/utils/github_ci.py` | 读取 failing check/workflow 状态和 payload 字段 | 不调度主 Agent 修复 |
| `agent/middleware/check_message_queue.py` | 消费 `("autofix", thread_id)` pending event | 不产生 autofix event |
| `agent/webhooks/github.py` | Reviewer check 创建/结算 | 不等同于 CI auto-fix |

因此本章对 CI 只能给出边界结论：**开关、检查读取和队列消费存在，但自动修复调度器在当前 checkout 缺失。** 后续恢复该模块后，需要重新补源码证据和测试，不能依据旧 AGENTS.md 直接声称已覆盖。

## 8. 综合调用链：PR 首次审查到风格持续学习

```text
PR ready_for_review
  -> github_webhook gate
  -> reviewer_thread_id(owner/repo/pr)
  -> create GitHub check + reviewer metadata
  -> dispatch_agent_run(assistant_id="reviewer")
  -> get_reviewer_agent()
  -> prepare_review_repo + diff + findings
  -> add/update finding
  -> publish_review
  -> settle_review_check_run
  -> finding outcomes stored

bootstrap analysis
  -> collect historical samples
  -> analyzer thread
  -> save_review_style_prompt
  -> ensure_continual_cron
  -> nightly read_finding_outcomes
  -> refine repository style prompt
```

## 9. 与源码一致的伪代码

```python
async def start_reviewer(pr):
    thread_id = reviewer_thread_id(pr.owner, pr.repo, pr.number)
    await ensure_thread_exists(thread_id)
    await set_reviewer_metadata(thread_id, pr=pr, watch=True)
    check_id = await create_review_check_run(pr.head_sha)
    await set_reviewer_metadata(thread_id, extra={"review_check_run_id": check_id})

    run = await dispatch_agent_run(
        thread_id,
        build_review_prompt(pr),
        build_reviewer_config(pr),
        source="github_pr_ready",
        assistant_id="reviewer",
    )
    await store_current_reviewer_run_id(thread_id, run)


async def run_analyzer(full_name, mode):
    thread_id = generate_review_style_thread_id(*full_name.split("/", 1))
    config = {
        "thread_id": thread_id,
        "review_style_full_name": full_name,
        "analyzer_mode": mode,
    }
    return await create_durable_run(
        thread_id,
        "analyzer",
        input=build_analyzer_input(full_name, mode),
        config={"configurable": config},
    )
```

## 10. 最小验证

建议运行当前 checkout 中的 Reviewer、Analyzer 和相关 check/metadata 测试：

```bash
uv run pytest -q tests/reviewer
uv run pytest -q tests/analyzer
uv run pytest -q tests/github/test_github_feedback.py
uv run pytest -q tests/agent/test_dispatch.py
```

若 `tests/reviewer` 或 `tests/analyzer` 目录不存在，应改为执行实际存在的匹配文件；本章不通过空目录命令伪造验证结果。真实 GitHub check、cron 注册、review publish 和模型调用未执行。

## 11. 常见误区与反例

1. **Reviewer 直接复用主 Agent 工具列表。** 这会让只读审查具备提交、推送或开 PR 能力，破坏权限边界。
2. **Reviewer sandbox 不可达就按主 Agent 规则永久失败。** Reviewer 工作树可按 SHA 重建，因此需要 `allow_replacement=True`。
3. **Analyzer 和 Reviewer 共用同一 thread。** 历史风格学习会污染 PR 对话上下文，也让 nightly run 无限积累消息。
4. **GitHub Check success 等于 CI success。** Reviewer Check 只表示 review graph 结论，CI workflow 是另一类状态。
5. **当前没有 `agent/ci_autofix.py` 却按旧文档讲完整自动修复。** 这是源码证据错误；只能记录缺失边界。
6. **把 `autofix_state` 当作调度器。** 它只保存 per-PR opt-out，不能自行发现失败检查或创建 fix run。

## 12. 检查题与改造练习

1. 解释 Reviewer 为什么使用 `owner/repo/pr/<number>/reviewer`，而主 Agent 使用分支/来源任务 ID。
2. 沿 `PrepareReviewerRunMiddleware._prepare()` 列出 checkout、skills、diff 和 finding context 的先后关系。
3. 设计测试断言：Reviewer tools 列表不能包含 `open_pull_request` 或 `write_file`。
4. 解释 Analyzer 的 bootstrap thread 为什么要稳定，但 nightly run 又不能依赖历史消息累积。
5. 如果恢复 CI auto-fix 模块，列出至少四个必须重新验证的 skip gate：PR opt-out、base branch failure、human commit、same-head dedupe。

## 已覆盖与下一步

本章已覆盖 Reviewer graph、只读工具边界、Reviewer sandbox、finding/check 生命周期、Analyzer bootstrap/continual 和当前 CI 相关模块的真实边界。Reviewer/Analyzer 只完成本地装配和单元验证；真实 GitHub review、cron、check 更新和 CI auto-fix 未执行，其中 CI 调度器因当前 checkout 缺失而受源码条件阻塞。

下一章进入 **测试、类型检查、部署和安全边界**，把课程中的源码证据、单元测试、运行命令、配置契约和外部服务风险收束成一套可复现检查清单。

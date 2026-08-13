# `agent/utils` 通俗说明

## 先说结论

`agent/utils/` 是 Open SWE 的“公共工具箱”。它不负责创建 Agent，也不负责页面业务，而是把很多 Agent 都会用到的麻烦事集中处理掉：认证、GitHub/Slack/Linear 通信、LangGraph 线程操作、模型选择、沙箱生命周期、代码差异和追踪等。

可以把它理解成下面这层“基础设施适配层”：

```text
Webhook / Dashboard / Tools / Agent 图
                |
                v
           agent/utils
                |
   GitHub  Slack  Linear  LangGraph  沙箱  模型提供商
```

上层模块通常直接导入这里的函数。例如：

- `agent/server.py` 用它解析用户、选择模型、创建或恢复沙箱。
- `agent/reviewer.py` 用它准备 PR 代码、读取 `AGENTS.md`、创建评审检查。
- `agent/webapp.py`、`agent/dashboard/` 用它派发线程、生成 Dashboard 链接、发送消息。
- `agent/tools/` 用它调用 Linear、Slack、GitHub 和安全 HTTP 接口。

这里的函数大多是“被调用后完成一件具体小事”的异步辅助函数，不需要单独启动服务。配置通常来自环境变量、LangGraph 的 `configurable`，或者当前线程的元数据。

## 一、Agent 指令、Skill 和提示词上下文

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`agents_md.py`](../agent/utils/agents_md.py) | 从 GitHub 仓库读取根目录或子目录里的 `AGENTS.md`；找不到时兼容 `CLAUDE.md`。还会根据改动文件计算哪些目录级规则适用。 | Reviewer 启动时调用 `fetch_agents_md()` 和 `fetch_scoped_agents_md()`，把仓库规则放进评审提示词；主 Agent 的 `SubdirAgentsReadMiddleware` 也会使用同类规则。 |
| [`analyzer_skills.py`](../agent/utils/analyzer_skills.py) | 把仓库分析器的两个 `SKILL.md` 读成虚拟文件，不写进沙箱。 | `agent/analyzer.py` 启动时调用 `build_skill_files()`，再把文件塞进 `/skills/` 路由；`skill_path_for_mode()` 根据 `bootstrap` 或 `continual` 选择入口。 |
| [`api_standards_skill.py`](../agent/utils/api_standards_skill.py) | 从 LangSmith Context Hub 拉取 API 规范 Skill。 | Reviewer 开始一次评审时调用 `fetch_api_standards_skill()`；拉取失败就跳过补充规则，不阻塞整个评审。 |
| [`dashboard_handoff.py`](../agent/utils/dashboard_handoff.py) | 定义“对话已经从 Slack 转到 Web Dashboard”的标记和提示文本。 | Dashboard 发消息时附加这个标记，模型看到后知道应该在 Web 流里回复，不要误发回 Slack。 |
| [`dashboard_links.py`](../agent/utils/dashboard_links.py) | 统一拼接“在 Web 中打开”的 URL，包括线程、计划审批、工作流审批和 PR 评审页面。 | Slack/Linear/GitHub 回复和 Dashboard API 调用 `dashboard_thread_url()` 等函数生成链接。 |
| [`user_messages.py`](../agent/utils/user_messages.py) | 统一生成 Open SWE 主动发出的警告消息格式。 | 沙箱不可达、步骤超限等自动通知调用 `warning()`，保证消息带统一的警告图标。 |

## 二、身份认证和 GitHub

### 认证、提交身份和 Token

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`auth.py`](../agent/utils/auth.py) | 解析“这次运行应该用谁的 GitHub 身份”：优先用户 OAuth，必要时回退 GitHub App 安装 Token；同时处理 LangSmith Agent Auth 和认证失败提示。 | `agent/server.py`、Webhook 和 Dashboard 在准备一次运行时调用 `resolve_github_token()`；拿不到用户授权时抛出 `GitHubUserAuthRequired` 或给来源渠道发提示。 |
| [`github_token.py`](../agent/utils/github_token.py) | 按线程和用户身份缓存 GitHub Token，检查过期、读取线程配置、失效旧 Token。 | 工具或服务通过 `get_github_token()` / `get_github_token_from_thread()` 取 Token，避免每次 API 调用都重新认证。 |
| [`github_app.py`](../agent/utils/github_app.py) | 用 GitHub App 私钥签 JWT，再换取组织安装 Token；按组织、仓库和权限范围缓存结果。 | `auth.py` 在没有用户 OAuth 时调用它；`github_proxy.py` 也用它给沙箱 GitHub 代理刷新 Token。 |
| [`authorship.py`](../agent/utils/authorship.py) | 处理提交作者、`Co-authored-by`、PR 署名和 Open SWE 线程链接，避免提交作者信息不被 GitHub/Vercel 识别。 | 主 Agent 提交代码或创建 PR 前调用 `resolve_triggering_user_identity()`、`add_bot_coauthor_trailer()` 和 `add_pr_collaboration_note()`。 |
| [`github_org_membership.py`](../agent/utils/github_org_membership.py) | 查询用户是不是某个 GitHub 组织的有效成员。 | Dashboard OAuth 回调和 webhook 门禁调用 `is_user_active_org_member()`，决定是否允许用户继续使用。 |

### GitHub API、评论和 CI

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`github_http.py`](../agent/utils/github_http.py) | GitHub API 的通用 HTTP 客户端：统一请求头、超时、重试、二次限流和退避等待。 | 其他 GitHub 工具通过 `github_client()` 或 `github_request()` 发请求，不再各自实现重试逻辑。 |
| [`github_comments.py`](../agent/utils/github_comments.py) | 处理 GitHub webhook 签名、评论清洗、PR 上下文提取、评论分页、回复和表情反应。 | `agent/webapp.py` 收到 GitHub 事件后校验签名并提取线程；Agent 工具和 reviewer 用它读取/发布 PR 评论。 |
| [`github_checks.py`](../agent/utils/github_checks.py) | 创建和完成“Open SWE Review”检查，以及发布自动修复状态检查。 | Reviewer 开始评审时创建 in-progress check，结束时按发现数量写 success/failure；CI 自动修复流程发布状态。 |
| [`github_ci.py`](../agent/utils/github_ci.py) | 只读查询失败的 Check Run、旧式 Status、开放 PR、提交作者和仓库写权限，并解析 CI webhook 的分支/SHA。 | `agent/ci_autofix.py` 和 `agent/ci_monitor.py` 用它判断“哪个 PR 的哪个提交真的失败了，是否值得触发自动修复”。 |
| [`github_feedback.py`](../agent/utils/github_feedback.py) | 把 reviewer 发现上的 GitHub 表情反馈转换成 LangSmith feedback 分数，并用 Store 记录事件去重。 | GitHub reaction webhook 调用 `process_github_reaction_added/removed()`，更新 finding 的接受/拒绝反馈。 |
| [`github_http.py`](../agent/utils/github_http.py) | GitHub 请求底座，负责 API 请求的可靠性。 | 被 `github_comments.py`、`github_ci.py`、组织权限等模块间接复用。 |
| [`github_proxy.py`](../agent/utils/github_proxy.py) | 记录沙箱里 GitHub 代理 Token 的过期时间，在快过期时刷新代理配置。 | `sandbox_state.py` 创建/恢复沙箱后调用 `maybe_refresh_proxy_token()`；这样沙箱里的 `git` 和 `gh` 不会突然因 Token 过期失效。 |

## 三、Slack、Linear 和外部网络

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`slack.py`](../agent/utils/slack.py) | Slack Web API 封装、线程 ID 解析、消息发送/读取、运行状态和用量摘要展示。 | Slack webhook 和 `slack_*` 工具调用它，把 Agent 回复发回原线程；也会附带 Dashboard、LangSmith 链接。 |
| [`slack_feedback.py`](../agent/utils/slack_feedback.py) | 处理 Slack 上对 reviewer finding 的表情反馈，并同步到 LangSmith。 | Slack reaction webhook 调用它，和 `github_feedback.py` 类似，但来源换成 Slack。 |
| [`linear.py`](../agent/utils/linear.py) | Linear GraphQL API 封装：评论、查 issue、搜 issue、建/改/删 issue、列团队。 | `linear_*` 工具和 Linear webhook 使用这些函数；`post_linear_trace_comment()` 还能把 LangSmith 追踪链接回写到 issue。 |
| [`comments.py`](../agent/utils/comments.py) | 从 Linear 评论列表中找出“上次 Agent 回复之后的用户新评论”。 | Linear webhook 触发跟进运行前调用 `get_recent_comments()`，避免把旧评论重复喂给模型。 |
| [`linear_team_repo_map.py`](../agent/utils/linear_team_repo_map.py) | 配置 Linear 团队/项目到 GitHub 仓库的映射。 | Linear 事件解析仓库时读取 `LINEAR_TEAM_TO_REPO`，没有项目时按团队默认仓库处理。 |
| [`http.py`](../agent/utils/http.py) | 普通外部 HTTP 请求的统一封装，提供超时和响应处理。 | `http_request` 工具以及少数外部集成使用它；GitHub 请求走专门的 `github_http.py`。 |
| [`url_safety.py`](../agent/utils/url_safety.py) | 防止 SSRF 和危险跳转：校验 URL、解析 DNS、阻止内网地址，并控制重定向。 | `fetch_url`、`http_request` 和多模态图片抓取在真正联网前调用它。 |

## 四、LangGraph 线程、运行和追踪

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`thread_ids.py`](../agent/utils/thread_ids.py) | 把 Slack 频道 + thread timestamp 转成稳定的 UUID。 | Slack webhook 每次收到消息都用它生成同一个 `thread_id`，从而让后续消息回到同一个 Agent 线程。 |
| [`thread_ops.py`](../agent/utils/thread_ops.py) | 创建 LangGraph SDK client，读取线程忙闲状态，并把 Dashboard 跟进消息放进 FIFO 队列。 | `agent/dispatch.py`、Dashboard thread API 和 middleware 调用；webhook 通常走 interrupt 派发，Dashboard 忙碌线程则走队列。 |
| [`tracing.py`](../agent/utils/tracing.py) | 给不同图绑定不同 LangSmith tracing project。 | `langgraph.json` 的入口包装使用 `traced_graph_factory()`，主 Agent 写入 `open-swe-agent`，Reviewer 写入 `open-swe-review`。 |
| [`langsmith.py`](../agent/utils/langsmith.py) | 拼 LangSmith trace URL，并创建/更新/删除确定性的 feedback。 | Slack/Linear/GitHub 回复中调用 `get_langsmith_trace_url()`；评审反馈和反应同步时调用 feedback 函数。 |
| [`run_usage.py`](../agent/utils/run_usage.py) | 从 LangGraph 状态里的 AI 消息汇总本轮使用过的模型和 Token 数。 | Slack、Dashboard 等展示运行摘要时调用 `summarize_run_usage()`。 |
| [`ttl_cache.py`](../agent/utils/ttl_cache.py) | 一个进程内异步 TTL 缓存，支持并发去重、过期后后台刷新和失败时返回旧值。 | 认证、配置或外部查询需要短时间复用结果时调用 `cached()` / `cached_stale_while_revalidate()`；它不是持久化数据库。 |
| [`json_types.py`](../agent/utils/json_types.py) | 把 LangGraph SDK 返回的宽泛对象收窄成可安全序列化的 JSON 字典。 | Dashboard 路由和 webhook 读取 thread/run 元数据时调用 `as_json_object()`、`thread_metadata()`、`run_metadata()`。 |

## 五、模型和网关

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`model.py`](../agent/utils/model.py) | 统一构造聊天模型，处理各家 provider 的参数差异、推理强度、超时、重试、Responses API 和模型缓存；还决定 fallback 模型。 | `agent/server.py`、`reviewer.py`、`chat.py` 通过 `make_model()` 创建模型，再用 `provider_model_kwargs()` 根据用户 effort 生成参数。 |
| [`gateway.py`](../agent/utils/gateway.py) | 决定模型是否经 LangSmith Gateway 转发，并为 OpenAI、Anthropic、Fireworks、Google 等 provider 生成对应 base URL 和参数。 | `model.py` 调用 `gateway_overrides()`；团队设置和环境变量共同决定是否启用。 |
| [`deferred_model.py`](../agent/utils/deferred_model.py) | 一个“先占位、调用时再报错”的模型对象。 | 模型初始化阶段遇到配置错误但仍要让图启动时，`make_deferred_error_model()` 返回它；真正调用模型时再把原始错误展示出来。 |
| [`multimodal.py`](../agent/utils/multimodal.py) | 从文本里找图片 URL，安全下载并转换成模型可读的图片 block，同时去重和处理 provider 认证。 | Slack/GitHub/聊天入口把带图片的用户消息交给它，再传给支持视觉的模型；不支持时生成明确警告。 |

## 六、沙箱和仓库

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`sandbox.py`](../agent/utils/sandbox.py) | 根据 `SANDBOX_TYPE` 选择 LangSmith、Daytona、Modal、Runloop、E2B 或本地沙箱工厂，并负责创建/重连。 | `server.py`、`sandbox_state.py` 和 reviewer 初始化时调用 `create_sandbox()`；启动时调用 `validate_sandbox_startup_config()`。 |
| [`sandbox_paths.py`](../agent/utils/sandbox_paths.py) | 找到沙箱里真正可写的工作目录，并拼出仓库目录。兼容不同沙箱供应商的路径 API。 | Agent 初始化、reviewer 仓库准备、checkpoint 和工具执行前调用 `resolve_sandbox_work_dir()` / `resolve_repo_dir()`。 |
| [`sandbox_state.py`](../agent/utils/sandbox_state.py) | 维护线程到沙箱 backend 的进程内缓存；负责创建、恢复、代理转发和“沙箱不可达”保护。 | `server.py` 的 `ensure_sandbox_for_thread()` 是核心入口。已有沙箱只恢复，不会悄悄创建空沙箱替换，避免丢失未提交代码。 |
| [`langsmith.py`](../agent/utils/langsmith.py) | 这里既提供 LangSmith URL/feedback，也在沙箱分组中作为追踪适配器被引用。 | 详见上面的“LangGraph 线程、运行和追踪”；它不是 LangSmith 沙箱创建器，真正的沙箱供应商在 `agent/integrations/langsmith.py`。 |
| [`read_only_backend.py`](../agent/utils/read_only_backend.py) | 给一个已有 backend 套只读外壳，只转发 `ls/read/grep/glob/download`，拒绝写入和执行。 | Analyzer、Reviewer 的虚拟文件路由使用它，确保模型能读规则但不能修改规则来源。 |
| [`repo.py`](../agent/utils/repo.py) | 从自然语言、`repo:owner/name` 或 GitHub URL 中提取仓库归属。 | Linear、Slack、Dashboard 等入口解析用户消息时调用 `extract_repo_from_text()`。 |
| [`repo_prep.py`](../agent/utils/repo_prep.py) | 在 Reviewer 第一次调用模型前，自动 clone/fetch 并 checkout PR head；还从 base ref 提取可信 Skill。 | `agent/reviewer.py` 调用 `prepare_review_repo()` 和 `materialize_trusted_skills()`，避免让模型临时执行 clone，也防止 PR head 注入恶意评审规则。 |

## 七、评审、差异和数据结构

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`file_diff.py`](../agent/utils/file_diff.py) | 根据 `edit_file` / `write_file` 的参数和编辑前内容，生成编辑前后文件对。 | Dashboard 的编辑审批预览使用 `build_file_diff()`；它处理新文件、替换文本、二进制和截断文件。 |
| [`turn_checkpoint.py`](../agent/utils/turn_checkpoint.py) | 每轮开始时在沙箱 Git 对象库里保存一个 checkpoint，之后用 `git diff` 算出本轮改了哪些文件。 | `PrepareAgentRunMiddleware` 记录 checkpoint；Dashboard 的 `/threads/{id}/turn-diff` 读取结果。这样连 Agent 通过 `execute` 改的文件也能被统计。 |
| [`reviewer_outcomes.py`](../agent/utils/reviewer_outcomes.py) | 把 finding 的状态、分数和最终结果写进 LangSmith 数据集，并按仓库读取历史结果。 | Reviewer 发布 finding 后调用 `emit_finding_status_outcome()`；Analyzer 的 continual 模式读取它来学习仓库评审风格。 |
| [`json_types.py`](../agent/utils/json_types.py) | 负责 SDK 对象到 JSON 的安全转换。 | 详见前面的线程和运行分组，Dashboard 数据返回时会用到。 |
| [`comments.py`](../agent/utils/comments.py) | 负责 Linear 评论增量筛选。 | 详见前面的 Slack/Linear 分组，跟进消息进入 Agent 前使用。 |

## 关键完整流程

### 1. Agent 启动、选模型、绑定沙箱

```text
Webhook / Dashboard
        |
        v
agent/server.py:get_agent
        |
        +--> auth.resolve_github_token()       -> 用户 OAuth / GitHub App Token
        +--> model.make_model()                -> provider 参数、Gateway、fallback
        +--> sandbox_state.ensure_sandbox...  -> 创建或恢复线程沙箱
        |       |
        |       +--> sandbox.create_sandbox()
        |       +--> sandbox_paths.resolve_repo_dir()
        |       +--> github_proxy.maybe_refresh_proxy_token()
        |
        +--> create_deep_agent(...)
```

关键点：沙箱 ID 会写在线程元数据里；内存缓存只是加速，不是唯一存储。已有沙箱不可达时会报错通知用户，不会拿一个空沙箱冒充原工作区。

### 2. GitHub 事件到 Agent 回复

```text
GitHub webhook
  -> github_comments.verify_github_signature()
  -> 提取 PR / branch / comment 上下文
  -> thread_ids / github_comments 生成稳定 thread_id
  -> thread_ops 或 agent.dispatch 派发 LangGraph run
  -> auth.resolve_github_token()
  -> Agent 调用 gh/git 或 GitHub tools
  -> github_comments.post_github_comment()
```

`github_http.py` 是底层请求的统一重试层；`github_token.py` 缓存当前线程 Token；`github_proxy.py` 则保证沙箱内部的 GitHub 访问持续有效。

### 3. Reviewer 评审一个 PR

```text
reviewer.py:get_reviewer_agent
  -> repo_prep.prepare_review_repo()
  -> agents_md.fetch_agents_md()/fetch_scoped_agents_md()
  -> api_standards_skill.fetch_api_standards_skill()
  -> sandbox 里固定到 PR head
  -> Reviewer 只读工具 + findings 工具
  -> github_checks 创建/完成 Review check
  -> reviewer_outcomes 写入结果
```

`repo_prep.py` 还会从 base commit 提取可信 Skill，避免把 PR 作者新加的规则直接当成评审指令。

### 4. Dashboard 在运行中的线程里发跟进消息

```text
Dashboard API
  -> thread_ops.get_thread_active_status()
  -> 线程忙：queue_message_for_thread() 写入 LangGraph Store
  -> check_message_queue_before_model middleware 取出队列
  -> 下一次模型调用前注入用户消息
```

Webhook 的紧急跟进通常使用 LangGraph 的 `multitask_strategy="interrupt"`，而 Dashboard 的“排队发送”才使用这里的 Store 队列。

### 5. 本轮改动如何显示在 Dashboard

```text
每轮开始
  -> turn_checkpoint.record_turn_checkpoint()
  -> refs/open-swe/turns/<key>
  -> 运行期间 Agent 任意修改文件
  -> Dashboard 请求 turn-diff
  -> turn_checkpoint.read_turn_diff()
  -> git diff + 文件内容返回 UI
```

它不是重放 `edit_file` 日志，而是直接比较 Git 工作区，所以通过 `execute` 修改、后来又撤销的文件也能得到正确结果。

## 最容易混淆的几点

1. `agent/utils` 不是数据库层。`ttl_cache.py`、Token 缓存和 `SANDBOX_BACKENDS` 都是进程内缓存；线程元数据、队列、finding 结果等持久信息由 LangGraph Store、LangSmith 或 Dashboard 存储层负责。
2. `github_app.py` 负责“铸造”安装 Token，`github_token.py` 负责“按线程取缓存 Token”，`auth.py` 负责“决定本次该用哪一种身份”，三者不是重复代码。
3. `file_diff.py` 是“编辑即将发生时”的预览；`turn_checkpoint.py` 是“这一轮已经跑完后”的真实 Git 差异，前者不能替代后者。
4. `langsmith.py` 是追踪链接和反馈适配器；LangSmith 沙箱供应商的创建代码在 `agent/integrations/langsmith.py`，别把两个文件混为一谈。
5. 所有这些工具通常由 `server.py`、`reviewer.py`、`webapp.py`、Dashboard 路由或 `agent/tools/` 间接调用，不需要用户直接运行。

## 按任务查文件

| 想解决的问题 | 先看 |
|---|---|
| 用户为什么没有 GitHub 权限 | `auth.py`、`github_token.py`、`github_app.py` |
| 沙箱为什么创建/恢复失败 | `sandbox.py`、`sandbox_state.py`、`sandbox_paths.py` |
| PR 评论怎么进 Agent | `github_comments.py`、`thread_ids.py`、`thread_ops.py` |
| CI 失败为什么触发自动修复 | `github_ci.py`、`github_checks.py` |
| Reviewer 怎么拿到仓库规则 | `repo_prep.py`、`agents_md.py`、`api_standards_skill.py` |
| 模型和 fallback 怎么选 | `model.py`、`gateway.py`、`deferred_model.py` |
| Dashboard 怎么显示改动 | `file_diff.py`、`turn_checkpoint.py` |
| 为什么能看到 LangSmith 链接/反馈 | `tracing.py`、`langsmith.py`、`reviewer_outcomes.py` |
| 外部网页请求是否安全 | `http.py`、`url_safety.py`、`multimodal.py` |


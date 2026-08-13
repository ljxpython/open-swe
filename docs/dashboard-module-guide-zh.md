# `agent/dashboard` 目录中文导读

这份文档解释 `agent/dashboard/` 下每个文件是干什么的。这里的“dashboard”不是单纯的网页，而是 Open SWE 仪表盘背后的 FastAPI 业务代码：网页发请求，`routes.py` 接住请求，再调用下面这些模块完成登录、保存设置、读取 Agent 线程、展示评审结果等工作。

## 先看整体关系

```text
浏览器 UI
   |
   v
agent/webapp.py 挂载的 dashboard router
   |
   v
agent/dashboard/routes.py  （HTTP 路由总入口）
   |
   +--> 登录与权限模块
   +--> 用户/团队/仓库配置模块
   +--> Agent 线程、计划、用量模块
   +--> PR 评审模块
   +--> 自动修复、调度、快照模块
   |
   +--> LangGraph Store       保存配置、凭据、评审状态等
   +--> LangGraph threads/runs 管理 Agent 线程和运行
   +--> GitHub / Slack / Notion 外部 API
   +--> 沙箱                       保存计划文件、构建仓库快照
```

大多数“数据库式”数据不是写进本项目自建的 SQL 表，而是通过 `langgraph_sdk.get_client().store` 存到 LangGraph Store。线程和运行则通过 `client.threads`、`client.runs` API 管理。令牌等敏感值会加密后再保存。

## 1. 总入口

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `__init__.py` | 让别人可以从 `agent.dashboard` 访问 `router`。它采用“需要时才导入”的方式，避免只用一个小工具时把整套 FastAPI 路由都加载进来。 | 被 `agent.webapp` 等入口使用；不负责业务存储。 |
| `routes.py` | 仪表盘的“总接待台”。登录、个人设置、团队设置、线程、评审、仓库、技能等 HTTP 接口都在这里注册，然后把具体工作转给其他模块。 | 被 `agent/webapp.py` 挂载；调用本目录几乎所有模块以及 GitHub API。 |

## 2. 登录、身份与权限

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `oauth.py` | 处理 GitHub 登录全流程：跳转 GitHub、校验 `state` 和来源、换取 token、签发登录 Cookie、检查会话是否有效。 | 被 `routes.py` 调用；与 GitHub OAuth 通信；会话放在签名 Cookie 中。 |
| `admin.py` | 判断当前用户是不是管理员，或是否有只读观测权限。管理员名单来自环境变量。 | 被路由依赖注入使用；读取环境变量，不保存数据。 |
| `profiles.py` | 保存用户的个人偏好，例如模型、推理强度、默认仓库、是否自动创建 PR；同时负责加密保存和刷新 GitHub OAuth token。 | 被 `routes.py`、`agent_overrides.py`、`repo_access.py` 使用；读写 LangGraph Store 的 profile/token 记录。 |
| `user_credentials.py` | 管理用户连接的第三方凭据，目前包括 Currents 和 Notion；对外只返回“是否已连接”等脱敏状态。 | 被 `routes.py` 调用；凭据加密后写入 LangGraph Store。 |
| `slack_oauth.py` | 把当前 GitHub 用户和 Slack 账号绑定起来：构造授权地址、换 token、校验 Slack 工作区和邮箱。 | 被 `routes.py` 调用；与 Slack OAuth API 通信；最终映射由 `user_mappings.py` 保存。 |
| `notion_oauth.py` | 处理 Notion MCP 的 OAuth，包括 PKCE、OAuth 元数据发现、动态注册客户端、换 token 和刷新 token。 | 被 `routes.py`、`user_credentials.py` 调用；流程临时状态和加密 token 写入 LangGraph Store。 |

## 3. 用户、团队和仓库配置

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `agent_instructions.py` | 保存“某个仓库专用”的 Agent 规则，例如项目编码规范或测试要求。运行 Agent 时会把它追加到提示词里。 | 被 `routes.py` 和 Agent 创建逻辑使用；读写 LangGraph Store。 |
| `user_instructions.py` | 保存“某个用户专用”的 Agent 规则。用户可在个人页面编辑，Agent 也能通过工具更新。 | 被 `routes.py` 和 Agent 提示词组装逻辑使用；读写 LangGraph Store。 |
| `agent_overrides.py` | 把用户资料翻译成一次运行真正要用的模型、推理强度、默认仓库等覆盖项，并处理邮箱到 GitHub 登录名的解析。 | 被 `agent/server.py` 等运行入口使用；读取 `profiles.py`、`user_mappings.py` 的数据。 |
| `options.py` | 集中列出支持哪些模型、每个模型支持哪些 effort/图像能力、上下文窗口多大，以及不兼容时如何降级。 | 被个人设置接口和模型构造逻辑使用；主要是静态配置。 |
| `enabled_repos.py` | 记录哪些仓库开启了自动 PR 评审。它相当于评审功能的仓库白名单。 | 被 `routes.py`、Webhook/评审逻辑使用；读写 LangGraph Store。 |
| `review_styles.py` | 保存每个仓库的评审风格配置和自定义提示词，例如希望评审更关注安全还是性能。 | 被评审路由、分析任务使用；读写 LangGraph Store。 |
| `team_settings.py` | 保存整个团队的默认模型、子 Agent 模型和评审相关开关。个人设置没有覆盖时就用这里的默认值。 | 被 `routes.py`、Agent/评审启动逻辑使用；读写 LangGraph Store。 |
| `team_credentials.py` | 管理团队级第三方凭据（目前是 Datadog、LangSmith），负责连接、断开和脱敏展示。 | 被管理员路由调用；凭据加密后写入 LangGraph Store。 |
| `user_mappings.py` | 维护 GitHub 登录名、公司邮箱、Slack 用户 ID 三者之间的对应关系。它还维护进程内缓存，方便同步代码快速查。 | 被 Slack OAuth、消息信任校验、提交作者解析等逻辑使用；持久数据在 LangGraph Store，热点查询走内存缓存。 |
| `skills.py` | 管理用户自定义 Agent Skill。每个 Skill 最终会被整理成一个虚拟的 `/技能名/SKILL.md` 文件，供 Agent 读取。 | 被技能管理接口和 Agent 的技能后端使用；读写 LangGraph Store。 |

## 4. Agent 线程、计划和用量

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `thread_api.py` | 给仪表盘提供 Agent 对话页面需要的接口：列线程、读历史、发消息、流式收消息、取消运行、看状态和代码差异。 | 被 `routes.py` 调用；读写 LangGraph `threads/runs`，并访问沙箱和消息流。 |
| `plan_api.py` | 处理计划审批页面的请求：查看计划、修改计划、发表评论、批准或拒绝计划。 | 被 `routes.py` 调用；读取线程元数据，调用 `plan_store.py`，必要时向 Slack 发通知。 |
| `plan_store.py` | 计划的真正存储层。计划正文既保存到 LangGraph Store，也写入对应沙箱的 `/workspace/plans/`，评论另存为线程相关记录。 | 被 `plan_api.py`、Agent 工具使用；读写 LangGraph Store、LangGraph 线程元数据和沙箱文件。 |
| `agent_usage.py` | 统计 Agent 使用量和评审贡献，例如处理了多少线程、多少 PR，并生成排行榜缓存。 | 被仪表盘和后台刷新任务使用；读写 LangGraph Store，刷新 PR 数据时调用 GitHub API。 |

## 5. PR 评审系统

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `review_api.py` | 给 PR 评审页面提供数据：评审列表、单个评审、发现的问题、PR 详情和 diff，还能触发重新评审、代理 PR 图片。 | 被 `routes.py` 调用；读 reviewer 线程状态，访问 GitHub API，并触发 reviewer graph。 |
| `review_chat_api.py` | 处理评审页面里的聊天窗口，让用户围绕某个 PR 评审继续提问、读取历史、接收流式回答。 | 被 `routes.py` 调用；代理 LangGraph 线程命令、历史和事件流。 |
| `pr_diff.py` | 从 GitHub 拉取 PR 的文件差异，并整理成评审 UI 能直接展示的文件列表。 | 被 `review_api.py` 使用；只读 GitHub API。 |
| `review_style_jobs.py` | 启动、取消和同步“分析这个仓库评审风格”的后台 Agent 任务。 | 被 `routes.py` 和评审风格页面使用；创建/查询 LangGraph runs，结果由分析器写回 Store。 |
| `analyzer_cron.py` | 给已经完成首次分析的仓库注册或删除每日持续学习任务，让评审风格能随着新 PR 继续更新。 | 被 `review_style_jobs.py`、`routes.py` 使用；通过 LangGraph cron API 管理定时任务。 |
| `eval_jobs.py` | 保存 reviewer 评估任务的心跳和状态，给管理员页面展示“评估是否正在运行、多久没更新”。 | 被管理员路由和 GitHub Action 使用；读写 LangGraph Store。 |

## 6. 自动修复、调度和仓库运行环境

| 文件 | 通俗作用 | 主要使用/读写 |
| --- | --- | --- |
| `autofix_state.py` | 保存某个 PR 的 CI 自动修复开关。用户可以对单个 PR 执行 `autofix on/off`，这里记录关闭状态。 | 被 `agent/ci_autofix.py` 和仪表盘路由使用；读写 LangGraph Store。 |
| `schedules.py` | 管理延迟执行或周期执行的 Agent 任务，例如稍后提醒 Agent 再跑一次。 | 被 `routes.py` 和调度工具使用；通过 LangGraph cron API 创建、更新、删除任务。 |
| `repo_access.py` | 在涉及私有仓库的操作前，拿当前用户 token 去 GitHub 验证确实有权限；token 过期时会尝试刷新一次。 | 被仓库选择、评审等路由使用；只读 GitHub API，不单独保存权限结果。 |
| `repo_cache.py` | 缓存用户可选仓库列表。GitHub 安装量大时，先返回缓存，过期后后台刷新，避免页面每次等十几秒。 | 被 `routes.py` 的 `/repos` 使用；读写 LangGraph Store，进程内记录正在刷新的用户。 |
| `repo_snapshots.py` | 管理“每个仓库一套定制沙箱镜像”：保存 Dockerfile、构建参数和构建状态，并在 LangSmith 沙箱里构建快照。 | 被管理员路由和沙箱创建逻辑使用；配置写 LangGraph Store，构建在远端沙箱完成。 |
| `workflow_approval.py` | 负责工作流文件审批的核心逻辑：检查待审批内容、校验目标仓库和分支，并准备提交/推送所需信息。 | 被 `workflow_approval_api.py` 和路由调用；访问 GitHub、线程元数据和 Store。 |
| `workflow_approval_api.py` | 把工作流审批核心逻辑包装成仪表盘可调用的接口，例如列出待审批项、批准或拒绝。 | 被 `routes.py` 注册；调用 `workflow_approval.py`，读写相关 Store/线程状态。 |

## 常见修改入口

- 改登录、Cookie、OAuth 回调：先看 `oauth.py`，再看 `routes.py` 对应的 `/auth/*` 路由。
- 改个人模型、默认仓库或个人指令：看 `profiles.py`、`agent_overrides.py`、`user_instructions.py` 以及 `routes.py`。
- 改 Agent 对话页面：看 `thread_api.py`；计划审批还要同时看 `plan_api.py` 和 `plan_store.py`。
- 改 PR 评审页面：看 `review_api.py`、`review_chat_api.py`、`pr_diff.py`；改评审风格学习则再看 `review_style_jobs.py` 和 `analyzer_cron.py`。
- 改仓库权限或仓库选择器：看 `repo_access.py`、`repo_cache.py`；不要把缓存当授权依据，真正权限必须走 `repo_access.py` 的实时 GitHub 校验。
- 改团队级默认值或第三方连接：看 `team_settings.py`、`team_credentials.py`。
- 改 CI 自动修复开关或延迟任务：看 `autofix_state.py`、`schedules.py`。

## 一句话总结

`agent/dashboard` 可以理解成“仪表盘的后端业务目录”：`routes.py` 负责接待请求，其他文件各管一类业务；数据主要落在 LangGraph Store，线程由 LangGraph 管，外部身份和仓库信息由 GitHub/Slack/Notion 提供，计划文件和镜像构建则落到沙箱体系里。

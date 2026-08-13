# `agent/tools` 中文导读

## 先用一句话理解

`agent/tools/` 是 Agent 可以调用的“动作库”。模型本身只会生成文字和工具调用请求，真正访问 GitHub、Linear、Slack、网页、计划存储和评审数据的代码，都在这里完成。

```text
模型判断：我需要查 Linear
        |
        v
调用 linear_get_issue
        |
        v
工具访问 Linear API
        |
        v
把结果返回给模型
        |
        v
模型继续思考
```

工具不是全部自动装进每个 Agent。不同 Agent 在自己的工厂里选择不同工具：

| Agent | 主要工具范围 |
| --- | --- |
| 主编码 Agent（`server.py`） | 网页、计划、Linear、GitHub PR、沙箱、Slack、定时唤醒、用户设置；集成工具还会动态加载。读写文件、执行命令、`task` 等基础工具由 Deep Agents 自动加入。 |
| Reviewer（`reviewer.py`） | PR diff、finding 增删改、发布评审、评审线程回复、网页和 HTTP；没有主编码 Agent 的提交/改代码工具。 |
| PR Chat（`chat.py`） | `read_repo_file`、`search_repo_code`、`list_review_findings`、网页搜索和 URL 读取；明确只读。 |
| Analyzer（`analyzer.py`） | `read_finding_outcomes`、`save_review_style_prompt`；学习并保存仓库评审风格。 |

## 工具是怎么接上的

### 1. `__init__.py` 统一导出

`agent/tools/__init__.py` 维护一个名字到模块的映射，例如：

```python
"open_pull_request": ".open_pull_request"
```

当 `server.py` 写：

```python
from .tools import open_pull_request
```

`__init__.py` 才按需加载真正的模块。好处是启动时不用一次性导入全部第三方 SDK，也让 Agent 工厂可以从一个位置拿到工具。

### 2. Agent 工厂把工具放进清单

主 Agent 会把工具放入 `static_tools`，Reviewer 和 PR Chat 则在自己的 `tools=[...]` 中明确列出。只有出现在对应清单里的工具，模型才会看到并能调用。

### 3. 工具调用会经过 middleware

工具真正执行前后还会经过 `agent/middleware/`，例如：

- `SanitizeToolInputsMiddleware`：修正错误参数。
- `ToolErrorMiddleware`：把异常变成 ToolMessage。
- `PlanModeMiddleware`：计划模式下隐藏危险工具。
- `PullRequestCreationGuardMiddleware`：阻止绕过标准工具直接开 PR。

所以工具文件负责“做动作”，middleware 负责“检查这个动作能不能做、失败后怎么处理”。

## 文件逐个说明

### A. 工具导出和内部辅助

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `__init__.py` | 工具总目录和懒加载出口。把 30 多个工具统一导出，真正访问某个名字时才导入对应模块。 | Agent 工厂通过 `from .tools import ...` 使用。 |
| `_sandbox_output.py` | 处理网页或 HTTP 返回内容过大的问题：把长文本写进沙箱文件，并返回分块/文件路径，避免一次性塞爆模型上下文。 | `http_request.py`、`web_search.py` 使用；不是模型直接调用的独立工具。 |

### B. 网页和 HTTP

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `web_search.py` | 使用 Exa 搜索网页，返回标题、链接和内容摘要；结果过长时写入沙箱。 | 主 Agent、Reviewer、PR Chat 使用；Analyzer 通常不需要。 |
| `fetch_url.py` | 抓取一个 HTTP/HTTPS 页面，把 HTML 转成更容易阅读的 Markdown；带安全跳转和长度限制。 | 主 Agent、Reviewer、PR Chat 使用。 |
| `http_request.py` | 发起受安全策略约束的 HTTP 请求，支持 GET/POST 等方法、请求头和 JSON 数据；超大结果转存沙箱。 | 主 Agent 和 Reviewer 使用，给需要访问外部 API 的任务使用。 |

### C. 计划模式和计划审批

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `enter_plan_mode.py` | 把当前运行切换到只读计划模式，并把计划状态写入线程/计划存储。之后 middleware 会隐藏修改类工具。 | 主 Agent 使用。 |
| `save_plan.py` | 读取 Agent 在沙箱 `/workspace/plans/` 下写出的 Markdown 计划，把它发布到 Dashboard 的计划页面；计划模式下可等待用户审批。 | 主 Agent 使用。 |
| `approve_plan.py` | 用户批准计划后退出计划模式，整理审批意见，并触发后续实现运行。 | 主要由计划审批流程调用，也作为 Agent 工具暴露给主 Agent。 |

典型流程：

```text
enter_plan_mode
      ↓
Agent 只读研究并写计划文件
      ↓
save_plan
      ↓
用户在 Dashboard 审批
      ↓
approve_plan
      ↓
重新启动实现阶段
```

### D. GitHub PR 和代码评审

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `open_pull_request.py` | 按当前触发用户身份打开或更新 GitHub PR，处理仓库权限、分支、重复 PR、计划链接、来源链接和使用量记录。 | 主 Agent 使用，是标准开 PR 入口。 |
| `request_pr_review.py` | 根据 PR URL 请求 Open SWE Reviewer 运行；内部会转给 GitHub webhook 的评审触发逻辑。 | 主 Agent 使用。 |
| `fetch_review_diff.py` | 把当前 PR 的评审 diff 写入 Reviewer 沙箱，并返回文件数量、范围等受限元数据。 | Reviewer 使用。 |
| `add_finding.py` | 在 Reviewer 线程中新增一条评审问题，校验严重级别、置信度、文件和行号是否真的在 diff 里。 | Reviewer 使用。 |
| `list_findings.py` | 查看当前 Reviewer 线程中已经保存的 findings，可按状态过滤。 | Reviewer 使用，尤其是重新评审时。 |
| `update_finding.py` | 修改现有 finding 的标题、严重级别、位置、说明或状态；用于标记已修复、误报或内容变化。 | Reviewer 使用。 |
| `publish_review.py` | 把 findings 汇总成 GitHub PR Review，发布行内评论、更新 Review Check、写回 finding 与线程关联信息。 | Reviewer 使用，是评审流程的最终提交动作。 |
| `resolve_finding_thread.py` | 根据 finding 处理 GitHub 行内评论线程的解决或关闭，并更新 finding 表面状态。 | Reviewer 使用。 |
| `reply_to_finding_thread.py` | 对某个已有评审评论线程发送普通回复，同时记录交互。 | Reviewer 使用，适合回答作者追问；不用于正式解决/驳回。 |
| `list_review_findings.py` | 读取 PR 的正式评审 findings，并返回适合聊天上下文的精简版本。 | PR Chat 使用。它读取的是 canonical reviewer thread，不是自己的聊天线程。 |
| `read_finding_outcomes.py` | 读取过去哪些 finding 被确认/修复，哪些被驳回/认为误报。 | Analyzer 使用，用来学习仓库真正重视的评审问题。 |

评审工具的大致关系：

```text
fetch_review_diff
      ↓
Reviewer 阅读 diff
      ↓
add_finding / update_finding
      ↓
list_findings 检查当前状态
      ↓
publish_review
      ↓
GitHub PR Review + 行内评论
```

### E. PR Chat 的只读仓库工具

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `read_repo_file.py` | 不依赖沙箱，直接通过 GitHub Contents API 读取 PR 仓库在指定 ref 上的文件或目录。 | PR Chat 使用。 |
| `search_repo_code.py` | 通过 GitHub Code Search API 在当前 PR 仓库中搜索符号或文本。 | PR Chat 使用。 |

PR Chat 只有这类只读工具，因此它能解释代码，但不能执行命令、写文件或提交代码。

### F. Linear 工具

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `linear_comment.py` | 给 Linear issue 发评论，汇报进展、回答问题或附上 PR 链接。 | 主 Agent 使用。 |
| `linear_create_issue.py` | 在指定 Linear team 中创建 issue，可设置描述、负责人、优先级、状态、标签和项目。 | 主 Agent 使用。 |
| `linear_delete_issue.py` | 删除一个 Linear issue。 | 主 Agent 使用，属于破坏性操作，只有明确需要时才应调用。 |
| `linear_get_issue.py` | 按 ID 获取完整 Linear issue。 | 主 Agent 使用。 |
| `linear_get_issue_comments.py` | 获取某个 Linear issue 的评论列表。 | 主 Agent 使用。 |
| `linear_list_teams.py` | 列出当前 Linear workspace 的 team，返回 team ID、名称和 key。 | 主 Agent 使用，创建 issue 前常用。 |
| `linear_search_issues.py` | 按关键词、team、状态、标签、项目、负责人等条件搜索 Linear issues，也可包含评论。 | 主 Agent 使用。 |
| `linear_update_issue.py` | 更新已有 issue 的标题、描述、负责人、优先级、状态、标签等。 | 主 Agent 使用。 |

这些文件大多是对 `agent/utils/linear.py` 的薄包装：工具负责把函数暴露给模型，底层 API 细节放在 utils 中。

### G. Slack 工具

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `slack_thread_reply.py` | 回复当前 Slack 线程，保存消息与 run 的映射，并支持选项按钮和工作流审批卡片。 | 主 Agent 使用。 |
| `slack_read_thread_messages.py` | 读取 Slack 线程历史，补齐用户名称并格式化成模型容易理解的上下文。 | 主 Agent 使用。 |
| `slack_add_reaction.py` | 给触发消息加合适的表情，例如正在调查、已处理或确认接手。 | 主 Agent 使用。 |
| `slack_start_new_thread.py` | 从当前 Agent 运行发起一个新的 Slack 顶层线程，创建对应 LangGraph thread 并派发新的 Agent run。 | 主 Agent 使用。 |

`slack_thread_reply.py` 负责当前线程内回复，`slack_start_new_thread.py` 负责另起一个独立任务，别把两者混用。

### H. 用户偏好和技能

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `save_user_instructions.py` | 保存用户明确提出的长期行为偏好，例如希望以后都使用某种代码风格。 | 主 Agent 使用；写入用户级指令存储。 |
| `user_skills.py` | 创建、更新或删除当前用户的 Skill；Skill 会以虚拟 `SKILL.md` 文件形式供 Agent 使用。 | 主 Agent 使用；底层调用 `dashboard/skills.py`。 |

### I. 沙箱、平台和自动化

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `recreate_sandbox.py` | 放弃当前线程绑定的沙箱，重新绑定一个全新的沙箱。旧沙箱不会删除，但当前线程不再使用它。 | 主 Agent 使用；只有当前沙箱确实不可用时才需要。 |
| `report_platform_issue.py` | 为沙箱或执行环境问题生成一个平台问题报告 ID。它不直接修复问题，只给后续支持流程一个编号。 | 主 Agent 使用。 |
| `schedule_thread_wakeup.py` | 创建一次性的 LangGraph cron，在未来某个时间重新触发当前 Agent 线程，可附带提醒 prompt。 | 主 Agent 使用。 |

### J. 评审风格分析

| 文件 | 通俗作用 | 谁会用 |
| --- | --- | --- |
| `save_review_style.py` | 保存 Analyzer 生成的仓库评审风格提示词，并在首次完成后确保仓库有持续学习 cron。 | Analyzer 使用。 |

## 工具的权限边界

工具大致分三类：

```text
只读工具
    web_search、fetch_url、read_repo_file、search_repo_code、
    linear_get_*、linear_search_issues、list_findings 等

写外部系统工具
    open_pull_request、linear_update_issue、linear_comment、
    slack_thread_reply、publish_review、schedule_thread_wakeup 等

高风险或改变运行状态的工具
    linear_delete_issue、recreate_sandbox、approve_plan、
    enter_plan_mode、resolve_finding_thread 等
```

工具自身还会做权限检查、用户归属检查、仓库权限检查和参数校验；middleware 会再做一层统一保护。比如计划模式下，写外部系统工具会被隐藏；PR Chat 则根本不注册写工具。

## 工具调用失败时会怎样

```text
模型请求工具
      ↓
ToolErrorMiddleware 捕获异常
      ↓
异常转换成 ToolMessage
      ↓
模型看到“工具失败”的结果
      ↓
模型决定重试、换方案或告诉用户
```

网页和 HTTP 工具还会限制响应大小；沙箱相关工具会把大输出写入沙箱文件，避免上下文被一大段日志撑爆。

## 一个完整例子：从需求到 PR

```text
用户：修好 bug 并开一个 PR
        |
        v
主 Agent 使用 Deep Agents 内置 read_file / edit_file / execute
        |
        v
需要查外部资料 -> web_search / fetch_url
        |
        v
需要更新 Linear -> linear_comment / linear_update_issue
        |
        v
完成代码 -> open_pull_request
        |
        v
请求评审 -> request_pr_review
        |
        v
Slack/Linear 汇报 -> slack_thread_reply / linear_comment
```

这里要注意：文件读写和命令执行工具不是 `agent/tools` 里的文件，它们是 Deep Agents 自动加入的内置工具；`agent/tools` 主要放 Open SWE 自己封装的业务动作。

## 最后总结

`agent/tools` 可以理解成 Open SWE 的“手和脚”：

- `server.py` 决定主 Agent 可以拿哪些工具。
- `reviewer.py` 只拿评审相关工具。
- `chat.py` 只拿只读工具。
- `analyzer.py` 只拿学习和保存评审风格的工具。
- `__init__.py` 负责统一、懒加载地导出工具。
- 每个工具文件负责一个具体动作，底层复杂 API 通常继续放在 `agent/utils/`、`agent/review/` 或 `agent/dashboard/`。

因此，新增一个工具通常要做三件事：写工具文件、在 `agent/tools/__init__.py` 导出、把它加入对应 Agent 工厂的 `tools=[...]` 清单；只写文件但不加入清单，模型实际上看不到它。

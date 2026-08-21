# 04. 把能力关进工具与中间件

## 学习目标

能区分“工具定义了 Agent 能做什么”和“中间件决定它在什么条件下能做”，并能为写操作设计最小安全控制。

## 概念全解

模型输出的不是可靠程序。工具调用是模型提出的动作请求，系统必须在真正执行前做检查。最实用的分层是：

- 工具层回答“能做哪些具体动作”，例如 `open_pull_request`、`linear_comment`、`slack_thread_reply`、HTTP/搜索工具。
- Deep Agents 内置工具回答“文件和任务如何操作”，例如 `read_file`、`edit_file`、`execute`、`task`。
- 中间件回答“什么输入、什么时机、什么权限可以做”，例如限步、超时、异常包装、计划模式和 PR 创建守卫。

重点不是“工具越多越聪明”。工具越多，模型选择空间越大、误调用概率越高、权限审计越难。Open SWE 在 `get_agent` 中维护明确的 `static_tools`，将 Deep Agents 的文件工具交给框架自动提供，而不是重复注册。

## 架构图

![Agent 设计蓝图中的工具与中间件层](architecture/png/02-agent-design-blueprint.png)

[Draw.io](architecture/02-agent-design-blueprint.drawio) · [HTML](architecture/html/02-agent-design-blueprint.html)

紫色层底部的横条是教学抽象，不表示单个 middleware：真实栈在 [agent/server.py](../../agent/server.py:1201) 按顺序配置，顺序会改变行为。

- 统一工具错误：[agent/middleware/tool_error_handler.py](../../agent/middleware/tool_error_handler.py:147) 的 `ToolErrorMiddleware` 把异常转为模型可理解的 `ToolMessage`，不让整次 run 直接崩掉。
- 计划模式：[agent/middleware/plan_mode.py](../../agent/middleware/plan_mode.py:40) 的 `PlanModeMiddleware` 在计划阶段从模型可见工具中移除写操作。
- PR 守卫：[agent/middleware/pr_creation_guard.py](../../agent/middleware/pr_creation_guard.py:257) 的 `PullRequestCreationGuardMiddleware` 阻止用 Shell、`curl` 或 `gh` 绕过有归属的 PR 工具。
- 模型超时：[agent/middleware/model_call_timeout.py](../../agent/middleware/model_call_timeout.py:44) 的 `ModelCallTimeoutMiddleware` 避免模型连接无限挂住。

## 项目中的完整路径

模型想“创建 PR”时，正确路径不是让它随便 `execute("gh pr create")`，而是调用 `open_pull_request`。专用工具能记录触发用户、处理参数、统一错误并保留审计语义。`PullRequestCreationGuardMiddleware` 再拦住常见的 Shell 绕过路径。

```text
模型提出 execute("gh pr create ...")
  -> PR 守卫检查命令
  -> 返回受控错误 ToolMessage
  -> 模型改用 open_pull_request
  -> 工具负责归属、创建和结果返回
```

同样，工具报错不是让服务抛 500 后终止，而是变成 `ToolMessage(status="error")`。模型能根据“认证失败”“Sandbox 不可达”“参数错误”选择修正、降级或向用户说明阻塞。

## 最小可运行示例

从最少工具开始。比如一个知识库 Agent：

```text
read_document(id)       只读，允许
search_documents(query) 只读，允许
draft_answer(text)      只生成草稿，允许
send_email(...)         禁止直接暴露；改为 create_send_request(...) 等待审批
```

再给每个写工具定义三件事：调用者身份、资源范围、幂等键。例如“发邮件”应带 `recipient`、`template_id`、`approval_id`，而不是一段任意 shell 命令。

## 常见误区与反例

1. 提示词写“不要危险操作”但仍暴露万能 Shell：这是建议，不是控制。
2. 把所有异常吞掉并回复“失败了”：模型和用户都不知道该重试、改参数还是升级人工。
3. 用工具名作为唯一权限判断：同一工具在不同用户、仓库、计划阶段应有不同策略。

## 扩展边界与练习

- 小型内部 Agent：先将读、写、外部副作用拆成三类工具就足够。
- 高风险场景：将策略放到服务端，加入审批 token、资源 allowlist、审计事件和速率限制。

练习：挑一个你要暴露给模型的写操作，设计一个专用工具参数。要求它不能接受任意命令字符串，并能在日志中说明“谁对哪个资源做了什么”。

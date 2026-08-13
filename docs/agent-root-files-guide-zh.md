# `agent` 根目录文件中文导读

本文只解释 `agent/` 根目录下的文件，不展开 `agent/dashboard/`、`agent/tools/`、`agent/middleware/` 等子目录。

先记住一个边界：

```text
agent/server.py   = 普通编码 Agent 的核心组装入口
agent/reviewer.py = PR 评审 Agent 的核心组装入口
agent/analyzer.py = 评审风格分析 Agent 的核心组装入口
agent/chat.py     = PR 问答 Agent 的核心组装入口

其他根目录文件   = 运行入口、调度、提示词、加密、故障收尾等支撑代码
```

## 整体关系

```text
LangGraph Runtime
       |
       +--> agent.graphs.agent  ------> agent/server.py      主编码 Agent
       +--> agent.graphs.reviewer ---> agent/reviewer.py    PR 评审 Agent
       +--> agent.graphs.analyzer ----> agent/analyzer.py    评审风格分析 Agent
       +--> agent.graphs.chat --------> agent/chat.py        PR 只读问答 Agent
       +--> agent.graphs.scheduler ---> agent/scheduler.py   定时任务分发器
       |
       +--> agent/dispatch.py      统一创建和派发运行
       +--> agent/completion.py    运行失败后的补偿通知
       +--> agent/reconcile.py     清理卡死的运行
       +--> agent/prompt.py        组装系统提示词
       +--> agent/encryption.py    加密敏感 token
       +--> agent/webapp.py        暴露 FastAPI 应用
```

## 文件逐个说明

| 文件 | 通俗作用 | 它是不是 Agent 核心 |
| --- | --- | --- |
| `server.py` | Open SWE 主编码 Agent 的“总装车间”。它解析用户、团队和线程配置，选择模型，准备或重连沙箱，加载工具和外部集成，拼出系统提示词，再用 `create_deep_agent` 创建真正能读写代码、执行命令、操作 GitHub 的 Agent。 | 是，普通编码 Agent 的核心入口。 |
| `reviewer.py` | PR 评审 Agent 的“专用装配线”。它准备 PR checkout 和 diff，限制评审只能关注改动行，加载 `add_finding`、`publish_review` 等评审工具，并创建只读评审 Agent。 | 是，评审 Agent 的核心入口。 |
| `analyzer.py` | 评审风格分析 Agent。它读取历史 PR 评审和过去的 finding 结果，分析这个仓库通常应该报什么问题、跳过什么问题，然后保存成仓库专属评审风格提示词。 | 是，分析 Agent 的核心入口。 |
| `chat.py` | PR 页面里的只读聊天 Agent。它围绕一个 PR 的 diff、评审发现和仓库文件回答问题，但没有沙箱，不允许执行命令、改文件、提交代码或开 PR。 | 是，PR 问答 Agent 的核心入口。 |
| `scheduler.py` | LangGraph 的定时任务入口。每次 cron tick 进来后，它判断是启动一个用户安排的 Agent 任务，还是执行 stale run 清理，然后调用对应逻辑。它自己不思考代码。 | 不是思考型 Agent，是调度图。 |
| `dispatch.py` | 统一的“发车调度器”。Slack、Linear、GitHub、Dashboard 等来源都通过它创建 LangGraph run；它统一设置可恢复流、同步 checkpoint、并发策略和完成 webhook。 | 不是 Agent，是运行调度层。 |
| `completion.py` | 运行完成 webhook 的处理器。某次运行因为 `error` 或 `timeout` 失败时，它向原来的 Slack、Linear 或 GitHub 位置发一条失败通知；评审运行失败时还会收尾 GitHub Check。 | 不是 Agent，是失败补偿层。 |
| `reconcile.py` | 卡死运行的“清道夫”。如果某个线程一直 busy，里面的 pending run 超过规定时间，它就把这些运行取消，避免线程永久占用。 | 不是 Agent，是后台清理任务。 |
| `prompt.py` | 系统提示词工厂。它把通用 Open SWE 规则、仓库指令、用户指令、计划模式、来源渠道和当前沙箱目录拼成最终 system prompt。 | 不是 Agent，但直接决定 Agent 的行为边界。 |
| `encryption.py` | 敏感 token 的保险箱。使用 `TOKEN_ENCRYPTION_KEY` 和 Fernet/MultiFernet 加密、解密 OAuth token 等凭据，并支持密钥轮换。 | 不是 Agent，是安全基础设施。 |
| `webapp.py` | FastAPI 兼容入口。它从 `agent.api.app` 导出 `app`，让 Uvicorn 或 LangGraph Runtime 能找到 Web 服务。路由本身不在这里实现。 | 不是 Agent，是 Web 应用入口。 |

## 1. `server.py`：普通编码 Agent 的核心

用户在普通 Agents 页面或 Slack、Linear、GitHub 中发起编码任务，最后会进入 `server.py` 的 `get_agent(config)`。

它主要做这些事：

1. 读取线程 ID、用户、仓库、团队和个人配置。
2. 决定模型和 reasoning effort。
3. 创建或重连当前线程专属沙箱。
4. 配置 GitHub 代理和 git 提交身份。
5. 加载内置工具和可选的 Datadog、LangSmith、Notion、浏览器等集成。
6. 组装系统提示词。
7. 添加中间件，例如错误处理、超时、计划模式、消息队列和模型 fallback。
8. 调用 `create_deep_agent(...)` 返回真正执行任务的图。

它最后导出：

```python
traced_agent = traced_graph_factory(get_agent, AGENT_TRACING_PROJECT)
```

`langgraph.json` 中的 `agent` 图最终指向这个 `traced_agent`。

## 2. `reviewer.py`：只做 PR 评审的 Agent

`reviewer.py` 和 `server.py` 都会创建 Deep Agent，但权限完全不同。

评审 Agent 会：

- 准备目标 PR 的仓库和 diff。
- 只允许在 diff 改动行上创建 finding。
- 使用 `add_finding`、`update_finding`、`publish_review` 等评审工具。
- 可以重新检查已有 finding 是否已修复或应当撤销。
- 不提供主编码 Agent 的提交、推送、开 PR 工具。

所以它的职责是“看代码并发布评审结果”，不是替用户修改代码。

## 3. `analyzer.py`：学习仓库评审风格

这个 Agent 不直接评审当前 PR，而是分析一个仓库过去的评审记录：

```text
历史人工评审
      + 当前 reviewer 过去的 finding 结果
      ↓
analyzer.py
      ↓
仓库专属 review-style prompt
      ↓
reviewer.py 下次评审时使用
```

它有两种常见模式：

- `bootstrap`：第一次建立仓库评审风格。
- `continual`：定期根据新结果持续修正风格。

分析结果通过 `save_review_style_prompt` 保存，而不是直接修改 reviewer 代码。

## 4. `chat.py`：PR 只读问答 Agent

这是评审页面“和这个 PR 聊天”使用的图。它和主编码 Agent 的差异很明确：

| 能力 | 主编码 Agent | PR Chat Agent |
| --- | --- | --- |
| 读取代码 | 可以 | 可以 |
| 运行命令 | 可以 | 不可以 |
| 修改文件 | 可以 | 不可以 |
| 提交/推送 | 可以 | 不可以 |
| 主要上下文 | 当前任务和沙箱 | PR diff、评审 findings、仓库文件 |

`chat.py` 通过 `create_deep_agent` 创建一个没有沙箱的只读 Agent，并显式排除：

```text
execute
write_file
edit_file
delete
```

它的目标是解释评审结果和代码，不是实施修复。

## 5. `scheduler.py`：定时任务的薄外壳

`scheduler.py` 自己只有一个很小的 LangGraph：

```text
START -> launch -> END
```

`launch` 根据任务类型选择：

- `reconcile`：调用 `reconcile_stale_runs()` 清理卡死运行。
- 普通 schedule：调用 `launch_scheduled_agent_run(schedule_id)` 启动用户安排的 Agent 任务。

真正的计划配置和任务启动细节在 `agent/dashboard/schedules.py`，不在这个文件里。

## 6. `dispatch.py`：所有入口共用的运行派发规则

这个文件解决的是“怎么可靠地启动一次 LangGraph 运行”，不是“Agent 怎么思考”。

`dispatch_agent_run(...)` 和 `create_durable_run(...)` 统一提供：

- `multitask_strategy="interrupt"`：新消息可以中断并接着旧运行继续。
- `durability="sync"`：每一步及时保存 checkpoint。
- `stream_resumable=True`：前端后来接入也能补看事件。
- `webhook=COMPLETION_WEBHOOK_URL`：运行结束时通知本项目。
- `prepare_run_id`：让一次准备流程有稳定标识。

调用方只需要告诉它线程 ID、assistant ID、输入和来源，剩下的运行参数由这里统一处理。

## 7. `completion.py`：失败时给用户一个明确结果

正常情况下，Agent 会自己在 Slack、Linear、GitHub 或 Dashboard 回复用户。但如果进程重启、模型超时或运行报错，Agent 可能来不及回复。

LangGraph Runtime 会调用 `/webhooks/run-complete`，`completion.py` 会：

1. 校验 webhook secret。
2. 判断状态是不是 `error` 或 `timeout`。
3. 按线程来源找到原来的 Slack、Linear 或 GitHub 位置。
4. 发出一条简短失败消息。
5. 对同一次 run 做幂等去重，避免重复通知。
6. 如果失败的是 reviewer，尽量把 GitHub Check 收尾为失败或中性状态。

## 8. `reconcile.py`：处理平台没有收尾的 pending run

`completion.py` 依赖完成 webhook。如果平台崩溃或 webhook 丢失，某个 run 可能永远停在 `pending`，线程也会一直显示 busy。

`reconcile_stale_runs()` 会定期：

```text
查找 busy 线程
  ↓
查找其中过旧的 pending run
  ↓
批量 cancel
  ↓
释放线程
```

它是兜底清理，不负责重试 Agent，也不会修改业务代码。

## 9. `prompt.py`：把各种规则合成一个 system prompt

Agent 真正看到的 system prompt 不是一段固定字符串，而是多个来源拼起来的：

```text
通用 Open SWE 规则
      + 仓库级 Agent 指令
      + 用户级 Agent 指令
      + 当前沙箱目录
      + Slack/Linear/GitHub 来源信息
      + 计划模式说明
      + 默认仓库和 PR 规则
      ↓
construct_system_prompt(...)
      ↓
最终 system prompt
```

`OPEN_SWE_SHARED_BASE` 是通用底座，`construct_system_prompt()` 负责把当前运行相关内容填进去。它不创建 Agent，但会强烈影响 Agent 的行为。

## 10. `encryption.py`：保存 token 前先加密

OAuth token、第三方凭据等敏感数据不能明文保存。`encrypt_token()` 使用当前最新密钥加密，`decrypt_token()` 会按配置的密钥列表尝试解密。

配置项：

```text
TOKEN_ENCRYPTION_KEY=key1,key2
```

多密钥设计用于密钥轮换：新数据使用第一把密钥，旧数据仍可以用后面的旧密钥解密。

## 11. `webapp.py`：让服务器找到 FastAPI app

它的代码很简单：

```python
from .api.app import app
```

它只是兼容入口。真正创建 FastAPI 实例、挂载 dashboard、Webhook 和健康检查路由的是 `agent/api/app.py`。

## 常见问题

## `agent/graphs` 这层为什么值得保留

`agent/graphs/*.py` 基本都是很薄的转出口文件，例如：

```python
from agent.server import get_agent, traced_agent

__all__ = ["get_agent", "traced_agent"]
```

它们看起来像“多绕了一层”，但这层解决的是入口管理问题，不是 Agent 推理问题。

### 1. 给 LangGraph 一个稳定门牌号

`langgraph.json` 只依赖这些公开入口：

```text
agent.graphs.agent
agent.graphs.reviewer
agent.graphs.analyzer
agent.graphs.chat
agent.graphs.scheduler
```

真正的实现可以继续放在 `server.py`、`reviewer.py`、`analyzer.py`、`chat.py` 和 `scheduler.py` 中。以后内部拆文件，只要 `agent/graphs/*.py` 仍然导出同名对象，部署配置就不用跟着改。

### 2. 把“图的注册”与“图的实现”分开

```text
agent/graphs/
    负责：有哪些图、对外暴露什么入口

agent/server.py 等
    负责：模型、工具、沙箱、提示词和中间件怎么组装
```

这样不会把 LangGraph 的部署配置和内部实现细节绑死在一起。

### 3. 统一暴露 tracing 后的版本

`server.py`、`reviewer.py`、`analyzer.py` 和 `chat.py` 会先用 `traced_graph_factory(...)` 包装图，再由 `agent/graphs` 导出 `traced_agent`、`traced_reviewer_agent` 等对象。

因此 LangGraph Runtime 加载到的是已经接入 LangSmith tracing 的公开版本，而不是未包装的内部工厂。

### 4. 给新增图提供固定模板

新增一张图时可以按同样步骤做：

1. 在 `agent/xxx.py` 实现图。
2. 在 `agent/graphs/xxx.py` 导出公开入口。
3. 在 `langgraph.json` 注册 `agent.graphs.xxx:traced_xxx`。

这让“实现、观测、注册”三件事的边界保持一致。

### 5. 这层不是绝对必需

理论上可以把 `langgraph.json` 直接写成 `agent.server:traced_agent`。保留 `agent/graphs` 的价值主要是稳定入口、统一 tracing 和降低配置对内部文件结构的依赖。它不是额外的业务逻辑，也不是重复创建了一份 Agent。

## `agent` 下还有哪些相似设计

有，但不是完全相同的复制。可以分成下面三类。

### A. 同样是“公开入口适配层”

`agent/graphs/*.py` 是这一类最标准的例子。它们把内部实现重新导出给 LangGraph 配置使用。

| 位置 | 对外暴露什么 | 作用 |
| --- | --- | --- |
| `agent/graphs/agent.py` | `get_agent`、`traced_agent` | 主编码图的稳定入口 |
| `agent/graphs/reviewer.py` | `get_reviewer_agent`、`traced_reviewer_agent` | PR 评审图的稳定入口 |
| `agent/graphs/analyzer.py` | `get_analyzer`、`traced_analyzer` | 风格分析图的稳定入口 |
| `agent/graphs/chat.py` | `get_chat_agent`、`traced_chat_agent` | PR 问答图的稳定入口 |
| `agent/graphs/scheduler.py` | `get_scheduler` | 定时调度图的稳定入口 |
| `agent/webapp.py` | `app` | 给 Uvicorn/LangGraph 找到 FastAPI 应用的兼容入口 |

`webapp.py` 和 `agent/graphs` 的思路很像：都是把真正实现藏在别处，给外部运行器一个稳定、简单的导入路径。区别是 `agent/graphs` 暴露 LangGraph 图，`webapp.py` 暴露 FastAPI 应用。

### B. 同样是“组装层”和“实现层”分开

`agent/api/app.py` 不是具体业务接口，而是把各个路由模块装进 FastAPI：

```text
agent/api/app.py
    +--> dashboard router
    +--> plan router
    +--> workflow approval router
    +--> GitHub webhook router
    +--> Linear webhook router
    +--> Slack webhook router
    +--> health router
```

具体接口仍然分别放在 `agent/dashboard/`、`agent/webhooks/` 和 `agent/api/health.py`。这和 `agent/graphs` 的共同点是“入口负责组装，业务放在专门模块”；区别是它组装的是 HTTP 路由，不是 LangGraph 图。

### C. 同样是“统一出口层”

以下 `__init__.py` 或汇总模块也有类似思想，但更轻：

| 文件 | 通俗作用 |
| --- | --- |
| `agent/api/__init__.py` | 标记 API 包，通常不承载业务逻辑。 |
| `agent/dashboard/__init__.py` | 延迟导出 `router`，避免导入一个小模块时顺手加载整套 FastAPI。 |
| `agent/integrations/__init__.py` | 统一导出常用集成类型，例如 LangSmith provider。 |
| `agent/middleware/__init__.py` | 把分散在多个文件里的中间件集中导出，`server.py` 可以从一个位置导入。 |
| `agent/tools/__init__.py` | 把工具集中导出，Agent 工具清单不必逐个深入工具文件。 |
| `agent/runtime/__init__.py` | 汇总运行时常量、沙箱和执行判断等公共接口。 |
| `agent/review/__init__.py` | 标记评审领域包，具体 diff、finding、发布和风格逻辑仍在子模块。 |
| `agent/webhooks/__init__.py` | 标记 Webhook 包，具体 GitHub、Linear、Slack 处理分别由对应模块负责。 |

这些文件通常不改变执行流程，主要解决导入路径、模块边界和依赖可见性问题。

## 这些设计分别解决什么问题

```text
agent/graphs/*.py
    解决：LangGraph 如何稳定找到每张图

agent/webapp.py
    解决：Web 服务器如何稳定找到 FastAPI app

agent/api/app.py
    解决：如何把多个路由模块组装成一个应用

各目录 __init__.py
    解决：如何集中导出、延迟导入和保持包边界

server.py / reviewer.py / chat.py 等
    解决：Agent 本身如何创建和运行
```

所以这不是“到处套一层”的随意设计，而是按运行边界拆开：外部运行器看到稳定入口，内部模块负责具体实现，汇总层负责把一组相关能力装配起来。

### 这些文件是不是都和 Agent 有关？

有关，但层次不同：

```text
直接创建 Agent 图：server.py / reviewer.py / analyzer.py / chat.py
调度 Agent 运行：scheduler.py / dispatch.py
影响 Agent 行为：prompt.py
支撑 Agent 稳定运行：completion.py / reconcile.py / encryption.py
提供 Web 服务入口：webapp.py
```

### `dashboard` 和这些根目录文件是什么关系？

可以把它们理解成：

```text
agent/dashboard
    = 仪表盘业务接口、配置和 LangGraph 请求代理

agent/server.py 等根目录图文件
    = 智能体图的组装和运行逻辑
```

例如前端发消息时，先经 `dashboard/routes.py` 和 `dashboard/thread_api.py`，再由 LangGraph Runtime 根据 `assistant_id="agent"` 找到 `server.py` 创建的主 Agent 图。

## 一句话总结

`agent/` 根目录不是一个单一模块，而是 Open SWE 的“运行骨架”：四个文件负责组装不同类型的 Agent，其他文件负责把运行可靠地派发出去、补齐提示词、处理失败和清理卡死任务，并把整个系统暴露成可启动的 FastAPI 服务。

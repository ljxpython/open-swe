# `traced_analyzer` 通俗讲解

## 一句话定位

`traced_analyzer` 是 Open SWE 的“Reviewer 教练”。

它不负责修改代码，不负责审查当前 PR，也不直接回复普通用户任务。它的工作是研究某个仓库过去的代码评审习惯，生成一段该仓库专属的评审提示词，让 Reviewer 后续评审时更像熟悉该团队规则的工程师。

```text
历史 PR 人工评论 + 过去 finding 的结果
                 |
                 v
          traced_analyzer
                 |
                 v
      仓库专属 reviewer 风格提示词
                 |
                 v
       Reviewer 评审 PR 时自动加载
```

## 它在哪里注册

[`langgraph.json`](../langgraph.json) 将 `analyzer` 注册为：

```json
"analyzer": "agent.graphs.analyzer:traced_analyzer"
```

[`agent/graphs/analyzer.py`](../agent/graphs/analyzer.py) 本身只是导出：

```python
from agent.analyzer import get_analyzer, traced_analyzer
```

真正的入口在 [`agent/analyzer.py`](../agent/analyzer.py)：

```python
traced_analyzer = traced_graph_factory(get_analyzer, REVIEW_TRACING_PROJECT)
```

它可以拆成两层理解：

```text
traced_analyzer
  -> 给运行套上 LangSmith tracing
  -> 调用 get_analyzer(config)
  -> 返回并运行一张 LangGraph 图
```

`traced` 的含义不是“分析 trace”，而是“这张图的运行过程会被记录到 LangSmith”。这里使用的 trace 项目是 `open-swe-review`，与 Reviewer 共用评审相关的可观测性空间。

## 它和主 Agent、Reviewer 的区别

| 角色 | 核心任务 | 输入 | 输出 | 会修改目标仓库吗 |
|---|---|---|---|---|
| 主 Agent | 解决用户提出的开发任务 | 用户消息、仓库、外部渠道上下文 | 代码修改、测试结果、PR、回复 | 会 |
| Reviewer | 评审当前某一个 PR | PR diff、仓库规则、评审工具 | finding 和 GitHub review | 不会 |
| `traced_analyzer` | 学习“这个仓库喜欢怎样评审” | 历史 PR 人工反馈、过去 finding 结果 | 仓库专属评审提示词 | 不会 |

所以可以把它们理解为：

```text
Analyzer：教 Reviewer 这个团队的标准
Reviewer：按标准检查某一个 PR
主 Agent：按用户任务修改代码
```

## 它仍然是 LangGraph + Deep Agents + LangChain

`traced_analyzer` 和主 Agent 一样，不是普通 Python 函数直接跑到底：

```text
LangGraph
  负责 thread、Run、checkpoint、cron 和图执行
        |
        v
Deep Agents
  create_deep_agent() 生成分析器的工具/模型循环图
        |
        v
LangChain
  提供 BaseChatModel、Tool、AgentMiddleware 等接口
```

`get_analyzer()` 的返回类型标注为 `Pregel`，内部调用：

```python
create_deep_agent(
    model=...,
    tools=[save_review_style_prompt, read_finding_outcomes],
    backend=...,
    skills=["/skills/"],
    middleware=[...],
)
```

因此它没有直接使用 LangChain 的 `create_agent()`，而是使用 `create_deep_agent()` 自动生成 LangGraph 的编译图。项目再用 LangGraph Runtime 运行它。

## 两种运行模式

分析器依赖 `configurable["analyzer_mode"]` 选择一本不同的操作手册。

| 模式 | 什么时候用 | 它主要研究什么 | 对应 Skill |
|---|---|---|---|
| `bootstrap` | 某个仓库第一次启用评审风格学习 | 历史已合并 PR 里的真实人类评审评论 | [`bootstrap-repo-analysis/SKILL.md`](../agent/skills/bootstrap-repo-analysis/SKILL.md) |
| `continual` | 已有风格提示词后，手动或每日定时运行 | Open SWE Reviewer 过去的 finding 是否被认可或驳回 | [`continual-learning/SKILL.md`](../agent/skills/continual-learning/SKILL.md) |

### `bootstrap`：第一次建立规则

冷启动时，分析器会：

```text
读取历史已合并 PR
  -> 找出真实人类的 review/comment
  -> 排除 bot 和明显自动化评论
  -> 归纳团队常抓的缺陷类型
  -> 归纳团队通常不想看到的低价值挑刺
  -> 写出第一版 repository-specific review prompt
```

Skill 要求收集足够的人类评审样本，不是只看几条评论就生成看似合理、实际没有证据的规则。

### `continual`：持续纠偏

后续运行时，分析器优先调用 `read_finding_outcomes()`，将历史 finding 分成两类：

| finding 结果 | 表示什么 | 如何影响下一版提示词 |
|---|---|---|
| confirmed | 被后续提交修复，或收到 👍 | 强化这类 bug 的“重点检查”规则。 |
| dismissed | 被驳回，或收到 👎 | 将反复出现的误报加入“不要再报”的规则。 |

它不会因为一条 finding 就大幅修改策略，而是寻找重复模式。目标是同时提高：

- **召回率**：真实问题不要漏掉；
- **准确率**：不要反复报团队不认可的假问题。

## 从 Dashboard 到保存结果的完整链路

首次分析通常由 Dashboard 的评审风格功能触发，关键代码在 [`agent/dashboard/review_style_jobs.py`](../agent/dashboard/review_style_jobs.py)。

```text
用户在 Dashboard 选择“分析某仓库的评审风格”
  |
  v
start_bootstrap_analysis("owner/repo")
  |
  +--> collect_review_samples()
  |     预取部分历史 PR 人工评审样本
  |
  +--> generate_review_style_thread_id(owner, repo)
  |     同一个仓库始终使用同一个 analyzer thread_id
  |
  +--> create_durable_run(
  |       assistant_id="analyzer",
  |       analyzer_mode="bootstrap",
  |       files=build_skill_files(),
  |     )
  |
  v
LangGraph 根据 langgraph.json 找到 traced_analyzer
  |
  v
traced_graph_factory(get_analyzer, "open-swe-review")
  |
  v
get_analyzer(config)
  |
  +--> 取回该 thread 的 sandbox backend
  +--> 建立 /skills/ 虚拟文件路由
  +--> 创建模型和 Deep Agent 图
  |
  v
PrepareAnalyzerRunMiddleware
  |
  +--> 确认 sandbox 和工作目录
  +--> 取得 GitHub App Token 或调用方提供的 Token
  +--> 配置 sandbox GitHub proxy
  +--> 指向 bootstrap/continual 对应 SKILL.md
  |
  v
分析器使用 gh 查询证据，归纳评审风格
  |
  v
save_review_style_prompt(...)
  |
  +--> 保存 custom_prompt 和分析摘要
  +--> 标记分析完成
  +--> 确保该仓库有每日 continual cron
```

## 为什么要有固定的 analyzer thread

每个 `owner/repo` 会生成确定性的 analyzer `thread_id`。这样同一仓库的：

- sandbox 绑定；
- thread metadata；
- 分析 Run；
- 评审风格状态；

都能稳定对应到同一个逻辑对象，而不是每次分析都产生一条完全无关的新会话。

每日 cron 看起来是“threadless”的定时任务，但它仍在 `configurable` 中显式传入这个固定 `thread_id`。否则 `get_analyzer()` 会认为没有执行上下文，返回一个没有工具的空 Agent，定时任务就什么也做不了。

## 它为什么需要 sandbox 和 GitHub proxy

Analyzer 不开发业务代码，但它需要在隔离环境里执行类似命令：

```bash
GH_TOKEN=dummy gh pr list --repo owner/repo --state merged
GH_TOKEN=dummy gh api repos/owner/repo/pulls/123/reviews
GH_TOKEN=dummy gh api repos/owner/repo/pulls/123/comments
```

真实 GitHub Token 不放在沙箱文件系统里。对于 LangSmith sandbox，`PrepareAnalyzerRunMiddleware` 会配置 GitHub proxy：命令仍然写 `GH_TOKEN=dummy`，代理再替它注入真实认证信息。

它借用 sandbox 是为了：

- 安全地运行 GitHub 查询命令；
- 与主 Agent/Reviewer 保持一致的执行环境；
- 避免把真实 Token 直接暴露给模型或落到文件中。

它不会克隆后修改目标项目、提交代码或打开 PR。

## `/skills/` 为什么是虚拟文件

分析器的流程不是全部写死在 Python 提示词里，而是放在两份 `SKILL.md` 中。`build_skill_files()` 将它们读成 LangGraph state 的 `files` 输入，再通过：

```text
CompositeBackend
  /skills/ -> StateBackend
```

暴露给 Deep Agents。

于是分析器能用：

```text
read_file("/skills/bootstrap-repo-analysis/SKILL.md")
```

读取当前模式的操作手册，但 Skill 文件不会被写入执行沙箱。这让“行为流程”与“工作目录中的普通代码文件”隔离开。

## 最终结果如何被 Reviewer 使用

分析器最终调用 [`save_review_style_prompt`](../agent/tools/save_review_style.py)，保存的关键字段是：

```text
custom_prompt
```

Reviewer 在准备一次 PR 评审时，会从 `agent/dashboard/review_styles.py` 取出该仓库的 prompt，再把它作为“repository-specific review style”附加到 Reviewer 的系统提示词中。

```text
Analyzer 保存 custom_prompt
          |
          v
review_styles Store
          |
          v
Reviewer PrepareReviewerRunMiddleware
          |
          v
Reviewer system prompt + 仓库专属评审规范
          |
          v
更贴合团队习惯的 finding
```

这是一条间接链路：Analyzer 不会直接审 PR，它通过影响 Reviewer 的 prompt 来影响之后的评审质量。

## middleware 和工具为什么很少

Analyzer 的工具只显式注册两项：

| 工具 | 作用 |
|---|---|
| `read_finding_outcomes` | continual 模式读取 Reviewer 的确认/驳回历史。 |
| `save_review_style_prompt` | 保存最终提示词、分析摘要和样本统计。 |

文件读写、`execute`、`read_file` 等由 Deep Agents 根据 backend 自动提供。它的 middleware 也比主 Agent 少：

| middleware | 作用 |
|---|---|
| `PrepareAnalyzerRunMiddleware` | 准备 sandbox、Token、工作目录和模式提示词。 |
| `SanitizeToolInputsMiddleware` | 规范工具输入。 |
| `ModelCallLimitMiddleware` | 最多 80 次模型调用，避免历史分析无限扩张。 |
| `ToolErrorMiddleware` | 将工具异常变成模型可以继续处理的结果。 |
| `TimeoutWrapupMiddleware` | 超时时给出可收尾的结果。 |

它没有主 Agent 的 PR 创建保护、Slack 状态、用户消息队列等 middleware，因为它不执行用户开发任务，也不需要与外部对话实时协作。

## 最容易误解的点

1. **`traced_analyzer` 不是一个日志分析工具。** `traced` 只是指运行会写入 LangSmith tracing project。
2. **它不等于 Reviewer。** Analyzer 生成规则，Reviewer 消费规则并审当前 PR。
3. **它不直接使用 LangChain `create_agent()`。** 它使用 `create_deep_agent()` 生成 LangGraph 编译图，但模型、工具、middleware 抽象仍来自 LangChain。
4. **它有 sandbox 不表示它会改业务代码。** sandbox 主要用于安全执行 `gh` 查询历史评审数据。
5. **cron 不是每晚重新学习一切。** bootstrap 才大规模研究历史 PR；continual 主要根据 outcome 做增量校准。
6. **保存的不是模型微调权重。** 它保存的是文本形式的仓库专属提示词，不会训练或微调基础模型。

## 推荐阅读顺序

1. [`agent/analyzer.py`](../agent/analyzer.py)：图工厂、准备 middleware 和工具集合。
2. [`agent/dashboard/review_style_jobs.py`](../agent/dashboard/review_style_jobs.py)：bootstrap 与手动 continual Run 如何创建。
3. [`agent/dashboard/analyzer_cron.py`](../agent/dashboard/analyzer_cron.py)：每日 continual cron 如何注册。
4. [`agent/skills/bootstrap-repo-analysis/SKILL.md`](../agent/skills/bootstrap-repo-analysis/SKILL.md)：第一次分析具体怎么收集证据。
5. [`agent/skills/continual-learning/SKILL.md`](../agent/skills/continual-learning/SKILL.md)：后续如何用 finding outcome 纠偏。
6. [`agent/tools/save_review_style.py`](../agent/tools/save_review_style.py)：结果如何持久化并注册 cron。
7. [`agent/reviewer.py`](../agent/reviewer.py)：最后追踪 `custom_prompt` 如何进入 Reviewer system prompt。

## 验证边界

本文根据当前源码静态确认了入口、图装配、两种模式、工具、cron 和结果消费关系。没有在本次讲解中实际调用 GitHub API、创建远程 sandbox、运行 Analyzer 或注册 cron，因为这些操作需要真实凭据和会产生外部状态。


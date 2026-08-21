# 在 Langfuse Cloud 阅读 Open SWE Trace

本讲义使用 Open SWE 已经上报到 Langfuse Cloud 的真实运行数据学习。它关注“看懂一次运行”，不重复讲解接入配置；接入方法见本目录的 [README.md](README.md)。

## 学习路线

1. Trace、Observation 与 Session：一次 Agent Run 如何映射到 Langfuse。
2. Trace Tree：理解 Agent、Generation、Tool 的父子关系。
3. 排错：从根错误定位到模型调用或工具调用。
4. 性能与成本：查看 token、TTFT、总耗时与失败率。
5. 生产可观测：环境、标签、视图、评分、告警与数据边界。

## 第 1 节：Trace、Observation 与 Session

Langfuse 用三层数据记录 Open SWE 的运行。`Session` 将同一个对话线程的多轮 Run 串起来；`Trace` 代表其中一次用户消息触发的 Agent Run；`Observation` 是该 Run 内的具体执行步骤。

```text
Session（同一个 thread_id）
  ├─ Trace（一次 Run）
  │   └─ AGENT（根 Observation）
  │       ├─ Generation（一次模型调用）
  │       └─ Tool（一次工具调用）
  └─ Trace（下一次 Run）
```

在本项目中，`thread_id` 会作为 Langfuse 的 `Session ID`。因此连续对 Agent 发送多条消息时，每条消息各自形成一个 Trace，但可以通过相同的 Session 回看完整对话链路。

### 在 Cloud 中怎么读

1. 打开项目的 **Tracing** 页面，按最近时间打开一条 Trace。
2. 先查看根 `AGENT` 节点的 `Level`、`Status Message`、`Duration` 与 `Session ID`。
3. 再展开 Trace Tree：`Generation` 表示模型请求已发出，`Tool` 表示 Agent 已执行工具。
4. 点击 `Generation`，查看完整 messages、模型名、模型输出、输入/输出 token 和耗时。
5. 点击 `Tool`，查看工具参数、工具结果和异常。

### 用一次真实失败理解层级

模型名配置错误时，Cloud 中会出现根 `AGENT` 为 `ERROR` 的 Trace，`Status Message` 会包含网关返回的错误。若树中没有 `Generation` 或 `Tool`，说明错误发生在有效模型调用之前：此时没有 token、模型响应或工具参数是正常的，并非 Langfuse 丢失了数据。

这也是排查的固定起点：先看根节点的状态和错误，再根据是否存在 `Generation`、`Tool` 缩小问题范围，不要一上来就查 token。

### 常见误区

一条 Trace 不是整个多轮聊天记录，而是一次 Run。查看同一个 Agent 的连续对话，应点击或筛选相同的 `Session ID`，而不是只停留在某一条 Trace。

## 第 2 节：读懂 Trace Tree

Trace Tree 是一次 Run 的执行树，而非简单日志列表。父节点表示“谁触发了后续工作”，子节点表示“该步骤实际做了什么”；节点的先后顺序则表示执行时间。

```text
AGENT open-swe-agent
  ├─ GENERATION DeepSeek-V4-Flash
  │   └─ 输出：调用 read_file
  ├─ TOOL read_file
  │   └─ 输入：{ "path": "agent/utils/tracing.py" }
  ├─ GENERATION DeepSeek-V4-Flash
  │   └─ 输出：解释代码并继续调用工具
  └─ TOOL slack_thread_reply
      └─ 输出：已向用户回复
```

这棵树表达了 Agent 的循环：模型先根据已有消息决定下一步，随后工具执行；工具结果回到下一次模型调用，直到模型输出最终答复。一次复杂任务通常有多个 `Generation` 和 `Tool`，这不是重复上报，而是 Agent 多轮思考与执行的真实过程。

### 在 Cloud 中怎么读

1. 打开一条正常完成的 Trace，先从根 `AGENT` 节点展开所有子节点。
2. 按时间从上到下读：每个 `Generation` 后，检查模型输出是否包含 `tool_calls`；随后应出现对应的 `Tool` 节点。
3. 点击该 `Tool`，核对输入参数和返回结果，再回到紧随其后的 `Generation`，确认模型是否正确使用了工具结果。
4. 最后查看最末尾的 `Generation`：它通常包含给用户的最终回复，且不再请求工具。

### 两个实用判断

| Tree 现象 | 表示什么 | 优先检查 |
| --- | --- | --- |
| `Generation` 后没有对应 Tool | 模型没有请求工具，或在工具执行前出错 | 模型输出、停止原因、错误字段 |
| Tool 后没有下一次 `Generation` | 工具异常、Run 被取消、达到步数/递归限制，或工具结果未回到模型 | Tool 输出与错误、根 Agent 状态、Runtime 日志 |

### 常见误区

不要只看最慢或最末尾的节点。必须将一次 `Generation`、它请求的 Tool、以及紧随其后的下一次 `Generation` 作为一个循环阅读，才能判断问题是模型决策错误、工具执行错误，还是模型没有正确理解工具结果。

## 第 3 节：排查一次失败 Run

Langfuse 记录的是执行证据，不会自动判断根因。排查时应始终从根 `AGENT` 开始，再沿失败分支向下找第一个 `ERROR` 节点；这能避免把后续连带错误误当成根因。

```text
根 AGENT
  ├─ ERROR，且没有 Generation / Tool
  │   └─ 初始化、鉴权、模型路由或运行时配置问题
  ├─ Generation ERROR
  │   └─ 模型请求、Prompt、响应格式、限流或超时问题
  └─ Tool ERROR
      └─ 工具参数、GitHub 鉴权、沙箱、网络或工具实现问题
```

### 固定排查顺序

1. 打开失败 Trace 的根 `AGENT`，读取 `Level`、`Status Message`、开始时间和总耗时。
2. 如果没有 `Generation` 和 `Tool`，错误发生在模型有效调用前。优先检查 Runtime 启动配置、模型 ID、模型网关和鉴权。
3. 如果存在失败的 `Generation`，打开它检查完整 input、模型名、响应、`finish_reason`、token 和延迟。再对照服务端 Runtime 日志中的同一时间点。
4. 如果存在失败的 `Tool`，检查 Tool 的输入参数、输出和错误文本；再查看这个工具依赖的外部系统，例如 GitHub OAuth、Sandbox 或网络。
5. 修复后发送一条新的消息生成新的 Trace。不要把旧失败 Trace 当作会自动恢复的任务。

### 本项目的真实案例：模型路由失败

此前模型 ID 与 DeepSeek 网关要求不一致，根 `AGENT` 的状态为 `ERROR`，错误文本为“`No model keys configured for model: deepseek-v4-flash`”，且 Tree 中没有 `Generation` 或 `Tool`。

这说明请求在真正向模型获取有效响应之前就被网关拒绝。根因不是 Prompt、token、工具或 Langfuse；修正为网关接受的 `DeepSeek-V4-Flash` 后，新 Run 才能生成模型与工具节点。

### 常见信号与含义

| 现象 | 常见原因 | 下一步 |
| --- | --- | --- |
| 根 Agent 很快失败，且没有子节点 | 配置、鉴权、模型路由、初始化失败 | 看 `Status Message` 和 Runtime 日志 |
| Generation 的耗时接近超时阈值 | 网关慢、网络不通或模型拥塞 | 看超时配置、网关日志、重试情况 |
| Generation 有输出但 Agent 未继续 | 响应格式异常、递归/调用次数限制、取消 Run | 看根状态和最后一个 Generation 的输出 |
| Tool 为 ERROR | 参数错误、外部依赖失败或工具自身异常 | 看 Tool input/output，再看外部服务日志 |

### 常见误区

不要只在 Langfuse 页面修问题。Langfuse 负责告诉你“哪一步、带着什么输入、为什么失败”；项目 Runtime 日志、网关日志和外部服务日志负责提供该步骤的底层异常与运行环境信息。两者按同一时间点和同一 Run 对照使用。

## 第 4 节：看性能与成本

性能和成本应优先在 `Generation` 节点查看，因为它代表一次真实模型请求。根 `AGENT` 的总耗时包含模型调用、工具执行以及多轮 Agent 循环，不能直接归因给模型。

| 指标 | 在哪里看 | 它回答的问题 |
| --- | --- | --- |
| Trace / Agent Duration | 根 `AGENT` | 整个 Run 花了多久 |
| Generation Duration | `Generation` 节点 | 单次模型请求或网关花了多久 |
| TTFT（首 Token 时间） | 流式 `Generation` 的延迟指标 | 用户等待第一个可见输出多久 |
| Input / Output Tokens | `Generation` 的 Usage | Prompt 和回答各消耗多少 token |
| Cost | `Generation` 的 Cost | 按 Langfuse 定价规则估算的模型费用 |

### 在 Cloud 中怎么读

1. 打开一条正常完成的 Trace，先比较根 `AGENT` 的 Duration 与各子节点 Duration。
2. 点击每个 `Generation`，记录模型名、输入/输出 token 和 Duration；按时间顺序判断是单次慢，还是 Agent 循环次数过多。
3. 点击耗时最长的 `Tool`。若它明显比 Generation 慢，瓶颈在 GitHub、Sandbox、网络或工具逻辑，而非模型。
4. 在 Tracing 页面按 `name`、`tag`、模型或时间范围筛选，再比较多条同类 Trace，而不是根据一条偶然慢请求下结论。

### 如何解释常见现象

```text
根 Agent 很慢，某个 Generation 也很慢
  -> 模型或模型网关延迟高

根 Agent 很慢，某个 Tool 很慢
  -> 外部服务、Sandbox 或工具执行慢

每个节点都不慢，但 Trace 很长且 Generation 很多
  -> Agent 多轮循环；检查 Prompt、工具结果和停止条件

Input Token 持续增长
  -> 对话历史、系统提示词或工具结果过长
```

### Token、TTFT 与 Cost 的边界

`TTFT` 只有流式模型调用正确记录首个输出时间时才有意义；非流式请求可能为空。Token 也依赖模型网关返回 usage 数据，网关没有返回时 Langfuse 无法凭空计算。

Cost 还额外依赖 Langfuse 能识别模型名并拥有对应价格。当前 `DeepSeek-V4-Flash` 经自定义网关转发，若 Cloud 没有该模型的价格定义，即使能看到 token，Cost 也可能为空或为零；这不表示免费。此时先以网关账单为准，之后再在 Langfuse 的模型价格配置中增加与实际网关计费一致的自定义价格。

### 本项目 Token 为 0 的实际原因

最近的 `Generation` 已经能看到模型名 `DeepSeek-V4-Flash`、Duration 和 TTFT，但 `usageDetails` 为空、输入/输出 Token 为 0。这说明 Langfuse callback 正常工作，缺的是模型响应中的 usage 数据，不只是 Cloud 价格表缺失。

项目的 DeepSeek 网关走 OpenAI Chat Completions 流式接口。该接口只有在请求中带 `stream_options: {"include_usage": true}` 时，才会在流末返回 usage；现在 `agent/utils/model.py` 对自定义 DeepSeek endpoint 自动设置了 `stream_usage=True`。重启 Runtime 后新产生的 Generation 才会带 usage，历史 Trace 不会自动补算。

验证顺序：

1. 重启 `langgraph dev`，发送一条新的最小任务。
2. 在新 Trace 的 `Generation` 中确认 `Input Usage`、`Output Usage`、`Total Usage` 不再是 0。
3. 若 Token 有值但 USD 仍为 0，在 Langfuse Cloud 的 **Models / Model Costs** 中新增或编辑模型，模型名必须精确匹配 `DeepSeek-V4-Flash`，并按网关实际账单填写输入、输出价格。
4. 若开启 `stream_usage` 后模型请求直接返回 400，说明该网关不支持 OpenAI 的 usage 流字段；此时保留 Token 为空的事实，改由网关返回 usage 或增加适配层，不能在 Langfuse 页面凭空推算真实 token。

### 在 Cloud 配置 DeepSeek 网关费用

费用配置位于 Langfuse Cloud 项目的 **Settings -> Models**（部分页面入口显示为 **Models / Model Costs**），不在 Open SWE 的 `.env` 或 Python 代码中。Langfuse 根据 Generation 上报的模型名匹配模型定义，再用 Token 和价格计算 USD。

为当前网关新增一个自定义模型时填写：

| 字段 | 当前值 |
| --- | --- |
| Model name | `DeepSeek-V4-Flash` |
| Match pattern | `^DeepSeek-V4-Flash$` |
| Unit | `TOKENS` |
| Input price | 网关账单的输入单价，单位为 USD / token |
| Output price | 网关账单的输出单价，单位为 USD / token |

网关若按“USD / 1M tokens”报价，不能原样填入。Langfuse 需要每一个 token 的美元价格：例如输入 `$0.20 / 1M tokens` 应填写 `0.0000002`，输出 `$0.80 / 1M tokens` 应填写 `0.0000008`。

自定义网关的实际计费策略可能与 DeepSeek 官方价格不同，因此应以网关提供的价目表或账单为准。配置完成后，等待新产生的 Generation 匹配价格；旧数据是否重算取决于 Cloud 的模型价格生效时间，不要以此替代网关账单核对。

### 常见误区

不要为降低单次 token 而盲目截断工具结果或系统提示词。应先确认 token 增长来自哪里：必要的仓库上下文属于有效成本，重复读文件、无效循环和过长的工具输出才是应优化的浪费。

## 第 5 节：用于生产的可观测性

生产环境的目标不是“存下每条日志”，而是在故障、成本异常或质量下降时，能迅速筛到对应的环境、版本、用户和步骤。先建立稳定的筛选维度，再谈告警和自动评价。

### 当前项目已写入的维度

| 维度 | 当前值来源 | 用途 |
| --- | --- | --- |
| Trace Name | `open-swe-agent` 或 `open-swe-review` | 区分主 Agent 和 Reviewer |
| Session ID | `thread_id` | 回放同一对话的多轮 Run |
| User ID | `github_login` | 定位某个用户报告的问题 |
| Tags | 图名称与消息来源 `source` | 区分 Dashboard、Slack、GitHub 等入口 |

在 Cloud 的 Tracing 页面，先围绕这些字段建立三个常用视图：最近失败的主 Agent、某一入口的 Trace、某个 Session 的所有 Run。排查时从视图进入，而不是在全部 Trace 中手工翻找。

### 环境与发布版本

Langfuse SDK 支持为 Trace 指定 `environment` 和 `release`，它们用于区分 `development`、`staging`、`production` 与具体发布版本。当前 `agent/utils/langfuse.py` 只创建了 tags、Session 和 User，没有显式设置这两个字段；若开发和生产共用同一个 Langfuse 项目，页面数据会混在一起。

生产建议优先使用独立 Langfuse 项目和独立密钥，将生产数据与个人开发测试数据隔离。需要在同一项目中比较多个环境时，再为 Runtime 增加稳定的 `environment` 与 `release` 配置，并以它们作为筛选条件；这属于后续接入改造，不是当前 callback 自动完成的能力。

### Scores、告警与抽样

`CallbackHandler` 自动采集的是执行事实：输入、输出、token、耗时、错误和节点层级。它不会自动判断“回复是否正确”或“代码是否可用”。

| 能力 | 解决的问题 | 当前状态 |
| --- | --- | --- |
| Score | 一次回复或整个 Trace 的质量如何 | 需要后续人工反馈或评测程序写入 |
| 告警 | 错误率、延迟或成本异常时通知 | 先定义阈值，再接入 Cloud 可用的告警能力或外部监控 |
| 抽样 | 高流量下控制采集量和成本 | 当前未配置，现有启用的 Run 会完整采集 |

先从可操作的阈值开始：错误 Trace 增加、Generation 延迟高于平时、单个 Session 出现异常多轮循环、或单次 token 明显高于同类请求。没有稳定基线时不要急着设置告警，否则只会制造噪声。

### 数据边界

本项目会上传完整 Prompt、模型响应、工具参数和工具结果，并在导出前尝试脱敏常见密钥。它不是通用 DLP：业务文本、源码或用户输入中的敏感内容仍可能被上传。

生产启用前要明确四件事：哪些内容允许出网、谁可以访问 Cloud 项目、数据保存多久、发生敏感数据误传时如何删除和审计。调试便利性不能替代这些边界。

### 一个最小生产排查流程

```text
收到用户故障报告
  -> 按用户 / Session / 时间找到 Trace
  -> 看根 Agent 是否 ERROR
  -> 定位首个失败的 Generation 或 Tool
  -> 对照同一时间点的 Runtime 与外部服务日志
  -> 修复后用新的 Trace 验证，并记录结果
```

### 常见误区

不要把 Langfuse 当作全部生产监控的替代品。它最擅长回答 Agent 为什么这样决策、调用了什么模型和工具、哪里失败；主机资源、数据库、HTTP 网关和基础设施健康度仍应由常规日志、指标和告警系统负责。

## 综合案例：一条用户任务为什么失败

用户报告 Dashboard 中的 Agent 任务失败。用其 GitHub login、发生时间或 `thread_id` 在 Cloud 中找到对应 Trace 后，按以下路径判断：

```text
Session
  -> 找到本次失败的 Trace
  -> 根 AGENT：确认 ERROR 和 Status Message
  -> Trace Tree：找到首个 ERROR 节点
      -> 没有 Generation：检查模型路由、鉴权、Runtime 配置
      -> Generation ERROR：检查 Prompt、模型响应、token、超时与网关日志
      -> Tool ERROR：检查参数、工具结果、Sandbox / GitHub / 网络日志
  -> 修复并重新发送消息
  -> 新 Trace：确认完成状态、工具链、token 与耗时恢复正常
```

如果任务虽成功但用户觉得慢，则在同类 Trace 中比较 Generation 和 Tool 的 Duration：最长节点决定优先优化对象；若节点数量异常多，回到每次 `Generation -> Tool -> Generation` 循环，检查模型是否重复调用工具或读取了过长上下文。

这套流程分别用到了：Session 定位会话、Trace 定位一次 Run、Tree 理解执行顺序、Observation 定位具体失败步骤、Generation 分析模型性能、Tool 分析副作用与外部依赖、tags 和环境筛选缩小生产范围。

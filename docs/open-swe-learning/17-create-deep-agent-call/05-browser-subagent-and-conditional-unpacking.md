# 05：浏览器子 Agent 与条件列表解包

本篇只精读 `get_agent()` 中这一项：

```python
*([_browser_subagent(subagent_model, browser_tools)] if browser_tools else [])
```

它的目标是：**只有当前部署具备可用的 Stagehand 浏览器配置时，才把名为 `browser` 的专用子 Agent 注册进主 Agent 的 `task` 工具。**

调用位置在 [agent/server.py:1186](../../../agent/server.py:1186)，浏览器子 Agent 的定义在 [agent/server.py:661](../../../agent/server.py:661)，工具加载在 [agent/integrations/stagehand_browser.py:863](../../../agent/integrations/stagehand_browser.py:863)。

## 1. 先还原成容易读的 Python

原代码位于一个列表字面量中：

```python
subagents=[
    _general_purpose_subagent(subagent_model, skill_sources, dynamic_tool_middleware),
    *([_browser_subagent(subagent_model, browser_tools)] if browser_tools else []),
]
```

分成三层看。

### 1.1 条件表达式

```python
[_browser_subagent(subagent_model, browser_tools)] if browser_tools else []
```

这是 Python 的条件表达式：

```python
结果 = 真值结果 if 条件 else 假值结果
```

当 `browser_tools` 是非空列表时，创建一个只含 `browser` spec 的列表；为空列表时，产生空列表：

```python
browser_tools = [browser_navigate, browser_act]
# 得到 [_browser_subagent(subagent_model, browser_tools)]

browser_tools = []
# 得到 []
```

这里利用了 Python 容器的真值规则：非空列表为真，空列表为假。

### 1.2 `*` 是列表解包，不是工具调用

```python
*[...]
```

在列表内部的 `*iterable` 会将可迭代对象的元素逐个放进外层列表。

```python
["general-purpose", *["browser"]]  # ["general-purpose", "browser"]
["general-purpose", *[]]           # ["general-purpose"]
```

所以原代码等价于更啰嗦但更直观的写法：

```python
subagents = [
    _general_purpose_subagent(subagent_model, skill_sources, dynamic_tool_middleware),
]
if browser_tools:
    subagents.append(_browser_subagent(subagent_model, browser_tools))
```

使用条件列表加解包的价值只是让固定子 Agent 和可选子 Agent 在一次 `create_deep_agent(...)` 声明中构造完成。它不会执行浏览器，也不会向 `subagents` 放入 `None`、`[]` 或一个“无工具的 browser Agent”。

## 2. `browser_tools` 从哪里来，何时为空

在同一个 `get_agent()` 调用内，代码先执行：

```python
browser_tools = load_browser_tools()
```

见 [agent/server.py:1102](../../../agent/server.py:1102)。`load_browser_tools()` 的实现很小：

```python
def load_browser_tools() -> list[Any]:
    if not browser_tools_enabled():
        return []
    return [
        browser_navigate,
        browser_act,
        browser_observe,
        browser_extract,
        browser_close,
    ]
```

见 [stagehand_browser.py:863](../../../agent/integrations/stagehand_browser.py:863)。因此是否注册 `browser` 不是模型自己判断的，而是服务启动环境的能力检测结果。

| Stagehand 环境 | `browser_tools_enabled()` 的条件 | `browser_tools` | `subagents` 结果 |
| --- | --- | --- | --- |
| `LOCAL`，默认值 | 存在 `STAGEHAND_MODEL_API_KEY`，或回退到 `MODEL_API_KEY` / `ANTHROPIC_API_KEY` | 五个浏览器工具 | `general-purpose` + `browser` |
| `BROWSERBASE` | 存在 `BROWSERBASE_API_KEY` | 五个浏览器工具 | `general-purpose` + `browser` |
| 未满足上述条件 | 不可用 | `[]` | 只有 `general-purpose` |

对应实现见 [stagehand_browser.py:58](../../../agent/integrations/stagehand_browser.py:58) 和 [stagehand_browser.py:82](../../../agent/integrations/stagehand_browser.py:82)。`LOCAL` 还需要可运行的 Chrome/Chromium；可用 `STAGEHAND_LOCAL_CHROME_PATH` 指定路径。`BROWSERBASE` 模式还可选传入 `BROWSERBASE_PROJECT_ID`。

这是 feature detection：没有配置就不暴露能力，主模型也不会看见可委派的 `browser`。这比注册一个必然失败的 Agent 干净，免得模型拿着一个空壳工具乱撞，艹，这种无效能力暴露只会制造失败循环。

## 3. `_browser_subagent()` 逐项解读

函数位于 [agent/server.py:661](../../../agent/server.py:661)：

```python
def _browser_subagent(model: BaseChatModel, tools: list[Any]) -> SubAgent:
    return {
        "name": "browser",
        "description": BROWSER_SUBAGENT_DESCRIPTION,
        "system_prompt": BROWSER_SUBAGENT_SYSTEM_PROMPT,
        "tools": tools,
        "model": model,
        "middleware": _subagent_model_timeout_middleware(),
    }
```

| 字段 | 当前值 | 被谁使用 | 含义 |
| --- | --- | --- | --- |
| `name` | `"browser"` | 主图的 `task` 工具 | 委派时使用的子 Agent 名称。 |
| `description` | `BROWSER_SUBAGENT_DESCRIPTION` | 主模型 | 告诉主模型它擅长真实网页交互、JS 渲染内容、填表、UI bug 复现和结构化抽取；静态页面优先 `fetch_url`。 |
| `system_prompt` | `BROWSER_SUBAGENT_SYSTEM_PROMPT` | browser 子图模型 | 规定浏览器工作流和安全要求。 |
| `tools` | `browser_tools` | browser 子图 | 明确声明专属浏览器工具集。 |
| `model` | `subagent_model` | browser 子图模型 | 使用 `get_agent()` 已解析出的子 Agent 模型，而非强制使用主模型。 |
| `middleware` | 独立 timeout | browser 子图 | 防止子图内的模型请求无限挂起。 |

### 3.1 `tools` 的显式声明为何关键

Deep Agents 对 raw `SubAgent` 的规则是：

```python
raw_subagent_tools = spec.get("tools") if "tools" in spec else tools
```

见 [deepagents/graph.py:726](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:726)。

`browser` spec 写了 `"tools": tools`，所以它**不继承**父 Agent 的 `static_tools`。也就是说，`browser` 不会因为主 Agent 有 `open_pull_request`、`slack_thread_reply`、Linear 工具而自动拥有它们；它的业务工具面就是上述五个浏览器工具。

Deep Agents 仍会为每个 raw 子 Agent 安装自己的 `FilesystemMiddleware`、摘要 middleware 和 `PatchToolCallsMiddleware`，并让它们共用 `backend`。见 [deepagents/graph.py:666](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:666)。这不改变本节结论：浏览器子 Agent 的**显式业务工具**是 Stagehand 工具，不是主 Agent 工具全集。

### 3.2 和 `general-purpose` 的边界

| 维度 | `general-purpose` | `browser` |
| --- | --- | --- |
| 名称 | `general-purpose` | `browser` |
| 主要任务 | 代码、搜索、研究、多步骤实现 | 真实网页交互和 JS 渲染内容 |
| 显式 `tools` | 没有，继承主 Agent `static_tools` | 有，只使用 `browser_tools` |
| Skills | `skill_sources` 非空时显式接入 | 当前 spec 未写 `skills` |
| 动态集成工具 | 传入 `dynamic_tool_middleware` 时接入 | 当前 spec 未接入 |
| Open SWE shared prompt | 显式拼接 | 使用浏览器专用 prompt |
| 模型 | `subagent_model` | `subagent_model` |
| 模型超时 | 有 | 有 |

因此 `browser` 不是给通用子 Agent 再加五个工具，而是一条工具、提示词、职责都被收窄的专用执行路径。主模型可以把“写代码并开 PR”和“登录一个后台验证页面”分别交给正确对象。

## 4. 编译后如何被主 Agent 选择

`create_deep_agent(...)` 接到 `subagents` 后，会把每个 spec 编译为独立子图，并在主图安装 `SubAgentMiddleware`。只要存在子 Agent，middleware 就会向主图加入 `task` 工具，且把可选 Agent 的名称和 description 放入其描述中。见 [deepagents/graph.py:827](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:827)。

运行时的关系如下：

```text
主 Agent 模型
  └─ 调用 task(subagent_type="browser", description="...")
       └─ browser 子图拿到独立的子任务消息
            ├─ browser_navigate
            ├─ browser_observe / browser_act / browser_extract
            ├─ browser_close
            └─ 最终摘要返回给主 Agent
```

是否调用 `task` 是模型根据 `browser` 的 description、用户任务和主系统 prompt 作出的选择；框架不会因“注册了 browser”就自动启动 Chromium。`task` 返回后，父 Agent 只拿到子 Agent 的最终结果，不能把子 Agent 的完整中间消息当作共享对话历史。这个独立子图边界已在 [04：默认子智能体继承与改造](04-default-subagent-inheritance-and-customization.md) 说明。

## 5. 五个工具与会话生命周期

`browser_tools` 的真实来源是 Stagehand Python SDK，不是 MCP 子进程；源码注释明确说明它直接调用 `AsyncStagehand`。见 [stagehand_browser.py:1](../../../agent/integrations/stagehand_browser.py:1)。

建议的正常流程为：

```text
browser_navigate(url)
  -> browser_observe("找到登录表单")      # 陌生页面先观察
  -> browser_act("在邮箱框输入 ...")      # 一次只做一个动作
  -> browser_extract("提取结果", schema) # 需要时指定 JSON Schema
  -> browser_close()
```

| 工具 | 行为 | 关键前置条件 |
| --- | --- | --- |
| `browser_navigate(url)` | 首次使用时创建会话，导航至绝对 URL | URL 必须通过安全检查。 |
| `browser_observe(instruction)` | 返回可操作元素 | 已执行 `navigate`。 |
| `browser_act(action)` | 执行一次自然语言描述的点击、输入或导航 | 已执行 `navigate`；每次只做一个动作并验证。 |
| `browser_extract(instruction, schema=None)` | 提取结构化结果 | 已执行 `navigate`；`schema` 可约束 JSON 输出。 |
| `browser_close()` | 结束会话、关闭客户端、释放资源 | 无；未打开会话时也安全。 |

### 5.1 会话按 `thread_id` 隔离，但不是 LangGraph checkpoint

Stagehand 模块通过 `langgraph.config.get_config()` 读取当前运行的：

```python
config["configurable"]["thread_id"]
```

然后以这个值作为 `_SESSIONS` 内存字典的 key。见 [stagehand_browser.py:49](../../../agent/integrations/stagehand_browser.py:49) 和 [stagehand_browser.py:94](../../../agent/integrations/stagehand_browser.py:94)。同一个线程内 `navigate -> act -> extract` 复用同一个实时页面；不同线程不会取到同一条内存会话。

但这不是 LangGraph Store、thread metadata 或 checkpoint：`_SESSIONS` 是当前 Python 进程内存。进程重启、部署实例切换或调用 `browser_close()` 后，会话即不在，下一次 `browser_navigate()` 会新建浏览器。不要把它误当成可恢复的对话持久化。

### 5.2 `browser_close()` 为什么必须写进 prompt

`browser_close()` 会从 `_SESSIONS` 中弹出当前 thread 的 `(client, session)`，随后关闭 URL guard、调用 `session.end()`，再关闭 Stagehand client。见 [stagehand_browser.py:804](../../../agent/integrations/stagehand_browser.py:804)。

项目把“结束时总是调用 `browser_close`”写进浏览器子 Agent prompt，是因为浏览器会话是有资源成本且可能含登录态的真实外部资源。这个约束是 prompt 约定，不是 `task` 框架自动替你清理；模型中途失败时仍需由运行治理和环境侧资源限制兜底。

## 6. 安全边界与选型

真实浏览器会加载页面的后续资源、跳转和脚本，所以仅在第一次传入 URL 时做检查不够。实现同时做了：

1. `browser_navigate()` 导航前调用 `is_url_safe(url)`；
2. 每次浏览器操作前检查上一次的 guard 失败和被拦截请求；
3. 导航或操作后再次验证当前页面 URL；
4. 检测到危险 URL 或请求后关闭会话并返回失败结果。

对应代码在 [stagehand_browser.py:633](../../../agent/integrations/stagehand_browser.py:633) 与 [stagehand_browser.py:694](../../../agent/integrations/stagehand_browser.py:694)。浏览器 prompt 还要求不外泄凭据和 secret，只执行被委派任务。这些规则降低风险，但不等于“浏览器自动安全”：点击、填表、登录和页面副作用仍应由上层授权、工具输入和运行环境共同约束。

选择工具时，当前项目给出的边界很明确：

```text
只读静态 HTML / API 数据 -> fetch_url 或 http_request
需要点击、填表、登录、客户端渲染、复现 UI -> task 委派 browser
```

这也解释了为什么不把浏览器工具直接塞给主 Agent：主 Agent 的工具 schema 更小，浏览器任务有专用流程 prompt，工具权限更收敛，而且在环境未配置时能力可以完全消失。

## 7. 常见误解

| 误解 | 实际行为 |
| --- | --- |
| `*` 会调用浏览器 | `*` 只在 Python 构造列表时解包元素。真正执行要等模型调用 `task`，子图再调用浏览器工具。 |
| 工具为空也会创建 `browser` | 不会。条件表达式返回 `[]`，外层解包后没有第二个子 Agent。 |
| `browser` 自动继承主 Agent 全部工具 | 不会。它显式写了 `tools`，因此不继承 `static_tools`。 |
| `browser` 自动拥有用户 Skills 和动态集成工具 | 当前 spec 未写 `skills`，也未传 `dynamic_tool_middleware`；不能按通用子 Agent 推断。 |
| 一个 `thread_id` 等于一个永久浏览器 | 不等于。它只是当前进程内 `_SESSIONS` 的 key，不是持久化 checkpoint。 |
| 注册 browser 后每个 Run 都会开浏览器 | 不会。只有主模型实际选择 `task(subagent_type="browser", ...)` 并且子图调用 `browser_navigate` 时才会创建会话。 |

## 8. 最小心智模型

```text
get_agent(config)
  -> load_browser_tools()
       -> 无配置：[] -> 不注册 browser
       -> 有配置：五个 Stagehand 工具
  -> create_deep_agent(subagents=[general-purpose, 可选 browser])
  -> 编译为主图 + 独立 browser 子图

后续某个 Run：
  主模型判断需要真实网页交互
    -> task("browser", 子任务)
    -> browser 子图操作 Stagehand 会话（按 thread_id）
    -> close 后返回摘要
```

验证提示：本篇根据当前源码和 [`tests/middleware/test_model_call_timeout.py`](../../../tests/middleware/test_model_call_timeout.py) 的子 Agent 超时断言编写；没有实际启动 Stagehand、Chromium 或 Browserbase，也没有触发真实网页操作。

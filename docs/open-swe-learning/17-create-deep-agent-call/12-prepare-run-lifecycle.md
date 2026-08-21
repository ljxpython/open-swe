# 12：`PrepareAgentRunMiddleware`：一次 Run 如何准备并可恢复

这是 `09-middleware-learning-roadmap.md` 的第 2 章。本章只回答一个问题：Agent 开始调用模型前，Open SWE 如何准备 sandbox、凭据、工作目录、提示词和本轮 diff 上下文，并且在 LangGraph 恢复时避免把已经完成的准备重复做一遍。

源码入口：

- 基类：[agent/middleware/prepare_run.py:18-97](../../../agent/middleware/prepare_run.py:18)
- Open SWE 实现：[agent/server.py:799-950](../../../agent/server.py:799)
- 行为测试：[tests/middleware/test_prepare_run_middleware.py:17-94](../../../tests/middleware/test_prepare_run_middleware.py:17)

## 1. 先看完整生命周期

```text
get_agent(config)
  -> 创建 PrepareAgentRunMiddleware（只装配，不调用 sandbox）

Run 开始
  -> abefore_agent(state, runtime)
       -> 计算 fingerprint
       -> 已准备且 fingerprint 相同：跳过
       -> 否则执行 _prepare()
            -> token、sandbox、work_dir、业务 prompt、turn checkpoint
       -> 返回 state update
       -> LangGraph checkpoint 保存 state
  -> before_model / awrap_model_call
       -> 把 rendered_system_prompt 放进最终 SystemMessage
  -> 模型和工具循环
```

`create_deep_agent(...)` 或 `get_agent(...)` 本身只负责构图和装配 middleware；sandbox 的创建/恢复发生在后续 Run 进入 `abefore_agent` 时。

## 2. `PrepareRunState`：准备结果放在哪里

```python
class PrepareRunState(AgentState):
    run_prepared: NotRequired[bool]
    run_prepared_for: NotRequired[str]
    work_dir: NotRequired[str | None]
    rendered_system_prompt: NotRequired[str | None]
```

四个字段职责不同：

| 字段 | 含义 | 是否是业务数据 |
| --- | --- | --- |
| `run_prepared` | 是否完成了这次准备 | 控制位 |
| `run_prepared_for` | 准备对应哪个 fingerprint | 幂等闩锁 |
| `work_dir` | sandbox 中模型/工具使用的目录 | 是 |
| `rendered_system_prompt` | 按当前用户、仓库和模式渲染出的最终 prompt | 是 |

这些字段属于图的 state，会随着 LangGraph checkpoint 一起保存。它们不是 `thread.metadata` 的替代品：thread metadata 用于跨请求查询和展示；state 字段用于图恢复时继续执行。

## 3. fingerprint：为什么只看“最新消息”还要看配置

### 3.1 `_latest_message_fingerprint`

`_latest_message_fingerprint(state)` 取 `state["messages"][-1]`，将以下内容序列化后做 SHA-256：

```python
{
    "type": latest.__class__.__name__,
    "id": latest.id,
    "content": latest.content,
}
```

它不是给消息做去重数据库键，而是判断“本轮输入是否变了”。只看最新消息是有意的：历史消息仍然在 state 中，但新的一条 HumanMessage 就足以标识新一轮准备。

### 3.2 `_prepare_fingerprint`

基类把三部分组合起来：

```python
{
    "middleware": self.__class__.__name__,
    "message": _latest_message_fingerprint(state),
    "config": self._prepare_config_fingerprint(),
}
```

然后对 JSON 做 SHA-256。`BasePrepareRunMiddleware` 的配置指纹默认是 `None`；Open SWE 的 `PrepareAgentRunMiddleware` 覆盖它，加入：

```text
prepare_run_id、thread_id、source、repo、plan_mode、draft_prs、model、effort
```

所以以下任一变化都会让准备重新执行：

- 同一个 thread 收到新的用户消息；
- `prepare_run_id` 变化；
- 仓库、模型、effort 或 plan/draft 模式变化；
- middleware 实现类变化。

## 4. `abefore_agent` 逐行理解

```python
prepared_state = cast(PrepareRunState, state)
fingerprint = self._prepare_fingerprint(prepared_state, runtime)
if (
    prepared_state.get("run_prepared")
    and prepared_state.get("run_prepared_for") == fingerprint
):
    return None
updates = await self._prepare(prepared_state, runtime)
return {"run_prepared": True, "run_prepared_for": fingerprint, **updates}
```

执行逻辑是：

1. `cast` 只改变类型检查，不会在运行时复制 state。
2. 计算当前输入和配置的指纹。
3. 只有“已准备”且“准备对象仍是当前 fingerprint”时才返回 `None`。
4. 否则调用 `_prepare`。
5. 将闩锁和 `_prepare` 的结果合并为 state update。

`return None` 很关键：它表示这次 before-agent 不修改 state，而不是把 state 清空。

## 5. checkpoint 恢复为什么不会重复准备

LangGraph 在 `abefore_agent` 节点成功返回后会保存更新后的 state。恢复同一个未完成 Run 时，节点再次看到：

```text
run_prepared=True
run_prepared_for=上次相同的 fingerprint
```

于是直接跳过 `_prepare`。这避免了恢复时重复刷新 token、重复构造 prompt、重复创建 turn checkpoint。

但这里有一个边界：如果 `_prepare` 已经完成副作用，进程却在 checkpoint 保存前崩溃，下一次恢复可能再次进入 `_prepare`。因此基类 docstring 要求子类的 `_prepare` **幂等**：

- `ensure_sandbox_for_thread` 应该复用或重连已有 sandbox，而不是盲目创建；
- 更新 metadata 要允许重复写入；
- Git checkpoint 合并要能覆盖同一个 turn key；
- token 刷新和 prompt 重新读取必须可重复。

这不是“绝对只执行一次”，准确说是“成功 checkpoint 后，同一 fingerprint 不再执行；checkpoint 前失败时允许重试”。

## 6. Open SWE 的 `_prepare()` 做了什么

对应 [agent/server.py:873-950](../../../agent/server.py:873)：

### 6.1 解析凭据和默认仓库

```python
github_token, _expires_at = await resolve_github_token(...)
configurable["draft_prs"] = self._draft_prs
prompt_default_repo = await _resolve_prompt_default_repo(configurable)
```

token 只在服务端准备阶段使用，随后显式 `del github_token`，避免把它放进返回的 state 或 prompt。`draft_prs` 写入 configurable，供后续 prompt/工具策略读取。

### 6.2 并行解析身份和 sandbox

```python
triggering_user_identity_task = asyncio.create_task(
    asyncio.to_thread(resolve_triggering_user_identity, ...)
)
sandbox_task = asyncio.create_task(
    ensure_sandbox_for_thread(self._thread_id, repo=prompt_default_repo)
)
triggering_user_identity, sandbox_backend = await asyncio.gather(...)
```

两个任务互不依赖，所以并行执行。`ensure_sandbox_for_thread` 负责复用、重连或首次创建 thread 对应的 sandbox；它不是每次 Run 都新建一个工作区。

如果抛出 `SandboxUnreachableError`，代码会清理进程内缓存、发送用户可见通知，然后重新抛出，让 Run 失败而不是悄悄换一个空 sandbox 丢掉未提交代码。

### 6.3 得到工作目录并读取规则

```python
work_dir = await aresolve_sandbox_work_dir(sandbox_backend)
repo_custom_instructions, user_custom_instructions = await asyncio.gather(...)
```

工作目录来自 sandbox backend。仓库级和用户级 instructions 也并行读取；它们稍后进入 `construct_system_prompt`，不是直接塞进 thread metadata。

### 6.4 为本轮建立 Git diff 起点

`_record_turn_checkpoint` 从最新的、带 id 的 `HumanMessage` 找到 `turn_key`，调用 `record_turn_checkpoint(...)` 在 sandbox 中记录 Git ref，再把 ref 合并到 thread metadata 的 `turn_checkpoints`。

这条记录用于 Dashboard 的“本轮改了哪些文件”视图。它和 LangGraph 的 state checkpoint 不同：

```text
LangGraph checkpoint -> 恢复图状态和消息
turn checkpoint      -> 通过 Git ref 计算本轮文件差异
```

### 6.5 记录 thread metadata 和用量

代码更新 thread metadata：`agent_kind`、model、effort、source、plan_mode 以及可选的 `turn_checkpoints`，再记录 agent thread usage。这个 try/except 是 best effort：统计写失败不能让已经准备好的 Agent Run 直接失败。

### 6.6 返回模型真正需要的 state

`_prepare()` 最终返回：

```python
{
    "work_dir": work_dir,
    "rendered_system_prompt": construct_system_prompt(...),
}
```

注意，它返回的是 state update，不是 `SystemMessage`，也不是模型响应。prompt 要到下一层 `awrap_model_call` 才会注入模型请求。

## 7. `awrap_model_call`：把准备结果送进模型

```python
rendered = request.state.get("rendered_system_prompt")
if isinstance(rendered, str) and rendered:
    existing = request.system_message.text if request.system_message is not None else ""
    content = f"{rendered}\n\n{existing}" if existing else rendered
    request = request.override(system_message=SystemMessage(content=content))
return await handler(request)
```

它做三件事：

1. 从 `ModelRequest.state` 读取准备阶段渲染的 prompt；
2. 如果 Deep Agents 或其他 middleware 已有 system message，把 Open SWE prompt 放在前面，中间加空行；
3. 用 `override` 创建新请求，再调用下游 `handler`。

所以 `abefore_agent` 负责“生成内容”，`awrap_model_call` 负责“注入请求”。后者不直接调用模型，而是必须继续 `await handler(request)`，否则真实 provider 永远不会执行。

## 8. 什么是“注入最终系统提示词”，怎么查看

`get_agent()` 传给 `create_deep_agent` 的 `system_prompt` 是空字符串：

```python
create_deep_agent(system_prompt="", ...)
```

因为本项目的系统提示词必须依赖每次 Run 的实际环境。`_prepare()` 调用 `construct_system_prompt(...)`，把 `work_dir`、默认仓库、触发用户身份、PR 策略、plan mode、仓库/用户自定义 instructions 等拼成 `rendered_system_prompt` 并写入 state。

随后 `PrepareAgentRunMiddleware.awrap_model_call()` 将它放进 `ModelRequest.system_message`。如果请求本来已有 system message，组合规则是：

```text
Open SWE rendered_system_prompt

已有 system message
```

这里的“最终”应理解为“Open SWE 为当前 Run 最终渲染出的动态 prompt”，不是保证它一定是 provider 请求的最后一字节。列表中更内层的 middleware，例如 `TimeoutWrapupMiddleware`，仍可能在转发给 provider 前追加自己的指令。

### 8.1 本地查看：只生成，不调用模型

下面命令打印默认情况下的 Open SWE prompt，不会访问 sandbox、GitHub、LangGraph Server 或模型：

```bash
uv run python -c 'from agent.prompt import construct_system_prompt; print(construct_system_prompt(working_dir="/workspace"))'
```

它适合先理解 prompt 的固定骨架。传入不同参数可观察分支，例如：

```bash
uv run python -c 'from agent.prompt import construct_system_prompt; print(construct_system_prompt(working_dir="/workspace", plan_mode=True, repo_custom_instructions="只修改 tests/", user_custom_instructions="使用中文回复"))'
```

### 8.2 查看某个真实 Run 写入 state 的内容

在能访问该 LangGraph Server 的受信环境中，Run 运行过后可读取 thread 最新 state：

```python
import asyncio

from langgraph_sdk import get_client


async def main() -> None:
    client = get_client(url="http://localhost:2024")
    state = await client.threads.get_state("你的-thread-id")
    values = state["values"]
    print(values.get("rendered_system_prompt"))


asyncio.run(main())
```

`state["values"]["rendered_system_prompt"]` 是第 6 节 `_prepare()` 写入 checkpoint 的版本，能看到该 thread 实际解析出的工作目录、仓库和用户/仓库 instructions。

### 8.3 查看 provider 真正收到的 messages

启用 LangSmith tracing 时，在对应的模型调用 trace 中查看 input messages；它能看见经过所有模型 middleware 后的 `system` message。不要把完整系统提示词打到生产日志或公开频道：它可能包含内部仓库规则、Dashboard URL、用户 instructions 或身份信息。

## 9. `state`、`runtime` 和 `config` 的边界

| 对象 | 本章中的用途 |
| --- | --- |
| `state` | 当前图状态，包含 messages、准备闩锁、work_dir、rendered prompt |
| `runtime` | LangGraph 运行时上下文；基类签名保留它，当前 Open SWE `_prepare` 主要使用 middleware 持有的 config 和依赖，因此没有读取它 |
| `self._config` | 工厂创建 middleware 时传入的 RunnableConfig，里面有 thread/repo/model 等 configurable 信息 |
| thread metadata | 通过 `client.threads.update` 持久化的可查询业务索引和 turn checkpoint 记录 |

不要把 `state` 和 `RunnableConfig` 混为一谈：`state` 会被图节点更新并 checkpoint；`config` 是本次运行的路由/配置输入。

## 10. 最小验证

运行配套脚本：

```bash
uv run python docs/open-swe-learning/17-create-deep-agent-call/12_prepare_run_lifecycle.py
```

预期输出：

```text
first prepare: 1
same fingerprint skipped: 1
new message prepared: 2
injected system prompt: prepared prompt
```

它只调用项目已有的 `BasePrepareRunMiddleware`，不创建 sandbox、不调用模型、不访问 LangGraph Server。对应测试还覆盖了相同 fingerprint 跳过、过期 fingerprint 重跑和 system prompt 注入：[test_prepare_run_middleware.py](../../../tests/middleware/test_prepare_run_middleware.py)。

也可以运行真实模块测试：

```bash
uv run pytest -q tests/middleware/test_prepare_run_middleware.py
```

## 11. 常见误区

### 误区一：`get_agent()` 调用时就创建 sandbox

不对。`get_agent` 只实例化 middleware；sandbox 准备在 Run 的 `abefore_agent` 阶段发生。没有执行 `ainvoke`/`astream`，就不会进入 `_prepare()`。

### 误区二：`run_prepared=True` 后永远不再准备

不对。闩锁必须和 `run_prepared_for` 同时匹配。新消息或配置变化会产生新 fingerprint，触发重新准备。

### 误区三：LangGraph checkpoint 和 Git turn checkpoint 是一回事

不对。前者恢复图状态，后者记录工作树 Git 起点供 Dashboard 计算 diff；两者都可能和 thread 关联，但用途不同。

### 误区四：准备逻辑失败后一定不会重复执行

不对。checkpoint 保存前失败时可能重试，因此 `_prepare()` 必须设计成幂等操作。

## 12. 本章掌握标准

能回答下面五个问题，就可以进入第 3 章“工具失败与副作用治理”：

1. 为什么 `run_prepared` 必须和 `run_prepared_for` 一起判断？
2. 新 HumanMessage 如何让 fingerprint 变化？
3. 为什么 checkpoint 前失败要求 `_prepare()` 幂等？
4. `work_dir` 和 `rendered_system_prompt` 是在哪一步进入 state、在哪一步进入模型请求？
5. LangGraph state checkpoint、thread metadata、Git turn checkpoint 分别解决什么问题？

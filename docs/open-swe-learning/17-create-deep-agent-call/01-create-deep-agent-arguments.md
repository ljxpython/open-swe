# 01：`create_deep_agent` 调用参数逐项解读

## 1. 先定位调用点

当前代码位于 [agent/server.py:1182-1237](../../../agent/server.py:1182)：

```python
return create_deep_agent(
    model=main_model,
    system_prompt="",
    tools=static_tools,
    subagents=[
        _general_purpose_subagent(subagent_model, skill_sources, dynamic_tool_middleware),
        *([_browser_subagent(subagent_model, browser_tools)] if browser_tools else []),
    ],
    skills=skill_sources,
    backend=agent_backend,
    middleware=cast(...),
).with_config(config)
```

调用前，`get_agent` 已完成以下准备：

```text
config
  -> thread_id / profile / model override
  -> main_model、subagent_model
  -> sandbox backend
  -> static_tools、可选集成工具
  -> /skills/ backend 路由
  -> middleware 实例
```

因此不要把这段代码理解为“create_deep_agent 自己去数据库读取所有配置”。它接收的是应用工厂已经解析好的对象。

## 2. `model=main_model`：主 Agent 的模型

```python
model=main_model
```

`main_model` 在 [agent/server.py:1176](../../../agent/server.py:1176) 由 `_make_model_or_defer(...)` 创建，最终是 `BaseChatModel` 或延迟错误模型。团队默认、用户 Profile、线程级覆盖和 Fable 门控都已经在此前完成。

这个模型服务的是**主 Agent 图**：它读取用户任务，决定直接调用哪个工具，或者是否调用 `task` 委派子 Agent。

它不等于所有子 Agent 的模型。子 Agent 通过下面的 `subagent_model` 单独指定。

## 3. `system_prompt=""`：为什么主提示词是空字符串

这里的空字符串不是“没有系统提示词”。Open SWE 把依赖当前线程的内容延迟到运行阶段：

1. `PrepareAgentRunMiddleware.abefore_agent` 调用项目的 `_prepare`。
2. `_prepare` 准备 sandbox、工作目录、回合 checkpoint，并渲染仓库/用户指令。
3. `BasePrepareRunMiddleware.awrap_model_call` 读取 `rendered_system_prompt`，把它合并进真正的 `SystemMessage`。

证据见 [agent/middleware/prepare_run.py:54-67](../../../agent/middleware/prepare_run.py:54) 和 [agent/middleware/prepare_run.py:84-94](../../../agent/middleware/prepare_run.py:84)。

这样做的原因是 system prompt 依赖每个 Run 的 thread、仓库、用户设置、计划模式和当前 sandbox，不能在进程启动时冻结。

```text
构图时：声明一个空的静态 prompt 槽位
运行前：生成本线程、本回合的真实 prompt
模型调用：收到渲染后的完整 SystemMessage
```

## 4. `tools=static_tools`：业务静态工具

```python
tools=static_tools
```

`static_tools` 在 [agent/server.py:1119-1146](../../../agent/server.py:1119) 中组装，包含搜索、HTTP、Linear、Slack、PR、计划和 sandbox 管理等 Open SWE 业务工具。

它们与 Deep Agents 内置文件工具不同：

| 工具来源 | 进入方式 | 示例 |
| --- | --- | --- |
| Open SWE 业务工具 | `tools=static_tools` | `linear_comment`、`open_pull_request` |
| Deep Agents 文件工具 | `backend` + 内置 `FilesystemMiddleware` | `read_file`、`edit_file`、`execute` |
| 集成工具 | `DynamicToolMiddleware` | Corridor、Notion、Currents |
| 子 Agent 工具 | `subagents` 规格和 Deep Agents 继承规则 | `task` 委派的子图 |

所以不能为了“补齐工具”而把 `read_file`、`execute` 再塞进 `static_tools`，这会造成重复注册或命名冲突。

## 5. `subagents=[...]`：传入子 Agent 规格，不是普通工具列表

### 5.1 通用子 Agent

```python
_general_purpose_subagent(
    subagent_model,
    skill_sources,
    dynamic_tool_middleware,
)
```

该函数返回 `SubAgent` 字典，核心字段是：

```python
{
    "name": "general-purpose",
    "description": "...",
    "system_prompt": OPEN_SWE_SHARED_BASE + "\n\n" + DEFAULT_SUBAGENT_PROMPT,
    "model": subagent_model,
    "middleware": [dynamic_tool_middleware?, ModelCallTimeoutMiddleware()],
    "skills": ["/skills/"]?,
}
```

它没有显式写 `tools`。当前 Deep Agents 版本在 [deepagents/graph.py:728](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:728) 执行：

```python
raw_subagent_tools = spec.get("tools") if "tools" in spec else tools
```

因此通用子 Agent 继承主 Agent 传入的 `static_tools`，同时 Deep Agents 还会为它添加文件系统工具、摘要 middleware 和 `SkillsMiddleware`。

### 5.2 浏览器子 Agent

```python
*([_browser_subagent(subagent_model, browser_tools)] if browser_tools else [])
```

这是条件展开：

- `browser_tools` 有值：列表中加入一个浏览器专用子 Agent。
- `browser_tools` 为空：展开空列表，不注册浏览器子 Agent。

这避免了把“没有浏览器能力”的空壳 Agent 暴露给主模型。浏览器子 Agent 使用自己的提示词和浏览器工具，和 `general-purpose` 的文件/研究职责不同。

### 5.3 父模型看到的是什么

父模型通常不会看到一个叫 `general-purpose` 的图节点；Deep Agents 的 `SubAgentMiddleware` 会创建一个统一的 `task` 工具。该工具描述中列出可用子 Agent 名称和职责，模型通过参数选择：

```python
task(
    subagent_type="general-purpose",
    description="独立分析测试失败原因，并返回带文件位置的结论",
)
```

## 6. `skills=skill_sources`：主 Agent 的技能目录

```python
skills=skill_sources
```

登录用户存在 Skills 路由时，`skill_sources` 是 `['/skills/']`；没有登录身份时为 `None`。

`agent_backend` 使用 `CompositeBackend` 把 `/skills/` 路由到只读的 Store backend：

```text
默认路径       -> 当前线程 sandbox
/skills/       -> StoreBackend(namespace=(SKILLS_NAMESPACE, profile_login))
```

Deep Agents 看到 `skills` 后加入 `SkillsMiddleware`。它先把技能元数据写入 state，再把技能列表追加到 system prompt；模型需要调用 `read_file('/skills/<name>/SKILL.md')` 才能读取完整技能内容。

这里的 `skills` 是“技能文件来源”，不是 Python 函数列表，也不是模型自动执行的插件。

## 7. `backend=agent_backend`：文件工具的实际工作区

```python
backend=agent_backend
```

这个对象来自当前 thread 的 sandbox backend。Deep Agents 的文件工具都通过它工作，因此 `read_file`、`write_file`、`execute` 操作的是当前线程 sandbox，而不是运行 LangGraph 服务的任意本地目录。

当同时使用 `CompositeBackend` 时，backend 根据路径分流：

```text
/workspace/... -> sandbox
/skills/...    -> 只读 Store 路由
```

这是能力边界的关键：工具名称只是接口，真正决定读写对象的是 backend。

## 8. `middleware=...`：把应用策略交给图

```python
middleware=cast(
    list[AgentMiddleware[Any, Any, Any]],
    [...],
)
```

`cast` 只服务于静态类型检查，不会在运行时转换对象。列表里的每个实例才是真正的行为：准备 sandbox、清理输入、限制预算、处理错误、重试 `task`、刷新代理凭据、插入队列消息、fallback 和 timeout。

具体顺序见下一篇：[02：middleware 列表与顺序](02-middleware-stack-line-by-line.md)。

## 9. `.with_config(config)`：绑定本次运行配置

`create_deep_agent` 返回一个 Runnable/CompiledStateGraph。`.with_config(config)` 返回带默认 `RunnableConfig` 的绑定对象，不会调用模型，也不会创建新的 thread。

```text
get_agent 前半段：读取 config，决定构图内容
with_config(config)：把 config 带到后续图执行和 middleware
ainvoke/astream：此时才真正执行图
```

`thread_id` 的 checkpoint 持久化由 LangGraph Runtime/checkpointer 负责，不是 `with_config` 自己写数据库。

## 10. 参数到能力的映射表

| 参数 | 调用前对象 | `create_deep_agent` 产生的能力 |
| --- | --- | --- |
| `model` | 主 `BaseChatModel` | 主 Agent 的模型调用 |
| `system_prompt` | 空字符串 | 由运行准备 middleware 动态补全 |
| `tools` | Open SWE 静态工具列表 | 主 Agent 可直接调用的业务工具 |
| `subagents` | `SubAgent` 规格列表 | `task` 工具和独立子图 |
| `skills` | `/skills/` 路由列表或 `None` | Skills 元数据加载和 prompt 注入 |
| `backend` | 当前 thread sandbox/composite backend | 文件工具的读写/执行目标 |
| `middleware` | 有序策略对象列表 | 每次 Agent/模型/工具调用的保护逻辑 |

## 本篇小结

这段调用的本质是“把已解析的应用资源声明成 Deep Agent 能力”。主模型、业务工具、backend、子 Agent 和 middleware 各自负责不同边界；`create_deep_agent` 负责把它们编译成可执行图。


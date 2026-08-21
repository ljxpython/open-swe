# 04：默认子智能体、继承关系与 `_general_purpose_subagent` 二次改造

## 1. Deep Agents 自带的子智能体叫什么

Deep Agents 的默认通用子智能体名称是：

```text
general-purpose
```

定义位于当前安装版本的：

```text
.venv/lib/python3.11/site-packages/deepagents/middleware/subagents.py:301-306
```

核心定义是：

```python
GENERAL_PURPOSE_SUBAGENT = {
    "name": "general-purpose",
    "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
    "system_prompt": DEFAULT_SUBAGENT_PROMPT,
}
```

默认描述的意思是：它负责复杂问题研究、文件搜索和多步骤任务；当主 Agent 不确定第一次搜索能否找到目标时，可以委派给它。

默认 prompt 还强调一个重要协议：**调用方只能看到子 Agent 的最终 assistant message，看不到子 Agent 的中间消息、工具结果和状态**。所以子 Agent 必须把完整结论写进最终回复。

## 2. 默认使用时，什么叫“继承主 Agent”

假设只写：

```python
agent = create_deep_agent(
    model=main_model,
    tools=tools,
    skills=skills,
    backend=backend,
)
```

没有显式传入 `subagents` 时，Deep Agents 会自动加入一个 `general-purpose` 子 Agent，除非 harness profile 禁用了它。自动装配逻辑位于：

```text
.venv/lib/python3.11/site-packages/deepagents/graph.py:745-814
```

默认情况下可以这样理解：

| 能力 | 是否继承 | 具体规则 |
| --- | --- | --- |
| 模型 | 是 | 没有单独模型时使用主 Agent 的 `model` |
| 工具 | 是 | 子 Agent 没有 `tools` 时使用主 Agent 的 `tools` |
| backend | 是 | 使用同一个 `create_deep_agent(backend=...)` backend |
| 文件工具 | 是 | 子图自动添加 `FilesystemMiddleware`，操作同一 backend |
| 文件权限 | 通常是 | 子 Agent 没有 `permissions` 时使用父 Agent 权限 |
| skills | 默认 GP 会接入 | 主调用的 `skills` 被用于创建 GP 的 `SkillsMiddleware` |
| 摘要能力 | 自动拥有 | 子图自动添加 summarization middleware |
| 工具调用修补 | 自动拥有 | 子图自动添加 `PatchToolCallsMiddleware` |
| 父 Agent 全部业务 middleware | 否 | 只有 Deep Agents 规定的可继承核心槽位可能被替换 |
| 父 Agent 当前 messages | 否 | `task` 调用会用独立的 description 作为新 HumanMessage |

最容易错的地方是最后三项：子 Agent 是独立图，不是把父图复制一份再递归执行。

## 3. Deep Agents 的实际继承代码

### 3.1 模型继承

处理 raw `SubAgent` 时，Deep Agents 使用：

```python
raw_subagent_model = spec.get("model", model)
```

源码见 [deepagents/graph.py:657](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:657)。

因此：

```python
SubAgent 不写 model -> 使用主 Agent model
SubAgent 写 model    -> 使用子 Agent自己的 model
```

### 3.2 工具继承

源码是：

```python
raw_subagent_tools = spec.get("tools") if "tools" in spec else tools
```

见 [deepagents/graph.py:726-738](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:726)。

注意这里判断的是 `"tools" in spec`，不是简单的 `spec.get("tools")`：

```python
{}                         # 没有 tools，继承父工具
{"tools": []}             # 明确声明空工具，不继承父工具
{"tools": custom_tools}   # 使用自定义工具
```

这是二次改造时最有用的开关。

### 3.3 backend 与文件能力

子 Agent 的 `FilesystemMiddleware` 使用父 `create_deep_agent` 传入的 backend：

```python
FilesystemMiddleware(
    backend=backend,
    _permissions=subagent_permissions,
)
```

见 [deepagents/graph.py:667-675](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:667)。

所以 Open SWE 中的主 Agent 和 `general-purpose` 子 Agent 都能访问当前线程的 sandbox；这不代表它们共享同一个 message 历史，而是代表它们使用同一个文件工作区 backend。

### 3.4 权限继承

源码规则是：

```python
subagent_permissions = spec.get("permissions", permissions)
```

子 Agent 没有单独权限时继承父权限；写了 `permissions` 后则替换父规则，而不是追加。

这比 prompt 中写“只读”可靠得多，因为 `FilesystemMiddleware` 会真正执行权限规则。

### 3.5 middleware 不是全部继承

Deep Agents 会给每个 raw 子 Agent 自动创建自己的基础 middleware：

```python
[
    FilesystemMiddleware(...),
    create_summarization_middleware(...),
    PatchToolCallsMiddleware(),
]
```

如果子 Agent 有 skills，再追加自己的 `SkillsMiddleware`。

然后它只从父 middleware 中挑选能够覆盖这些核心槽位的项。Open SWE 的这些主图专属 middleware 不会自动进入子图：

```text
PrepareAgentRunMiddleware
ModelCallLimitMiddleware
ToolErrorMiddleware
PullRequestCreationGuardMiddleware
check_message_queue_before_model
PlanModeMiddleware
ModelFallbackMiddleware
```

因此不能说“子 Agent 完全继承主 Agent 的 middleware”。准确说法是：**资源配置有继承，运行策略需要显式安装**。

## 4. Open SWE 为什么自己调用 `_general_purpose_subagent`

Open SWE 在 [agent/server.py:1186-1188](../../../agent/server.py:1186) 显式传入：

```python
subagents=[
    _general_purpose_subagent(
        subagent_model,
        skill_sources,
        dynamic_tool_middleware,
    ),
    *([_browser_subagent(subagent_model, browser_tools)] if browser_tools else []),
]
```

一旦显式提供了名称为 `general-purpose` 的 spec，Deep Agents 就不会再自动创建一个同名默认子 Agent。也就是说，Open SWE 是在覆盖默认 GP 子 Agent，而不是额外创建第二个同名 Agent。

## 5. `_general_purpose_subagent` 做了哪些二次改造

函数位于 [agent/server.py:606-626](../../../agent/server.py:606)：

```python
def _general_purpose_subagent(
    model: BaseChatModel,
    skills: list[str] | None = None,
    dynamic_tools: DynamicToolMiddleware | None = None,
) -> SubAgent:
    subagent: SubAgent = {
        "name": GENERAL_PURPOSE_SUBAGENT["name"],
        "description": GENERAL_PURPOSE_SUBAGENT["description"],
        "system_prompt": OPEN_SWE_SHARED_BASE + "\n\n" + GENERAL_PURPOSE_SUBAGENT["system_prompt"],
        "model": model,
        "middleware": [
            *([dynamic_tools] if dynamic_tools else []),
            *_subagent_model_timeout_middleware(),
        ],
    }
    if skills:
        subagent["skills"] = skills
    return subagent
```

### 5.1 保留框架身份

```python
"name": GENERAL_PURPOSE_SUBAGENT["name"],
"description": GENERAL_PURPOSE_SUBAGENT["description"],
```

项目没有重新发明名字和委派描述，而是复用 Deep Agents 的 `general-purpose` 定义。这样父 Agent 的 `task` 工具仍然使用框架约定的子 Agent 名称和职责描述。

### 5.2 替换模型

```python
"model": model,
```

Open SWE 把已经按照团队、Profile、线程配置解析好的 `subagent_model` 塞进 spec。

因此项目可以让：

```text
主 Agent -> main_model
general-purpose -> subagent_model
```

两者使用不同模型和 effort。这里是显式覆盖，不是默认继承。

### 5.3 拼接 Open SWE 项目 prompt

```python
"system_prompt": OPEN_SWE_SHARED_BASE
    + "\n\n"
    + GENERAL_PURPOSE_SUBAGENT["system_prompt"]
```

Deep Agents 默认 prompt 只描述“如何作为一个被委派的子 Agent 工作”；`OPEN_SWE_SHARED_BASE` 再补充 Open SWE 的身份、GitHub proxy、工具节奏和项目约定。

最终形成：

```text
Open SWE 工作约定
  +
Deep Agents 子任务协议
```

### 5.4 给子 Agent 增加动态集成工具

```python
*([dynamic_tools] if dynamic_tools else []),
```

当 Corridor、Observability、Currents 或 Notion 等集成工具存在时，`dynamic_tools` 会进入子图 middleware。

子 Agent 不会直接看到所有集成工具 schema，而是遵循同样的两阶段机制：

```text
先看到 load_integration_tools
  -> 加载指定工具名称
  -> 下一次模型调用才看到工具 schema
  -> 正常调用已加载工具
```

这个 middleware 被同时放到主图和通用子图，保证两者遵循同一套动态工具治理规则。

### 5.5 给子 Agent 增加独立超时

```python
*_subagent_model_timeout_middleware(),
```

`_subagent_model_timeout_middleware()` 返回一个新的 `ModelCallTimeoutMiddleware`。原因是子图独立编译，主图末尾的 timeout 不会包住子图里的 provider 调用。

```text
父图调用 task
  -> 父图只是在等待一个工具执行
  -> 子图内部自行调用模型
  -> 子图必须有自己的 timeout
```

这不是重复配置，而是两个不同的模型调用边界。

### 5.6 显式传入用户 Skills

```python
if skills:
    subagent["skills"] = skills
```

对于当前 Open SWE，`skills` 一般是 `['/skills/']`。显式放入 spec 很重要：它保证这个手工覆盖的 `general-purpose` 子 Agent 仍然拥有用户 Skills 路由。

不要把“主调用传了 `skills=skill_sources`”和“显式子 Agent 自动继承 skills”混为一谈。当前项目是通过 `_general_purpose_subagent` 明确写入 `subagent["skills"]` 的。

## 6. 默认 GP 与 Open SWE GP 的对照

| 项目 | Deep Agents 默认 GP | Open SWE `_general_purpose_subagent` |
| --- | --- | --- |
| 名称 | `general-purpose` | 复用 `GENERAL_PURPOSE_SUBAGENT["name"]` |
| 描述 | 默认通用研究/多步骤描述 | 复用默认描述 |
| 模型 | 默认使用主 Agent model | 显式使用解析后的 `subagent_model` |
| prompt | `DEFAULT_SUBAGENT_PROMPT` | Open SWE shared base + 默认 prompt |
| tools | 默认继承主 Agent tools | 不写 `tools`，因此仍继承主 Agent tools |
| backend | 使用父 create_deep_agent backend | 使用当前线程 sandbox/composite backend |
| skills | 默认 GP 自动接入主调用 skills | 显式写入 `skill_sources` |
| 动态集成工具 | 默认没有 Open SWE 动态工具 | 显式加入 `DynamicToolMiddleware` |
| 子模型超时 | 取决于框架/调用方配置 | 显式加入独立 `ModelCallTimeoutMiddleware` |
| 是否自动添加 | 由 Deep Agents 自动添加 | Open SWE 显式提供同名 spec，覆盖默认项 |

## 7. 二次改造时应该改哪些字段

### 7.1 只改职责，不改底层能力

适合专门做代码搜索、测试分析或文档研究：

```python
def research_subagent(model: BaseChatModel) -> SubAgent:
    return {
        "name": "repo-researcher",
        "description": "只研究仓库结构和实现，返回带文件位置的结论",
        "system_prompt": "你是仓库研究员。先搜索和阅读，最后输出证据链。",
        "model": model,
    }
```

这里没有写 `tools`，所以仍继承父工具；没有写 `backend`，因为 backend 不是 SubAgent spec 字段，而是由 `create_deep_agent` 统一提供。

### 7.2 做只读子 Agent

不要只依赖 prompt：

```python
readonly_subagent = {
    "name": "readonly-researcher",
    "description": "只读取和分析文件，不修改工作区",
    "system_prompt": "只做研究，不得修改文件或执行有副作用操作。",
    "model": model,
    "permissions": readonly_permissions,
    "tools": readonly_tools,
}
```

`tools=[]` 可以完全切断继承的父业务工具；`permissions` 可以限制文件系统能力。但如果 `tools` 中仍包含 `execute` 或外部写入工具，prompt 不能替代真实权限治理。

### 7.3 继承父工具但增加专属 middleware

这正是 Open SWE 当前模式：

```python
{
    "name": "general-purpose",
    "description": "...",
    "system_prompt": "...",
    "model": subagent_model,
    # 不写 tools -> 继承父工具
    "middleware": [
        dynamic_tool_middleware,
        ModelCallTimeoutMiddleware(),
    ],
    "skills": ["/skills/"],
}
```

这是最小改造方式：保留 Deep Agents 自动提供的文件、摘要、Skills 和 `task` 运行机制，只增加当前业务真正需要的两项策略。

## 8. 不应该怎么改

### 错误一：把子 Agent 当普通工具

不要把 `general-purpose` 放进 `tools=[...]`。它是一个 `SubAgent` spec，由 `SubAgentMiddleware` 编译成子图，再通过 `task` 工具调用。

### 错误二：以为父 middleware 全部保护子图

父图的 Plan Mode、PR guard、消息队列、fallback 等不会自动覆盖子图。涉及安全、外部副作用和超时的策略，要在子 Agent spec 中显式安装或从源头禁止 `task`。

### 错误三：写空 `tools` 却以为会继承

```python
{"tools": []}
```

表示明确使用空工具列表；只有完全省略 `tools` 才会触发继承。

### 错误四：只改 prompt 实现安全边界

prompt 是行为指导，不是权限系统。文件权限使用 `permissions`，工具范围使用 `tools`，外部副作用使用 guard middleware。

## 9. 一张最终关系图

```text
create_deep_agent(
    model=main_model,
    tools=static_tools,
    skills=skill_sources,
    backend=agent_backend,
    subagents=[_general_purpose_subagent(...)],
)
        |
        +--> 主 Agent
        |      - main_model
        |      - static_tools
        |      - 主图 middleware
        |
        +--> general-purpose 子 Agent
               - subagent_model（Open SWE 显式覆盖）
               - static_tools（省略 tools，继承）
               - 同一个 agent_backend
               - skills（Open SWE 显式传入）
               - DynamicToolMiddleware（显式加入）
               - ModelCallTimeoutMiddleware（独立加入）
               - Filesystem/Summarization/Patch（Deep Agents 自动加入）
               - 通过 task 工具被主 Agent 调用
```

## 小结

默认子智能体叫 `general-purpose`。默认使用时，它继承主 Agent 的模型、工具、backend 和权限等资源，但作为独立子图运行，不继承父图全部 middleware 和完整消息历史。

Open SWE 的 `_general_purpose_subagent` 没有重写 Deep Agents 的整个子 Agent 机制，而是做了四个精准改造：替换子模型、拼接项目 prompt、加入动态工具 middleware、加入独立模型超时，并显式保留用户 Skills。这个模式适合项目二次开发：**框架负责通用子图能力，项目只覆盖真正不同的业务边界。**


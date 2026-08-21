# 03. 让 Agent 成为可装配产品

## 学习目标

理解为什么主 Agent 不是一个全局单例，为什么 `get_agent(config)` 会在每个线程运行时解析模型、身份、Sandbox、工具和提示词。

## 概念全解

一个“Agent 配置”至少有两类东西：

- **相对稳定的能力**：基础工具、通用中间件、默认提示词模板。
- **本次运行才知道的上下文**：是谁触发、在哪个仓库、使用哪种模型、当前工作目录、是否进入计划模式、有哪些用户/仓库指令。

把后一类塞进进程全局变量，多个用户和多线程一上来就串线。Open SWE 用工厂函数按 `thread_id` 创建图，并用 `PrepareAgentRunMiddleware` 在第一轮模型调用前补齐运行期信息。

## 架构图

![Agent 设计蓝图](architecture/png/02-agent-design-blueprint.png)

[Draw.io](architecture/02-agent-design-blueprint.drawio) · [HTML](architecture/html/02-agent-design-blueprint.html)

图中紫色层就是“组装 Agent”的位置：`get_agent` 选模型和后端，`PrepareAgentRunMiddleware` 把动态上下文放进状态，`create_deep_agent` 再提供实际的推理-工具循环。

- Agent 工厂：[agent/server.py](../../agent/server.py:951) 的 `get_agent` 解析线程配置、profile/team 默认值、模型和工具。
- 运行准备：[agent/server.py](../../agent/server.py:803) 的 `PrepareAgentRunMiddleware` 解析令牌、准备 Sandbox、渲染提示词并记录元数据。
- 系统提示词：[agent/prompt.py](../../agent/prompt.py:345) 的 `construct_system_prompt` 合并工作目录、来源渠道、仓库/用户指令和计划模式。
- Deep Agent：[agent/server.py](../../agent/server.py:1181) 的 `create_deep_agent` 注入模型、工具、子 Agent、backend 和 middleware。

## 项目中的完整路径

1. Runtime 依据 `langgraph.json` 找到 `agent.graphs.agent:traced_agent`。
2. `traced_agent` 调用 `get_agent(config)`；无 `thread_id` 时返回空工具图，用于加载/探测，真正执行才进入完整装配。
3. 工厂按“线程覆盖 > 用户 profile > 团队默认”解析模型和推理强度。
4. `PrepareAgentRunMiddleware` 在 run 开始准备 Sandbox、读出仓库和用户自定义指令，生成 `rendered_system_prompt`。
5. `create_deep_agent` 将内置文件/Shell/子任务能力，与 Open SWE 的平台工具和中间件组合起来。

```python
# 与 agent/server.py 的设计对应；省略了细节
async def get_agent(config):
    thread_id = config["configurable"]["thread_id"]
    model = resolve_model(config)
    backend = sandbox_backend_for(thread_id)
    return create_deep_agent(
        model=model,
        backend=backend,
        tools=curated_tools,
        middleware=[PrepareAgentRunMiddleware(...), *guardrails],
    )
```

这里有一个常见误解：`create_deep_agent` 的 `system_prompt=""` 不代表没有系统提示词。项目把动态提示词留给准备中间件写入 `rendered_system_prompt`，因为工作目录、仓库、用户和计划状态都必须在运行时才能确定。

## 最小可运行示例

为自己的 Agent 建一个不可变配置和一次运行配置：

```python
BASE = {"tools": ["read", "search"], "max_steps": 30}
run_context = {
    "thread_id": "task-42",
    "actor_id": "u-17",
    "repo": "acme/app",
    "work_dir": "/sandbox/task-42",
}
```

原则：`BASE` 可复用，`run_context` 必须显式传递，不要写进模块级全局变量。

## 常见误区与反例

1. 一个全局 Agent 复用所有用户的工具和 prompt：容易泄漏前一位用户的仓库或凭据上下文。
2. 把大段动态信息拼到用户消息里：模型会把系统约束、外部不可信内容和用户要求混淆。
3. 每次都从零初始化所有外部资源：速度慢、失败面大；应像本项目一样按线程复用可恢复资源。

## 扩展边界与练习

- 初期只需一个 `build_agent(run_context)` 函数，不需要复杂配置中心。
- 当模型/工具随租户、仓库或风险等级变化，再引入明确的覆盖优先级。

练习：为“客服退款 Agent”写出 5 个必须在运行期解析的字段，并说明其中哪一个绝不能由用户输入直接决定。

# 专题：`get_agent()` 中的 `create_deep_agent(...)`

这个专题只研究 `agent/server.py:get_agent` 最后的装配代码：

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
    middleware=[...],
).with_config(config)
```

它不是另一个完整的 `get_agent` 教程。`02-1-main-agent-factory.md` 已经解释了工厂如何取得配置、sandbox 和模型；这里继续向下钻，回答三个问题：

1. 每个参数在调用前已经是什么对象，进入 `create_deep_agent` 后会变成什么能力？
2. middleware 为什么按当前顺序排列，哪一层负责准备、重试、降级和超时？
3. 调用返回的图什么时候才真正执行模型，主 Agent 如何调用 `general-purpose` 子 Agent？

## 阅读顺序

1. [01：调用参数逐项解读](01-create-deep-agent-arguments.md)
2. [02：middleware 列表与顺序](02-middleware-stack-line-by-line.md)
3. [03：构图、配置绑定与运行边界](03-build-vs-run-and-subagents.md)
4. [04：默认子智能体继承与 `_general_purpose_subagent` 改造](04-default-subagent-inheritance-and-customization.md)
5. [05：浏览器子 Agent 与条件列表解包](05-browser-subagent-and-conditional-unpacking.md)
6. [06：`task` 机制与多个子智能体如何协作](06-task-mechanism-and-subagent-collaboration.md)
7. [07：多智能体编排边界：Deep Agents 还是 LangGraph](07-multi-agent-orchestration-boundaries.md)
8. [08：`skills=skill_sources` 与 `backend=agent_backend`](08-skills-sources-and-composite-backend.md)
9. [09：中间件学习路线：按生命周期、数据流和故障流掌握](09-middleware-learning-roadmap.md)
10. [10：先写一个完整的 `create_deep_agent` 中间件](10-write-a-custom-middleware-basics.md)
11. [11：生命周期钩子与 wrapper 嵌套](11-hooks-and-wrapper-nesting.md)
12. [12：`PrepareAgentRunMiddleware`：一次 Run 如何准备并可恢复](12-prepare-run-lifecycle.md)
13. [13：工具失败与副作用治理](13-tool-failure-and-side-effect-governance.md)
14. [14：模型可靠性与协议适配](14-model-reliability-and-protocol-adaptation.md)

## 源码范围

- 主工厂：[agent/server.py:1182](../../../agent/server.py:1182)
- 子 Agent 工厂：[agent/server.py:606](../../../agent/server.py:606)
- 运行准备：[agent/middleware/prepare_run.py:41](../../../agent/middleware/prepare_run.py:41)
- 动态工具：[agent/middleware/dynamic_tools.py:30](../../../agent/middleware/dynamic_tools.py:30)
- Deep Agents 当前安装版本实现：[.venv/lib/python3.11/site-packages/deepagents/graph.py:268](../../../.venv/lib/python3.11/site-packages/deepagents/graph.py:268)

## 总体心智模型

```text
get_agent(config)
  ├─ 已经解析 main_model / subagent_model
  ├─ 已经准备 agent_backend / skill_sources / static_tools
  ├─ 创建 SubAgent 配置
  ├─ 把 middleware 顺序交给 create_deep_agent
  ├─ Deep Agents 补入文件工具、task、SkillsMiddleware
  └─ 返回 CompiledStateGraph，再绑定 config

后续 Run 才会：
  before_agent -> 模型 -> 工具 -> 模型 -> ... -> 最终消息
```

`create_deep_agent(...)` 是装配和编译，不是 LLM 请求。真正的模型调用只能在后续 `ainvoke`、`astream` 或 LangGraph Runtime 执行 Run 时发生。

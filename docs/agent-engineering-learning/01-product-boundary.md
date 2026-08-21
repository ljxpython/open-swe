# 01. 先划清产品边界

## 学习目标

学完后，你能先定义 Agent 的职责和危险动作，再去选模型或写提示词；也能看懂 Open SWE 为什么同时有 FastAPI、LangGraph Runtime、Sandbox 和多个 Agent 图。

## 概念全解

把 Agent 想成一个“能提出下一步动作建议的执行员”，不是一个万能后端。它最适合处理目标模糊、步骤因上下文而变、需要在工具结果之间继续判断的工作，例如“修复这个 CI 失败并开 PR”。

但模型不应该拥有系统的最终控制权：

- 模型：负责理解目标、选择下一次读取/搜索/修改/验证动作；不负责自己决定权限、绕过审计或伪造工具结果。
- 工具层：负责把有限能力变成有参数、有返回值的动作；不应暴露一条无约束的万能管理接口。
- 中间件/策略：负责限制工具、次数、时间、输入和副作用；不能依赖模型“自觉遵守”。
- Runtime：负责保存任务历史、恢复和流式事件；不负责理解业务语义。
- Sandbox：负责隔离文件与命令执行；不是唯一业务真相的存储位置。
- FastAPI/入口：负责鉴权、接收 Webhook、对 UI 提供业务 API；不承担 Agent 的推理循环。

**先画边界，后写 Agent。** 若一个需求其实是固定的 `POST /invoice` 业务规则，就写普通服务；只有“看什么、怎么做、做几步”会变化时，才需要 Agent loop。

## 架构图

![系统上下文](architecture/png/01-system-context.png)

[Draw.io](architecture/01-system-context.drawio) · [HTML](architecture/html/01-system-context.html)

这张 C4 图从外部协作者开始，逐层下钻到 Runtime 和 Agent 组件。箭头不是“所有请求都经过所有组件”，而是说明各组件间真实存在的主要依赖关系。

- FastAPI 接入：[agent/api/app.py](../../agent/api/app.py:32) 的 `create_app` 挂载 Dashboard、Webhook 和健康检查路由。
- 图注册：[langgraph.json](../../langgraph.json:1) 的 `graphs` 注册 `agent`、`reviewer`、`analyzer`、`chat`、`scheduler`。
- 主 Agent：[agent/server.py](../../agent/server.py:951) 的 `get_agent` 按线程配置组装 Deep Agent。
- 专用图：[agent/reviewer.py](../../agent/reviewer.py:1402) 的 `get_reviewer_agent` 将审查限制为只读工具集。

## 项目中的完整路径

Open SWE 不把“收到 GitHub 评论”直接交给模型。入口先校验事件，派发层创建可恢复运行，Runtime 再调用图工厂，工厂才构造该线程的 Agent。这样模型面对的是已准备好的上下文和受控能力。

```text
GitHub/Slack/Linear/Dashboard
  -> FastAPI：验证来源和用户
  -> dispatch：创建统一的 durable run
  -> LangGraph：持久化并运行指定 graph
  -> get_agent：准备上下文，装配模型/工具/中间件
  -> Deep Agent：循环执行任务
```

## 最小可运行示例

先不要写多 Agent。一个最小设计表可以决定是否值得引入 Agent：

```text
输入：用户说“修复 PR #123 的失败测试”
允许读取：PR diff、仓库文件、CI 日志
允许写入：隔离工作区
允许外部副作用：创建草稿 PR，需走专用工具
终止：测试通过且结果已回复；或缺少凭据/证据时明确阻塞
```

若你不能写清这五项，先补产品边界，不要急着调模型。

## 常见误区与反例

1. “给模型一个 shell 就够了”：这会让审计、权限、输入验证和副作用归属全部失控。
2. “所有能力都塞进主 Agent”：审查、问答和代码修改有不同权限，混用工具等于把最小权限原则拆掉。

## 扩展边界与练习

- 小团队内部工具：一个 Agent + 少量只读工具 + 明确写入工具即可，不必先引入多图。
- 对外或高权限系统：再增加身份、线程持久化、Sandbox、审计和审批。

练习：列出你想做的一个 Agent 的“允许动作”和“禁止动作”；再找出其中至少一个动作应由普通 API 而不是模型决定。

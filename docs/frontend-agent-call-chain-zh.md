# 前端对话如何调用后端智能体

本文说明 Open SWE 前端对话框从用户点击发送，到后端智能体执行，再到结果流式返回页面的完整过程。

## 一、整体调用链

普通云端对话的链路如下：

```text
用户输入文字、图片、模型和仓库
              |
              v
React 对话框（AgentsHome / AgentThreadView）
              |
              v
@langchain/react 的 StreamProvider
              |
              +--> POST /dashboard/api/threads/{thread_id}/commands
              |        启动或控制一次 Agent 运行
              |
              +--> POST /dashboard/api/threads/{thread_id}/stream/events
                       接收实时事件流
              |
              v
FastAPI dashboard 路由
              |
              v
LangGraph Runtime
              |
              v
assistant_id = "agent"
              |
              v
agent.graphs.agent:traced_agent
              |
              v
模型 + Deep Agents 工具 + 沙箱 + 子 Agent
              |
              v
SSE 事件返回前端并渲染成消息、工具卡片和代码变更
```

前端不会直接导入 `agent/server.py`，也不会直接调用模型。前端只调用带登录 Cookie 的 HTTP/SSE 接口，FastAPI 负责鉴权、补充运行配置和代理到 LangGraph Runtime。

## 二、用户点击发送后发生什么

入口主要是：

- `ui/src/features/agents/components/AgentsHome.tsx`
- `ui/src/features/agents/components/AgentThreadView.tsx`
- `ui/src/features/agents/components/composer/ChatComposer.tsx`

前端会收集以下内容：

- 用户文字
- 图片附件
- 选择的模型
- 推理 effort
- 目标仓库
- 是否启用 `plan_mode`

然后调用 `stream.submit()`，大致等价于发送一个 `run.start` 命令：

```ts
stream.submit(
  {
    messages: [{ type: "human", content: prompt }],
  },
  {
    config: {
      configurable: {
        agent_model_id,
        agent_effort,
        repo,
        plan_mode,
      },
    },
  },
)
```

代码位置：`ui/src/features/agents/components/AgentsHome.tsx:166`。

## 三、`StreamProvider` 如何连接后端

`ui/src/features/agents/lib/AgentThreadStreamProvider.tsx` 创建了一个 `ProtocolSseTransportAdapter`，地址指向：

```text
/dashboard/api
```

它还统一设置：

```ts
credentials: "include"
```

所以浏览器会自动带上登录 Cookie。SDK 通过 LangGraph 的流式协议访问这些接口：

```text
POST /dashboard/api/threads/{thread_id}/commands
POST /dashboard/api/threads/{thread_id}/stream/events
POST /dashboard/api/threads/{thread_id}/history
GET  /dashboard/api/threads/{thread_id}/state
```

`AgentThreadStreamProvider` 挂在整个 `/agents` 页面布局上，因此从首页跳到具体线程时，流式连接不会被重新创建，正在执行的任务也能继续显示。

## 四、FastAPI 做了什么

路由统一挂载在 `agent/dashboard/routes.py` 的 `/dashboard/api` 前缀下，最终由 `agent/api/app.py` 加入 FastAPI 应用。

执行命令时，后端会先完成这些检查：

1. 检查用户登录会话。
2. 检查用户是否能访问这个线程。
3. 检查 GitHub token 是否有效。
4. 校验模型、effort、图片格式和大小。
5. 合并用户、团队和线程级配置。
6. 将命令转发给 LangGraph Runtime。

相关代码：

- `agent/dashboard/routes.py:1798`：流式事件接口。
- `agent/dashboard/routes.py:1819`：命令接口。
- `agent/dashboard/thread_api.py:2038`：命令代理。
- `agent/dashboard/thread_api.py:1998`：事件流代理。

后端转发时还会从环境变量读取 LangSmith/LangGraph API Key，并放入内部请求头 `X-API-Key`。真实的模型调用发生在 LangGraph Runtime，不发生在这层 FastAPI 路由里。

## 五、第一次发送如何创建线程

新建对话时，前端还没有对应的 LangGraph 线程记录。第一次 `stream.submit()` 会发送 `run.start`。

后端发现线程不存在后，会延迟创建线程并写入 metadata，包括：

- 线程 ID
- 当前 GitHub 用户
- 用户邮箱
- 目标仓库
- 对话标题
- 选择的模型和 effort
- 是否处于计划模式
- 来源是否为 `dashboard`

随后后端强制设置：

```python
params["assistant_id"] = "agent"
```

并开启 `messages`、`tools`、`events` 等流式模式。

代码位置：`agent/dashboard/thread_api.py:1269-1392`。

## 六、`assistant_id = "agent"` 对应哪个智能体

`langgraph.json` 中的映射是：

```json
{
  "graphs": {
    "agent": "agent.graphs.agent:traced_agent"
  }
}
```

因此普通 Agents 页面调用关系是：

```text
assistant_id = "agent"
        |
        v
agent.graphs.agent:traced_agent
        |
        v
主编码 Agent
        |
        +--> 调用模型
        +--> 读取/修改文件
        +--> 执行命令
        +--> 操作 GitHub
        +--> 使用沙箱
        +--> 通过 task 工具启动子 Agent
```

前端不需要知道每个工具或子 Agent 的实现，只需要接收 LangGraph 的事件流。

## 七、Agent 运行中继续追问

如果当前线程正在运行，前端不会再启动一个并行运行，而是调用：

```text
POST /dashboard/api/threads/{thread_id}/messages
```

前端代码在 `ui/src/features/agents/lib/provider/useSubmitAgentMessage.ts`。

后端会把消息放入线程队列。下一次模型调用前，`check_message_queue_before_model` 中间件会把队列里的新消息注入当前运行。

流程可以理解为：

```text
Agent 正在修复 bug
        |
用户补充：“顺便把测试补上”
        |
消息进入线程队列
        |
下一轮模型调用前注入
        |
Agent 继续处理补充要求
```

如果线程已经空闲，消息接口会返回 `409`，前端随后改用新的 `run.start` 启动一次运行。

## 八、输出如何回到前端

LangGraph Runtime 会持续发送 SSE 事件，内容可能包括：

- 模型文本
- 工具调用
- 工具返回结果
- 文件修改
- 子 Agent 状态
- 运行完成、失败或中断

后端的 `proxy_dashboard_thread_stream_events()` 会把 Runtime 的事件流继续转发给浏览器。

前端从 `StreamProvider` 读取：

```ts
stream.messages
stream.toolCalls
stream.subagents
```

`AgentThreadView.tsx` 再调用 `streamMessagesToUi()`，将底层事件转换成：

- 用户消息和 Agent 回复
- 工具执行卡片
- 子 Agent 活动卡片
- 代码变更和 diff 展示
- 运行状态和错误提示

## 九、PR 评审聊天使用另一张图

PR 详情页中的“和这个 PR 聊天”不是普通的 `agent` 图，而是单独的 `chat` 图：

```text
ReviewChat.tsx
    |
    v
/dashboard/api/reviews/{owner}/{repo}/{number}/chat
    |
    v
assistant_id = "chat"
    |
    v
agent.graphs.chat:traced_chat_agent
```

这个聊天 Agent 是无沙箱的 PR 问答 Agent。首次运行时，后端会把以下内容准备成上下文：

- PR diff
- 评审发现的问题
- PR 概览

相关代码：

- `ui/src/features/reviews/components/ReviewChat.tsx`
- `agent/dashboard/review_chat_api.py`
- `langgraph.json` 中的 `chat` 图入口

普通 `reviewer` 和 `analyzer` 通常不是由聊天框直接选择：

- `reviewer`：由 PR 事件或重新评审接口触发。
- `analyzer`：由评审风格分析任务或定时任务触发。
- `scheduler`：由自动化调度任务触发。

## 十、本地模式是例外

如果用户在前端选择 `This Mac`，流程不会经过云端 FastAPI 和 LangGraph Runtime，而是调用：

```ts
window.openSweDesktop.startAcpSession(...)
```

然后由桌面端 ACP 会话在本机项目目录中运行 Agent。

所以两种模式要分清：

```text
云端模式：React → FastAPI → LangGraph Runtime → Agent 图 → 云端沙箱

本地模式：React → Open SWE Desktop ACP → 本机项目目录
```

## 最后总结

```text
普通网页对话：assistant_id="agent"
PR 页面聊天：assistant_id="chat"
PR 自动评审：reviewer 图，由事件或后台接口触发
评审风格分析：analyzer 图，由任务或 cron 触发
本机执行：桌面 ACP，不走云端 dashboard API
```

最关键的一点是：前端只负责提交消息和显示流式事件，真正决定调用哪张图、使用哪个模型、连接哪个仓库以及如何操作沙箱的逻辑，都在 FastAPI 和 LangGraph Runtime 后端。

# 第 2-2 章：Dashboard 命令代理

## 学习目标

读完本章后，你应能从源码解释：浏览器的 `run.start` 为什么不是直接到 Agent；Dashboard 代理如何创建线程、限制权限、重建配置、转发 LangGraph 命令，并把运行状态反馈给 UI。

## 先定边界：它不是普通反向代理

代理核心在 `agent/dashboard/thread_api.py`：

```python
async def proxy_dashboard_thread_commands(...):
```

它不只是把字节原样转发给 `LANGGRAPH_URL`。在转发前，它需要理解其中的 `method`；在转发后，它还会读取响应中的 `run_id` 并更新线程 metadata。因此它是一个**协议感知的安全边界**。

Dashboard 路由前缀为 `/dashboard/api`，并统一带有 `require_same_origin_for_mutations` 依赖。具体命令入口是：

```text
POST /dashboard/api/threads/{thread_id}/commands
  -> api_thread_commands
  -> proxy_dashboard_thread_commands
  -> POST {LANGGRAPH_URL}/threads/{thread_id}/commands
```

其中 `api_thread_commands` 很薄：读取原始 body，传入已验证 session 的 GitHub 登录名和 email，并将下游响应状态码、body、content type 原样返回。

![Dashboard 命令代理与 SSE 时序](architecture/premium/png/02-dashboard-run-sequence.png)

图中的 `thread_api.py` 位于 FastAPI 和 LangGraph Runtime 之间：它会改写可信配置、更新 `latest_run_id`，但不会把最终 Agent 输出重新包装成另一套消息协议。

源码：

- [FastAPI 路由](../../agent/dashboard/routes.py)
- [命令代理实现](../../agent/dashboard/thread_api.py)

## 一、commands 与 stream/events 是两条不同的路

| 路径 | 用途 | Dashboard 做什么 |
| --- | --- | --- |
| `POST /threads/{id}/commands` | `run.start`、取消、交互输入等命令 | 鉴权、线程创建、补全 `run.start`、转发、回写 `run_id` |
| `POST /threads/{id}/stream/events` | 获取运行的 SSE 事件 | 在 SSE 开始前完成 content type 和可读权限检查，然后持续转发字节流 |

不要把两者混成“发消息接口”。`commands` 触发或控制运行；`stream/events` 让浏览器看到运行过程。对于 SSE，`proxy_dashboard_thread_stream_events` 故意把权限校验放在 generator 创建前，否则 HTTP 已经开始流式返回时，401/415 会变成难以处理的 SSE 片段。

## 二、函数入口：先拒绝无效协议

`proxy_dashboard_thread_commands` 的前几步是：

```python
_require_json_content_type(content_type)
parsed = json.loads(body)
if not isinstance(parsed, dict):
    raise HTTPException(400, "command body must be a JSON object")
```

这里没有用 Pydantic 为整个命令建模，因为 body 是 LangGraph SDK 的命令协议，项目只需要理解并改写 `run.start` 的局部字段。

| 条件 | 行为 | 原因 |
| --- | --- | --- |
| Content-Type 不是 JSON | `415` | 避免对任意请求体做 JSON 解析 |
| JSON 解析失败或根节点不是对象 | `400` | 命令必须有 `method` 和可选 `params` |
| 不存在的线程 + 非 `run.start` | `404` | 只有启动新运行可以惰性创建线程 |

## 三、线程存在性决定授权分支

代理先通过 `langgraph_client().threads.get(thread_id)` 尝试读取线程。

```text
线程不存在
  method == run.start ? creating=True，进入首次启动路径
  其他 method       ? 404

线程存在
  method == run.start ? _assert_thread_readable(metadata)
  其他 method       ? _assert_thread_owner(metadata, login, email)
```

为什么 `run.start` 只要求“可读”，而其他写命令要求“所有者”？因为团队成员可以在一个可见线程中发起带归属标记的新 Agent 工作；但 `input.respond`、取消等命令携带的输入无法像 `run.start` 一样由代理添加可验证的归属信息，因此保持 owner-only。

对于已存在线程，代理还合并两个繁忙信号：

```python
thread_busy = _thread_is_busy(thread) or metadata_run_status in {"pending", "running"}
```

metadata 是缓存索引，线程实时 `status` 是平台状态；二者都看能减少 UI 刚启动或刚取消时的竞态误判。

## 四、`run.start` 改写前后的具体例子

假设浏览器提交了下面的命令。`github_login`、`source`、`user_email` 是恶意客户端可伪造的字段，故意放在例子里观察代理如何处理：

```json
{
  "method": "run.start",
  "params": {
    "input": {
      "messages": [
        {"type": "human", "content": "检查测试为什么失败"}
      ]
    },
    "config": {
      "configurable": {
        "github_login": "attacker",
        "source": "github",
        "user_email": "attacker@example.com",
        "repo": {"owner": "evil", "name": "repo"},
        "agent_model_id": "openai:gpt-5.6-terra",
        "agent_effort": "medium",
        "plan_mode": true
      }
    }
  }
}
```

若线程 `t-123` 已存在，metadata 中记录的是：

```json
{
  "source": "dashboard",
  "github_login": "real-user",
  "repo_owner": "trusted-org",
  "repo_name": "service-api"
}
```

`_enrich_run_start_command` 的结果不是在原 `configurable` 上打补丁，而是调用 `_build_dashboard_configurable` 重建它：

```json
{
  "thread_id": "t-123",
  "source": "dashboard",
  "github_login": "real-user",
  "user_email": "real-user@example.com",
  "repo": {"owner": "trusted-org", "name": "service-api"},
  "agent_model_id": "openai:gpt-5.6-terra",
  "agent_effort": "medium",
  "plan_mode": true
}
```

因此客户端选择的模型/effort 只在通过 `_normalize_model_choice` 后成为 override；身份、来源和已有线程的仓库都来自服务端。这个规则比“过滤几个危险字段”更可靠：代理构造新对象，而不是试图清理不可信旧对象。

## 五、首次 `run.start` 与后续 `run.start`

### 首次启动

当 `thread_id` 尚不存在，`creating=True`。`_enrich_run_start_command` 调用 `_create_dashboard_thread_record`，它会：

1. 读取当前登录用户的 profile。
2. 解析并校验模型、effort、图像能力和仓库。
3. 建立 `source`、`github_login`、标题、默认分支、模型、计划模式等 metadata。
4. `threads.create(..., if_exists="do_nothing")`，随后 `threads.update(...)` 写入完整 metadata。
5. 用新 metadata 重建运行期 `configurable`。

这解释了一个常见现象：前端可以先由 SDK 生成 thread ID，再立即 `run.start`，而不会先收到 `GET /state` 的 404。

### 已有线程的再次启动

已有线程时，代理会：

1. 检查 `thread_busy`；忙碌则返回 `409`，提示客户端走 `/messages` 队列路径。
2. 校验图像是否适配已选模型；必要时选择视觉 fallback。
3. 为非 owner 的发起者加 `@login:` 前缀，保留消息可追溯性。
4. 若线程原本来自 Slack，在内容前加入 Web handoff 指令。
5. 更新 plan mode、模型、effort，以及重新打开已 resolved 的线程。

## 六、代理最终补了哪些字段

在发送给 LangGraph 之前，`_enrich_run_start_command` 写入：

```python
params["assistant_id"] = "agent"
params.setdefault("stream_mode", list(_DASHBOARD_STREAM_MODES))
params.setdefault("stream_resumable", True)
params["config"] = {**client_config, "configurable": merged_configurable}
params["metadata"] = run_metadata
```

含义：

- `assistant_id="agent"` 强制选择 `langgraph.json` 中注册的主图。
- `stream_mode` 默认包含 `messages`、`tools`、`checkpoints`、`events` 等 UI 所需通道；客户端若已经给出该字段，`setdefault` 保留其值。
- `stream_resumable=True` 默认让浏览器可以重新接入事件流。
- 外层 `client_config` 可保留 SDK 的非 `configurable` 参数，但内部 `configurable` 用服务端重建结果完全替换。
- `run_metadata` 合并部署版本信息；它不等于 thread metadata。

## 七、真正的 HTTP 转发和回写

代理构造 `outgoing = json.dumps(enriched).encode()` 后，使用 30 秒总超时、5 秒连接超时向以下地址 POST：

```text
{LANGGRAPH_URL}/threads/{thread_id}/commands
```

`_langgraph_proxy_headers` 同时转发 JSON Content-Type，并在环境中存在 LangSmith API key 时加入 `X-API-Key`。浏览器从不直接拿到该 key。

下游成功且 body 包含 `run_id` 时，代理更新：

```json
{
  "latest_run_id": "run-456",
  "latest_run_status": "pending",
  "updated_at_ms": 0
}
```

这份 metadata 供 Agents UI 显示运行状态和停止按钮。它是 UI 索引，不能替代 LangGraph 对 Run 状态的权威记录；因此列表加载时还会刷新最新 Run。

如果线程来自 Slack，成功启动已有线程后还会 best-effort 更新 Slack 中的 Web handoff 链接。该更新失败只记录日志，不能使一个已经创建成功的 Run 变成失败。

## 八、错误路径与客户端应该怎么理解

| 状态 | 触发点 | 前端正确动作 |
| --- | --- | --- |
| `400` | command body 不是 JSON 对象 | 修复 SDK/调用代码，不能重试同一 body |
| `401` | GitHub OAuth token 不可用 | 重新登录 GitHub |
| `404` | 缺失线程的非 `run.start`，或非 owner 的受限命令 | 不重试；检查线程和权限 |
| `409` | 同线程已有 pending/running Run | 调用 `/messages`，由队列追加追问 |
| `415` | 不是 JSON Content-Type | 修复请求头 |
| 下游 `4xx/5xx` | LangGraph 命令端点拒绝或不可用 | 保留响应信息，按下游原因处理 |

## 九、相关的源码测试

以下测试不调用模型、sandbox 或外部网络：

```bash
uv run pytest -vvv \
  tests/dashboard/test_dashboard_thread_api.py::test_proxy_commands_lazily_creates_missing_thread_only_for_run_start \
  tests/dashboard/test_dashboard_thread_api.py::test_proxy_commands_rejects_non_object_body \
  tests/dashboard/test_dashboard_thread_api.py::test_proxy_commands_non_run_start_by_non_owner_is_rejected \
  tests/dashboard/test_dashboard_thread_api.py::test_enrich_run_start_command_allowlists_client_configurable
```

它们分别固定住惰性创建、输入协议、owner-only 命令和不可信客户端配置不能越权这四项行为。

当前验证结果：4 项通过。

## 十、读源码的建议顺序

1. 从 `api_thread_commands` 读到 `proxy_dashboard_thread_commands`，确认 HTTP 边界。
2. 阅读 `_enrich_run_start_command`，理解首次和后续 Run 的差异。
3. 阅读 `_build_dashboard_configurable`，确认可信字段来源。
4. 回到 [第 2-1 章](02-1-main-agent-factory.md)，观察最终 `configurable` 怎样触发 `get_agent` 的执行分支。
5. 最后阅读 `useSubmitAgentMessage.ts`，理解为什么忙碌时不再发 `run.start`。

## 常见误区

1. 认为代理会信任前端传来的 `github_login` 或 `repo`。已有线程的这些字段必须来自 metadata。
2. 认为 `run.start` 与 `/messages` 等价。前者创建新 Run，后者把追问并入当前 Run。
3. 认为写入 `latest_run_id` 就是启动 Run。启动由 LangGraph 完成，metadata 只是 UI 的快速索引。
4. 把 SSE 流代理当成命令代理的一部分。它们共享线程权限，但错误处理和生命周期完全不同。

## 本章边界

本章说明 Dashboard 与 LangGraph 之间的协议边界；没有深入 LangGraph SDK 的命令规范和 SSE event schema。后续研究 UI 时，可继续追踪 `@langchain/react` 的 `StreamProvider` 如何消费这些事件。

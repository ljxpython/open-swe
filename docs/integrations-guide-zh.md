# `agent/integrations` 通俗说明

## 先说结论

`agent/integrations/` 是 Open SWE 的“外部服务适配层”。它把第三方平台的 SDK、HTTP API 和 MCP 服务包装成项目内部能直接使用的两类东西：

1. **沙箱 backend**：给 Agent 一个可以执行命令、读写文件的远程或本地工作环境。
2. **Agent tools**：给模型增加查询 Datadog、LangSmith、Currents、Notion、Corridor 或浏览器的能力。

这里的代码不负责决定“什么时候调用哪个工具”。主 Agent 的组装逻辑在 [`agent/server.py`](../agent/server.py)，它根据环境变量、团队凭据、用户凭据和权限，动态加载这些集成。没有配置某个服务时，通常返回空工具列表，Agent 仍然可以启动。

## 和 LangGraph、Deep Agents、LangChain 的关系

这个目录正好能看出三层框架是怎么配合的：

| 层 | 在项目里负责什么 | `agent/integrations` 里的例子 |
|---|---|---|
| LangChain | 定义模型、消息、工具和 Provider 适配接口。 | `BaseTool`、`StructuredTool`、`langchain_daytona`、`langchain_e2b`。 |
| LangGraph | 负责线程、Run、状态、checkpoint 和图执行。 | `get_config()` 读取当前 thread；`server.py` 通过 LangGraph 运行整张 Agent 图。 |
| Deep Agents | 在 LangGraph/LangChain 之上提供 Agent 成品能力：文件工具、子 Agent、backend 和上下文管理。 | `LangSmithSandbox`、`SandboxBackendProtocol`，以及最终传给 `create_deep_agent()` 的工具。 |

所以这里的集成文件并不是“只给 Deep Agents 用的插件”。它们先把第三方服务变成 LangChain 能识别的工具或 backend，再由 Deep Agents 装进 Agent，最后由 LangGraph 负责运行和持久化。

可以记成一句话：

```text
第三方 SDK/API -> LangChain 工具/backend -> Deep Agents Agent 能力 -> LangGraph Run
```

```text
agent/server.py / agent/utils/sandbox.py
                 |
                 v
          agent/integrations
          /                 \
    沙箱供应商适配器       外部工具适配器
          |                 |
   LangSmith/Daytona/...  MCP/API/Stagehand
          |                 |
          v                 v
    DeepAgents backend    LangChain tools
```

## 文件总览

| 文件 | 它是干什么的 | 怎么被使用 |
|---|---|---|
| [`__init__.py`](../agent/integrations/__init__.py) | 集成包的公共出口，导出 `LangSmithProvider` 和 `LangSmithSandbox`。 | 其他代码需要引用 LangSmith 沙箱类型时从 `agent.integrations` 导入。 |
| [`langsmith.py`](../agent/integrations/langsmith.py) | LangSmith 云沙箱适配器，也是默认沙箱实现。负责创建/恢复沙箱、启动未运行的沙箱、配置容量和生命周期、给 GitHub 代理注入 Token，并给执行增加超时保护。 | `agent/utils/sandbox.py` 在 `SANDBOX_TYPE=langsmith` 时动态调用 `create_langsmith_sandbox()`；启动时调用 `LangSmithProvider.validate_startup_config()`。 |
| [`daytona.py`](../agent/integrations/daytona.py) | Daytona 沙箱适配器。 | `SANDBOX_TYPE=daytona` 时由 `create_sandbox()` 调用 `create_daytona_sandbox()`。 |
| [`modal.py`](../agent/integrations/modal.py) | Modal 沙箱适配器。 | `SANDBOX_TYPE=modal` 时创建或按 ID 恢复 Modal sandbox。 |
| [`runloop.py`](../agent/integrations/runloop.py) | Runloop devbox 适配器。 | `SANDBOX_TYPE=runloop` 时创建或恢复 devbox，需要 `RUNLOOP_API_KEY`。 |
| [`e2b.py`](../agent/integrations/e2b.py) | E2B 沙箱适配器。 | `SANDBOX_TYPE=e2b` 时创建或连接 E2B sandbox，可选指定 `E2B_TEMPLATE`。 |
| [`local.py`](../agent/integrations/local.py) | 本机 shell 适配器。 | `SANDBOX_TYPE=local` 时把当前机器目录包装成 sandbox；只建议本地开发和人工确认场景。 |
| [`datadog_mcp.py`](../agent/integrations/datadog_mcp.py) | 连接 Datadog 托管 MCP，加载日志、指标、trace、监控、事件等查询工具。 | `server.py` 只给有权限的用户加载；团队未配置 Datadog 或 MCP 失败时返回 `[]`。 |
| [`langsmith_tools.py`](../agent/integrations/langsmith_tools.py) | 连接 LangSmith API，提供只读的 trace 查询和运行列表工具。 | `server.py` 给管理员/允许组织成员加载；Reviewer 的 trace 上下文也会复用其中的 client。 |
| [`corridor_mcp.py`](../agent/integrations/corridor_mcp.py) | 连接 Corridor 托管 MCP，目前只暴露允许列表里的 `analyzePlan` 工具。 | 从环境变量读取 URL/Token，配置正确才加载，并且会过滤 MCP 返回的其他工具。 |
| [`currents_tools.py`](../agent/integrations/currents_tools.py) | 连接 Currents.dev，只读查询端到端测试项目、run 和 instance。 | 按用户 login 从加密凭据存储取 API Key；用户未连接 Currents 时不加载。 |
| [`notion_mcp.py`](../agent/integrations/notion_mcp.py) | 连接 Notion 托管 MCP，给用户提供 Notion 工具。 | 按用户 OAuth Token 加载；每次真正调用工具时会重新取 Token 和 MCP 工具，能应对 Token 轮换。 |
| [`stagehand_browser.py`](../agent/integrations/stagehand_browser.py) | 浏览器自动化适配器，支持本地 Chromium 或 Browserbase 云浏览器。 | `server.py` 组装 Agent 时调用 `load_browser_tools()`；未配置必要凭据时不启用。每个 Agent thread 复用一个浏览器 session。 |

## 一、沙箱供应商适配器

### 统一选择入口

真正选择供应商的代码在 [`agent/utils/sandbox.py`](../agent/utils/sandbox.py)：

```python
SANDBOX_TYPE=langsmith  # 默认
```

它维护一张映射表：

```text
langsmith -> agent.integrations.langsmith:create_langsmith_sandbox
daytona   -> agent.integrations.daytona:create_daytona_sandbox
modal     -> agent.integrations.modal:create_modal_sandbox
runloop   -> agent.integrations.runloop:create_runloop_sandbox
e2b       -> agent.integrations.e2b:create_e2b_sandbox
local     -> agent.integrations.local:create_local_sandbox
```

调用过程是：

```text
server.ensure_sandbox_for_thread()
  -> utils.sandbox.create_sandbox(sandbox_id)
  -> 读取 SANDBOX_TYPE
  -> 动态导入 integrations/<provider>.py
  -> create_*_sandbox()
  -> 返回 SandboxBackendProtocol
```

所有供应商最终都要返回 `SandboxBackendProtocol` 能理解的 backend，因此上层的 `read_file`、`write_file`、`execute` 等工具不用关心底下到底是 LangSmith 还是 E2B。

### 各沙箱文件的区别

| 供应商 | 创建/恢复方式 | 关键配置 | 通俗理解 |
|---|---|---|---|
| LangSmith | 通过 `AsyncSandboxClient` 创建或按 ID 获取；必要时启动休眠中的沙箱。 | `LANGSMITH_API_KEY`、`DEFAULT_SANDBOX_SNAPSHOT_ID`、CPU/内存/磁盘/TTL 等。 | Open SWE 的默认云工作区，功能最完整，能持久保存线程工作目录。 |
| Daytona | `Daytona.create()` 或 `Daytona.get()`，再包装成 `DaytonaSandbox`。 | `DAYTONA_API_KEY`、`DAYTONA_SANDBOX_SNAPSHOT`。 | Daytona 提供的远程开发环境。 |
| Modal | `modal.Sandbox.create()` 或 `modal.Sandbox.from_id()`。 | `MODAL_APP_NAME`。 | Modal 应用里的临时计算环境。 |
| Runloop | `Client.devboxes.create()` 或 `devboxes.retrieve()`。 | `RUNLOOP_API_KEY`。 | Runloop 的远程 devbox。 |
| E2B | `Sandbox.create()` 或 `Sandbox.connect()`，可指定模板。 | `E2B_API_KEY`、`E2B_TEMPLATE`。 | E2B 提供的隔离代码执行环境。 |
| Local | `LocalShellBackend(root_dir=...)`，直接操作当前机器。 | `LOCAL_SANDBOX_ROOT_DIR`。 | 本地开发用的“假沙箱”，隔离性最弱，命令实际在宿主机执行。 |

### 为什么 LangSmith 文件特别大

LangSmith 是默认供应商，所以 [`langsmith.py`](../agent/integrations/langsmith.py) 不只是几行创建代码，还处理了这些现实问题：

- 远程沙箱创建/重连是异步的；
- 沙箱可能处于 stopped 状态，需要先启动并等待 ready；
- 创建可能遇到限流或暂时不可用，需要重试；
- 单次 `execute` 不能无限等待，需要超时和 WebSocket fallback；
- 每个线程的沙箱需要写回 LangGraph thread metadata；
- GitHub Token 要通过沙箱代理注入，沙箱里不保存真实 Token；
- 沙箱有 idle TTL 和 stop 后删除时间，但代码不主动 delete，回收由平台负责。

因此它既是供应商适配器，也是默认沙箱的可靠性保护层。

### Local 沙箱要特别小心

`local.py` 明确写了：它没有真正的隔离，命令会直接在宿主机运行。适合本地调试和人工确认，不适合生产或把不可信请求直接放进来。生产环境应使用远程隔离供应商。

## 二、服务端外部工具适配器

这些文件不是沙箱。它们通常在 LangGraph 服务进程里运行，凭据也留在服务端，不会写入 Agent 沙箱。模型看到的是一个普通 LangChain tool，例如“查询某个测试 run”或“读取某条 trace”。

### Datadog：查线上观测数据

[`datadog_mcp.py`](../agent/integrations/datadog_mcp.py) 通过 Datadog 托管 MCP 加载工具，默认使用 `core` toolset，覆盖日志、指标、trace、dashboard、monitor、incident、host、service、event 等查询。

调用链：

```text
server.py
  -> _load_observability_tools(authorized=True)
  -> load_datadog_tools()
  -> dashboard.team_credentials.get_datadog_credentials()
  -> MultiServerMCPClient
  -> LangChain tools
  -> Agent 查询 Datadog
```

只有通过 observability 权限检查的用户才会获得这些工具。没有团队凭据或连接失败时返回空列表，主 Agent 不会因此启动失败。

### LangSmith Tools：查 Agent 自己的运行记录

[`langsmith_tools.py`](../agent/integrations/langsmith_tools.py) 只读访问 LangSmith，提供两类工具：

- 按 run ID 查看单条 trace，可选择加载子 runs；
- 按项目列出最近运行，可按状态过滤。

它和 [`agent/utils/langsmith.py`](../agent/utils/langsmith.py) 不一样：

- `integrations/langsmith_tools.py`：给 Agent 调用的 LangSmith 查询工具；
- `utils/langsmith.py`：生成 trace 链接、写 feedback 的公共工具；
- `integrations/langsmith.py`：LangSmith 沙箱供应商。

三者都叫 LangSmith，但职责完全不同。

### Corridor：只开放一个计划分析工具

[`corridor_mcp.py`](../agent/integrations/corridor_mcp.py) 从环境变量读取 MCP URL 和 Token，默认地址是 Corridor 官方地址。它不会把 MCP 返回的所有工具原样暴露，而是只保留 `analyzePlan`。

```text
环境变量 URL/Token
  -> 校验必须是 https://app.corridor.dev/api/mcp
  -> 连接 MCP
  -> 只保留 analyzePlan
  -> server.py 缓存 10 分钟
  -> Agent 使用
```

这是一个典型的“外部服务能力很大，但项目只开放需要的那一小部分”的安全做法。

### Currents：调查 E2E 测试失败

[`currents_tools.py`](../agent/integrations/currents_tools.py) 是只读工具，提供：

- 列出项目；
- 查看单个测试 run；
- 按项目、CI build、分支寻找最近 run；
- 列出项目 runs；
- 查看单个 spec instance 的错误和尝试记录。

调用时 API Key 通过 `get_currents_api_key(login)` 从用户凭据存储读取，服务端直接请求 Currents REST API。沙箱里没有 Currents Key，所以 Agent 即使执行代码，也读不到这份密钥。

### Notion：按用户 OAuth 使用 Notion

[`notion_mcp.py`](../agent/integrations/notion_mcp.py) 连接 Notion MCP，并按照用户 login 读取 Notion access token。它有一个很重要的设计：

1. 加载工具列表时先取一次 Token；
2. 真正调用某个工具时，再次取最新 Token、重新连接 MCP；
3. 用 `_RefreshingNotionMCPTool` 保留原工具名称和参数，但把调用转发给新工具。

这样用户重新授权或 Token 轮换后，不需要重启整个 Agent 服务。没有 Notion 授权则返回空工具列表。

### Stagehand：浏览器自动化

[`stagehand_browser.py`](../agent/integrations/stagehand_browser.py) 不是 MCP，而是直接使用 Stagehand Python SDK，提供浏览器的导航、操作、观察、提取和关闭等能力。

支持两种模式：

| 模式 | 浏览器在哪里 | 必要配置 |
|---|---|---|
| `LOCAL`（默认） | 当前机器的 Chromium | `STAGEHAND_MODEL_API_KEY` 或 fallback 模型 Key；可设置 Chrome 路径。 |
| `BROWSERBASE` | Browserbase 云端 | `BROWSERBASE_API_KEY`，可选 project ID。 |

它会做几件额外的事：

- 用 LangGraph `thread_id` 为每个 Agent 线程复用同一个浏览器 session；
- 所有导航和页面请求都经过 `utils/url_safety.py`，拦截危险 URL/SSRF；
- 发现被拦截的请求后记录原因，并让后续操作得到可读错误；
- 没有可用配置时 `load_browser_tools()` 返回空列表；
- Agent 使用完应调用 `browser_close()`。

## 三、主 Agent 如何加载这些工具

`agent/server.py` 在组装主 Agent 时，不是无脑把所有工具塞进去，而是按权限和凭据决定：

```text
get_agent(config)
  |
  +--> observability 权限通过？
  |       +--> Datadog tools
  |       +--> LangSmith tools
  |
  +--> Corridor 环境配置正确？
  |       +--> Corridor tools
  |
  +--> 浏览器配置正确？
  |       +--> Stagehand tools
  |
  +--> profile_login 存在？
          +--> Currents tools（用户凭据）
          +--> Notion tools（用户 OAuth）
  |
  +--> 静态工具 + 动态工具
  |       -> create_deep_agent(...)
```

工具加载通常套了 `ttl_cache`：

- Datadog、LangSmith、Corridor 缓存约 10 分钟；
- Currents、Notion 按用户缓存约 5 分钟；
- 加载超时或失败返回空列表，不阻塞主 Agent 启动。

这意味着“服务没接入某个平台”不是启动错误，而是 Agent 看不到对应工具。

## 四、权限和凭据怎么走

### 团队级凭据

Datadog、LangSmith 通常来自 Dashboard 的团队凭据存储，服务端读取并在连接 MCP/API 时加 header。Agent 沙箱不接触这些 Key。

### 用户级凭据

Currents、Notion 使用当前用户 login 读取个人凭据或 OAuth Token。这样不同用户看到的是自己的外部数据，而不是共享一把密钥。

### 环境级凭据

Corridor、沙箱供应商和部分浏览器配置来自环境变量。部署时由服务端注入，不应该把 Token 写进仓库或提示词。

### 权限门禁

`server.py` 会先判断用户是否有 observability 权限或组织成员资格，再加载 Datadog/LangSmith。工具返回了并不代表模型可以绕过其他业务权限；它只是拿到了当前运行允许使用的工具集合。

## 五、完整示例：Agent 调查一次 CI/E2E 失败

```text
用户通过 Slack/GitHub 请求修复失败测试
        |
        v
server.get_agent(config)
  -> 检查用户身份和权限
  -> load_currents_tools(profile_login)
  -> load_datadog_tools()（若有 observability 权限）
  -> load_browser_tools()（若已配置）
  -> create_deep_agent(...)
        |
        v
模型调用 currents_find_run()
  -> 服务端带用户 Currents Key 请求 API
  -> 返回测试 run、截图/错误摘要
        |
        v
模型调用 browser_navigate()
  -> Stagehand 按 thread 复用浏览器
  -> url_safety 检查目标 URL
        |
        v
模型在沙箱里修改代码、运行测试
  -> runtime/utils 选择并连接 SANDBOX_TYPE 对应的 backend
```

注意：Currents/Datadog/Notion/Stagehand 是“服务端工具”，而 `execute`、`read_file`、`write_file` 是“沙箱工具”。前者查询外部系统，后者操作 Agent 工作区。

## 六、最容易混淆的区别

| 容易混淆的东西 | 实际区别 |
|---|---|
| `integrations/langsmith.py` vs `integrations/langsmith_tools.py` | 前者创建 LangSmith 沙箱；后者给 Agent 查询 LangSmith trace。 |
| `integrations/*_sandbox.py` 不存在 vs `utils/sandbox.py` | 沙箱适配器分散在 `daytona.py`、`modal.py` 等文件；统一选择逻辑在 `utils/sandbox.py`。 |
| MCP 工具 vs 沙箱 | MCP/API 工具通常在 LangGraph 服务进程发网络请求；沙箱提供文件系统和命令执行环境。 |
| `load_*_tools()` 返回空列表 | 表示没有凭据、未授权或外部服务暂时不可用，不代表整个 Agent 启动失败。 |
| Local sandbox vs 远程 sandbox | Local 直接在宿主机执行命令，没有真正隔离，只适合开发。 |
| Stagehand session vs sandbox | 浏览器 session 是按线程保存的网页会话；sandbox 是代码工作目录和命令执行环境，两者不是同一个东西。 |

## 七、推荐阅读顺序

1. [`agent/utils/sandbox.py`](../agent/utils/sandbox.py)：先看 `SANDBOX_TYPE` 如何选择供应商。
2. [`agent/integrations/langsmith.py`](../agent/integrations/langsmith.py)：理解默认沙箱的完整生命周期。
3. [`agent/server.py`](../agent/server.py)：看动态工具何时、按什么权限加载。
4. [`agent/integrations/*_mcp.py`](../agent/integrations/corridor_mcp.py)：理解 MCP 工具的凭据、超时和失败降级。
5. [`agent/integrations/currents_tools.py`](../agent/integrations/currents_tools.py)、[`notion_mcp.py`](../agent/integrations/notion_mcp.py)：对比用户级凭据工具。
6. [`agent/integrations/stagehand_browser.py`](../agent/integrations/stagehand_browser.py)：最后看浏览器 session 和 URL 安全。

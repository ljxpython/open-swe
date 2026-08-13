# 定制指南

Open SWE 设计为可被 fork 并按组织需求定制。核心 Agent 集中在 `agent/server.py` 的 `get_agent()` 中组装；你可以在那里替换沙箱、模型、工具和触发器。

```python
# agent/server.py - 关键部分
model_id = os.environ.get("LLM_MODEL_ID", DEFAULT_LLM_MODEL_ID)
model_kwargs = {"max_tokens": DEFAULT_LLM_MAX_TOKENS}
if model_id == DEFAULT_LLM_MODEL_ID:
    model_kwargs["reasoning"] = DEFAULT_LLM_REASONING

return create_deep_agent(
    model=make_model(model_id, **model_kwargs),
    system_prompt=construct_system_prompt(...),
    tools=[http_request, fetch_url, linear_comment, slack_thread_reply],
    backend=sandbox_backend,
    middleware=[
        ToolErrorMiddleware(),
        check_message_queue_before_model,
        ensure_no_empty_msg,
        notify_step_limit_reached,
    ],
)
```

---

## 1. 沙箱

默认情况下，Open SWE 会在 [LangSmith 云沙箱](https://docs.smith.langchain.com/)中执行每项任务。这是一个隔离的 Linux 环境，Agent 会在其中克隆仓库并运行命令。沙箱的创建与连接由 `agent/integrations/langsmith.py` 处理。

### 使用自定义沙箱快照

在 LangSmith 中从 Docker 镜像构建快照（通过 UI 或 `SandboxClient.create_snapshot`），然后将 Open SWE 指向其 UUID：

```bash
DEFAULT_SANDBOX_SNAPSHOT_ID="<snapshot-uuid>"                      # 必填
DEFAULT_SANDBOX_SNAPSHOT_FS_CAPACITY_BYTES="137438953472"          # 可选，默认 128 GiB
DEFAULT_SANDBOX_VCPUS="4"                                          # 可选，默认 4
DEFAULT_SANDBOX_MEM_BYTES="17179869184"                            # 可选，默认 16 GiB
DEFAULT_SANDBOX_IDLE_TTL_SECONDS="7200"                            # 可选，默认 7200（2 小时）；0 表示禁用
DEFAULT_SANDBOX_DELETE_AFTER_STOP_SECONDS="2592000"                # 可选，默认 2592000（30 天）；0 表示禁用
REPO_SNAPSHOT_BASE_IMAGE="<registry>/<open-swe-sandbox-image>"      # 可选；管理员生成仓库快照模板时必填
```

这适合预装仓库依赖的语言、框架或内部工具，从而减少每次 Agent 运行的准备时间。默认快照包含 GitHub CLI；Agent 使用 `GH_TOKEN=dummy gh <command>` 调用它，并通过 LangSmith 代理取得真实凭据。

`REPO_SNAPSHOT_BASE_IMAGE` 应指向用于创建默认 Open SWE 沙箱快照的已发布 Docker 镜像，通常是本仓库 `Dockerfile.sandbox` 构建出的镜像。管理员的“Repository Snapshots”页面会以它为基础生成按仓库划分的 Dockerfile 模板。未配置时，模板生成会拒绝执行，避免建议一个缺少 Git、GitHub CLI、`sfw` 和其他必备工具的裸镜像。

对于 LangSmith 沙箱，Open SWE 会在每次创建或重新连接时配置两条 GitHub 代理规则：

- `github.com` / `*.github.com`：为 HTTPS Git 操作提供 Basic 认证；
- `api.github.com`：为 `gh` 和 REST API 操作提供 Bearer 认证。

代理 token 在运行时由 GitHub App 安装凭据签发。不要把 GitHub 访问 token 存为部署环境变量。

### 使用其他沙箱提供商

通过 `SANDBOX_TYPE` 环境变量切换提供商。每个提供商在 `agent/integrations/` 下都有对应集成文件，并在 `agent/utils/sandbox.py` 注册工厂函数：

| `SANDBOX_TYPE` | 集成文件 | 必需环境变量 |
|---|---|---|
| `langsmith`（默认） | `agent/integrations/langsmith.py` | `LANGSMITH_API_KEY_PROD`、`SANDBOX_TYPE="langsmith"` |
| `daytona` | `agent/integrations/daytona.py` | `DAYTONA_API_KEY`、`SANDBOX_TYPE="daytona"`、可选 `DAYTONA_SANDBOX_SNAPSHOT` |
| `runloop` | `agent/integrations/runloop.py` | `RUNLOOP_API_KEY`、`SANDBOX_TYPE="runloop"` |
| `e2b` | `agent/integrations/e2b.py` | `E2B_API_KEY`、`SANDBOX_TYPE="e2b"`、可选 `E2B_TEMPLATE` |
| `modal` | `agent/integrations/modal.py` | Modal 凭据、`SANDBOX_TYPE="modal"` |
| `local` | `agent/integrations/local.py` | 无（不隔离，仅限开发）、`SANDBOX_TYPE="local"` |

> **警告：**`local` 会直接在宿主机执行命令，不提供沙箱隔离。只应在本地开发、且有人参与监督时使用。

对于 `langsmith`，沙箱默认复用追踪使用的 LangSmith 凭据。若要让沙箱使用**另一个** LangSmith 工作区，设置 `SANDBOX_LANGSMITH_API_KEY`（依次回退到 `LANGSMITH_API_KEY` / `LANGSMITH_API_KEY_PROD`），也可设置 `SANDBOX_LANGSMITH_ENDPOINT`（回退到 `LANGSMITH_ENDPOINT`）。这些变量会作用于沙箱创建、连接、删除、GitHub 代理配置和仓库快照构建；`DEFAULT_SANDBOX_SNAPSHOT_ID` 必须存在于这些凭据指向的工作区。

### 新增沙箱提供商

1. 在 `agent/integrations/my_provider.py` 创建集成文件，提供以下签名的工厂函数：

```python
def create_my_provider_sandbox(sandbox_id: str | None = None):
    """创建或重新连接沙箱。

    Args:
        sandbox_id: 可选的已有沙箱 ID。为 None 时创建新沙箱。

    Returns:
        一个实现 SandboxBackendProtocol 的对象。
    """
    ...
```

2. 在 `agent/utils/sandbox.py` 的 `SANDBOX_FACTORIES` 中注册：

```python
SANDBOX_FACTORIES = {
    ...
    "my_provider": ("agent.integrations.my_provider", "create_my_provider_sandbox"),
}
```

工厂必须返回实现 deepagents `SandboxBackendProtocol` 的对象。可参考现有集成文件。

### 构建自定义沙箱提供商

若内置提供商均不适用，可以自行实现。Agent 接受任何实现 deepagents `SandboxBackendProtocol` 的后端。协议需要：

- **文件操作：**`ls()`、`read()`、`write()`、`edit()`、`glob()`、`grep()`；
- **Shell 执行：**`execute(command, timeout=None) -> ExecuteResponse`；
- **身份：**返回唯一沙箱标识符的 `id` 属性。

最简单的方法是继承 `deepagents.backends.sandbox` 中的 `BaseSandbox`。它通过 `execute()` 实现全部文件操作，因此只需实现 Shell 执行层：

```python
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import ExecuteResponse


class MySandbox(BaseSandbox):
    def __init__(self, connection):
        self._conn = connection

    @property
    def id(self) -> str:
        return self._conn.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        result = self._conn.run(command, timeout=timeout or 300)
        return ExecuteResponse(
            output=result.stdout + result.stderr,
            exit_code=result.exit_code,
            truncated=False,
        )
```

完整参考实现见 `deepagents.backends.LangSmithSandbox` 与 `agent/integrations/langsmith.py`。

---

## 2. 模型

模型在 `agent/server.py` 的 `get_agent()` 中配置。默认使用中等推理强度的 `openai:gpt-5.6-sol`，可通过 `LLM_MODEL_ID` 环境变量覆盖：

```bash
# 通过环境变量设置模型，格式为 provider:model
LLM_MODEL_ID="anthropic:claude-sonnet-5"
```

未设置 `LLM_MODEL_ID` 时使用默认模型 `openai:gpt-5.6-sol`。

`max_tokens` 是最大补全/输出 token 预算，不是模型总上下文窗口。对于 OpenAI 推理模型，该预算可能同时包含内部推理 token 与最终响应 token。

### 切换模型

使用 `provider:model` 格式：

```python
# Anthropic
model = make_model("anthropic:claude-sonnet-5", temperature=0, max_tokens=16_000)

# OpenAI（默认使用 Responses API）
model = make_model("openai:gpt-5.6-sol", max_tokens=128_000, reasoning={"effort": "medium"})

# Google
model = make_model("google_genai:gemini-2.5-pro", temperature=0, max_tokens=16_000)
```

`agent/utils/model.py` 中的 `make_model()` 封装了 `langchain.chat_models.init_chat_model`。它会为 OpenAI 自动启用 Responses API。若需要完全控制，可直接传入预配置模型实例：

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model_name="claude-sonnet-5", temperature=0, max_tokens=16_000)

return create_deep_agent(
    model=model,
    ...
)
```

### 按上下文使用不同模型

可按任务复杂度、仓库或触发来源路由到不同模型：

```python
async def get_agent(config: RunnableConfig) -> Pregel:
    source = config["configurable"].get("source")

    if source == "slack":
        # Slack 问答使用更快的模型
        model = make_model("anthropic:claude-sonnet-5", temperature=0, max_tokens=16_000)
    else:
        # Linear 发起的代码修改使用完整模型
        model = make_model("openai:gpt-5.6-sol", max_tokens=128_000, reasoning={"effort": "medium"})

    return create_deep_agent(model=model, ...)
```

### 通过 LangSmith LLM Gateway 路由

模型调用可以通过 [LangSmith LLM Gateway](https://docs.langchain.com/langsmith/llm-gateway)（私测）代理，而不是直连模型提供商。网关使用带 `gateway:invoke` 权限的 **LangSmith API key** 认证，并从工作区 Provider Secrets 解析真实提供商密钥，因此运行时不需要提供商 API key；它还提供统一的成本上限、PII/密钥脱敏与追踪。组织必须先启用网关并配置 Provider Secrets。

该路由默认关闭，可通过以下任一方式启用：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `LANGSMITH_GATEWAY_ENABLED` | `false` | 部署级网关路由默认值。 |
| `LANGSMITH_GATEWAY_API_KEY` | 未设置 | 可选的专用 Gateway LangSmith key。在 LangGraph Cloud 中平台提供的 `LANGSMITH_API_KEY` 没有 `gateway:invoke` 时优先使用；随后回退到 `LANGSMITH_API_KEY_PROD`、`LANGSMITH_API_KEY`。 |
| `LANGSMITH_GATEWAY_BASE_URL` | `https://gateway.smith.langchain.com` | 区域或自托管网关地址覆盖。 |
| `LANGSMITH_GATEWAY_OPENAI_USE_RESPONSES` | `true` | 通过网关使用 OpenAI Responses API。只有必须强制 Chat Completions 时设为 `false`。 |

管理面板的“Admin → LLM Gateway”提供按工作区保存的开关；设置后会覆盖 `LANGSMITH_GATEWAY_ENABLED` 的环境变量默认值，未设置时继承环境变量。

路由在 `make_model` 中集中应用：`agent/utils/model.py` 解析最终开关，`agent/utils/gateway.py` 负责 URL/密钥接线。**OpenAI、Anthropic、Fireworks 和 Google Gemini** 会通过网关路由；Google Vertex（服务账号认证）及其他提供商仍直接调用，并记录警告。

**注意 OpenAI 端点：**open-swe 默认使用 OpenAI Responses API，因为带函数工具的 OpenAI 推理模型会拒绝在 Chat Completions 中使用 `reasoning_effort`。直连 OpenAI 使用 `wss://` 基础 URL；通过网关时使用启用 Responses 的 HTTPS 网关 URL。只有确实要强制 Chat Completions 时才设置 `LANGSMITH_GATEWAY_OPENAI_USE_RESPONSES=false`；Anthropic 和 Fireworks 不受影响。

---

## 3. 工具

除 Deep Agents 内置的读写、编辑、删除、搜索、Shell 与子 Agent 工具外，Open SWE 默认还提供 `fetch_url`、`http_request`、`linear_comment`、`slack_thread_reply`。沙箱中的 GitHub 操作使用 `GH_TOKEN=dummy gh`，真实认证由代理负责。

### 新增、移除与条件化工具

在 `agent/tools/` 新建带类型标注和清晰 docstring 的函数，随后从 `agent/tools/__init__.py` 导出，并在 `agent/server.py` 的 `create_deep_agent(tools=[...])` 列表注册。docstring 会直接成为模型看到的工具说明，因此应准确描述权限、输入和副作用。

不需要 Slack、Linear 或网页抓取时，直接从该列表移除对应工具即可。也可按 `config["configurable"].get("source")` 构建不同工具列表，例如只向 Linear 任务提供 `linear_comment`、只向 Slack 任务提供 `slack_thread_reply`。

### 浏览器自动化

`browser` 子 Agent 通过 Stagehand + Browserbase 控制真实 Chromium，提供导航、交互、观察、抽取和关闭操作。需要登录、点击流程、复现 UI 问题或处理 JS 渲染页面时再委派给它；静态页面优先用 `fetch_url`。

相关实现位于 `agent/integrations/stagehand_browser.py`，并由 `agent/server.py` 的 `load_browser_tools()` 门控。每个 Agent 线程复用一个浏览器会话。常用变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `STAGEHAND_ENV` | `LOCAL` | `LOCAL` 在进程内启动 Chromium；`BROWSERBASE` 使用 Browserbase 云端浏览器。 |
| `STAGEHAND_MODEL_API_KEY` | 回退到 `MODEL_API_KEY`、`ANTHROPIC_API_KEY` | Stagehand 的 `act`、`observe`、`extract` 使用的模型密钥。 |
| `STAGEHAND_MODEL` | `anthropic/claude-sonnet-4-5` | Stagehand 使用的模型。 |
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | 无 | 使用 `BROWSERBASE` 时前者必填，后者可选。 |
| `STAGEHAND_LOCAL_CHROME_PATH` | Docker 中为 `/usr/bin/chromium` | `LOCAL` 模式的浏览器路径。 |
| `STAGEHAND_HEADLESS` | `true` | 是否无头运行本地浏览器。 |

`LOCAL` 模式要求沙箱镜像含 Chromium；`BROWSERBASE` 模式不需要镜像内浏览器。

---

## 4. 触发器

Open SWE 支持 Linear、Slack 和 GitHub 入口。应在对应 Webhook 路由边界保留签名校验、仓库/用户门禁、确定性线程 ID 和 `dispatch_agent_run` 调度契约。

不使用某来源时，不配置相应 Webhook 和环境变量即可；若要彻底移除代码，删除其路由与处理器，并同步删除工具和测试。

### 默认仓库与消息解析

没有明确仓库时可设置：

```bash
DEFAULT_REPO_OWNER="my-org"
DEFAULT_REPO_NAME="my-repo"
```

Slack 和 Linear 文本均可使用 `repo:owner/name`、`repo owner/name`、`repo:name` 或 `https://github.com/owner/name` 指定目标。`repo:name` 的组织名取 `DEFAULT_REPO_OWNER`。

Linear 映射位于 `agent/utils/linear_team_repo_map.py` 的 `LINEAR_TEAM_TO_REPO`，按团队、项目和默认仓库解析；评论里显式 `repo:owner/name` 优先。Slack 的仓库解析依次查看既有线程元数据、频道 topic/purpose、用户默认仓库、团队默认仓库、`SLACK_REPO_OWNER`/`SLACK_REPO_NAME`，再回退到默认仓库。读取频道描述需要 Slack 的 `channels:read`，私有频道还需 `groups:read`。

### 新增触发器

新增 Jira、Discord 或自定义 API 入口时：实现签名校验的路由；从事件中构建任务和仓库上下文；生成确定性线程 ID；通过 `dispatch_agent_run` 创建运行；必要时新增来源回复工具。`configurable` 至少应包含 `repo`（`{"owner": ..., "name": ...}`）、`source` 和触发用户身份信息。不要绕过共享调度器直接拼接运行状态。

---

## 5. 系统提示词

系统提示词在 `agent/prompt.py` 中由模块化片段组装：`WORKING_ENV_SECTION` 管理沙箱与路径；`TASK_EXECUTION_SECTION` 管理理解、实现、验证和提交流程；`CODING_STANDARDS_SECTION` 管理代码质量；`COMMIT_PR_SECTION` 管理提交/PR；`CODE_REVIEW_GUIDELINES_SECTION` 管理评审；`COMMUNICATION_SECTION` 管理回复格式。

### 默认提示词与 `AGENTS.md`

组织级默认指令使用 `agent/resources/default_prompt.md`；也可设置：

```bash
DEFAULT_PROMPT_PATH="/path/to/my-org-prompt.md"
```

该文件是普通 Markdown，会以“Custom Instructions”加入每次运行的系统提示词。加载顺序为：默认提示词 → 系统提示词片段 → 目标仓库的 `AGENTS.md`。文件缺失或为空会被静默跳过。

`default_prompt.md` 用于默认仓库和全组织规范；`AGENTS.md` 放在目标仓库根目录，用于仓库专属编码规范。后者会在沙箱中读取并追加到提示词，不需要改 Open SWE 源码。

---

## 6. 中间件

中间件围绕 Agent 循环运行。默认重要组件包括：`ToolErrorMiddleware`（格式化工具错误）、`check_message_queue_before_model`（注入运行中收到的后续消息）、`ensure_no_empty_msg`（模型未调用工具时让运行继续）和 `notify_step_limit_reached`（达到模型调用上限时通知 Slack）。

系统特意没有“运行结束后自动开 PR”的兜底中间件；Agent 本身负责提交、推送、创建/更新草稿 PR 并回复来源渠道。需要确定性收尾策略时，在 `get_agent()` 的中间件列表添加 `@after_agent` 钩子，例如触发 CI 检查。修改中间件顺序属于行为和安全变更，应配套针对性测试。更多装饰器见 [LangChain Middleware 文档](https://python.langchain.com/docs/concepts/agents/#middleware)。

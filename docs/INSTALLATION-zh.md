# 安装指南

本文说明如何端到端配置 Open SWE：本地开发、GitHub App、LangSmith、Webhook、Web 仪表盘和生产部署。

Open SWE 有两个可运行部分：

- **后端：**LangGraph 图与 FastAPI 应用 `agent.webapp:app`；`langgraph dev` 会同时提供它们。
- **仪表盘：**位于 `ui/` 的 TanStack Start + Vite 客户端，通过 `/dashboard/api/*` 调用后端。纯 Webhook 使用可以不部署它，但建议保留。

## 前置条件

- Python 3.11 - 3.13；
- [uv](https://docs.astral.sh/uv)；
- [LangGraph CLI](https://docs.langchain.com/langsmith/cli)；
- [ngrok](https://ngrok.com/)（本地接收公网 Webhook）；
- `pnpm`（本地运行仪表盘时需要）。Node 20+ 可用，`ui/pnpm-lock.yaml` 是规范锁文件。

## 1. 克隆并安装

```bash
git clone https://github.com/langchain-ai/open-swe.git
cd open-swe
uv venv
source .venv/bin/activate
uv sync --all-extras
```

## 2. 启动 ngrok

后续配置 Webhook 时需要公网地址，先启动：

```bash
ngrok http 2024 --url https://some-url-you-configure.ngrok.dev
```

不传 `--url` 也可以，但每次启动可能得到不同子域名，需要重新更新 GitHub、Slack 和 Linear 的 Webhook URL。保留该终端运行，并复制它提供的 HTTPS 地址。

## 3. 创建 GitHub App

Open SWE 通过 [GitHub App](https://docs.github.com/en/apps/creating-github-apps)克隆仓库、推送分支和创建 PR。

### 3a. 选择 OAuth Provider ID

选择一个用于连接 GitHub 与 LangSmith 的简短 ID，例如：

```text
your-org-github-oauth
```

后续在 GitHub 回调 URL 和 LangSmith OAuth Provider 中使用同一个值。

### 3b. 创建 App

进入 GitHub Settings → Developer settings → GitHub Apps → New GitHub App，填写：

- **App name：**`open-swe` 或组织名称；
- **Homepage URL：**任意有效 URL；
- **Callback URL：**每行一个，至少加入：
  - `https://smith.langchain.com/host-oauth-callback/<your-provider-id>`；
  - `http://localhost:2024/dashboard/api/auth/callback`；生产环境还应加入 `https://<your-dashboard-api-url>/dashboard/api/auth/callback`。
- 启用 **Request user authorization (OAuth) during installation**；
- **Webhook URL：**`https://<your-ngrok-url>/webhooks/github`；
- **Webhook secret：**生成并保存为 `GITHUB_WEBHOOK_SECRET`：

```bash
openssl rand -hex 32
```

仓库权限最少包括 Contents、Pull requests、Issues、Checks、Workflows 的读写，以及 Metadata 只读。需要 CI 自动修复时增加 Commit statuses、Check run、Check suite、Workflow run 事件；只读 Actions 权限可用于诊断 CI 日志。若使用 `ALLOWED_GITHUB_ORGS`，还需要组织 Members 只读权限。

订阅事件：`Issue comment`、`Pull request review`、`Pull request review comment`、`Check run`、`Check suite`、`Workflow run`；`Status` 是兼容旧状态 API 的可选项。

### 3c. 收集凭据

创建后保存以下值：

| GitHub App 信息 | 环境变量 |
|---|---|
| App ID | `GITHUB_APP_ID` |
| Private key PEM 内容 | `GITHUB_APP_PRIVATE_KEY` |
| Client ID | `GITHUB_APP_CLIENT_ID` |
| Client secret | `GITHUB_APP_CLIENT_SECRET` |
| 安装 URL 末尾的数字 | `GITHUB_APP_INSTALLATION_ID` |

`GITHUB_APP_CLIENT_ID` 与 `GITHUB_APP_CLIENT_SECRET` 用于仪表盘直接 GitHub 登录；它们和 LangSmith 代理的运行时 OAuth 是两条独立流程。

### 3d. 安装 App

在 App 设置页选择 Install App，选择组织/账号及授权仓库。安装页面 URL 末尾的数字就是 `GITHUB_APP_INSTALLATION_ID`。

## 4. 配置 LangSmith

LangSmith 用于追踪及默认的隔离云沙箱。

### 4a. API Key、项目与租户

创建 API key 并设置 `LANGSMITH_API_KEY_PROD`；从 LangSmith URL 获取租户 UUID，设置 `LANGSMITH_TENANT_ID_PROD`；从追踪项目页面获取项目 ID，设置 `LANGSMITH_TRACING_PROJECT_ID_PROD`。主 Agent 与评审器分别使用 `open-swe-agent`、`open-swe-review` 项目名；应提前创建它们。

### 4b. GitHub OAuth（可选但建议）

LangSmith Settings → OAuth Providers → Add Provider：使用第 3a 步的 Provider ID，填入 GitHub App Client ID/Secret；授权 URL 为 `https://github.com/login/oauth/authorize`，Token URL 为 `https://github.com/login/oauth/access_token`，不要启用 PKCE。将 Provider ID 写入 `GITHUB_OAUTH_PROVIDER_ID`。

启用后，提交和 PR 可使用触发用户的 GitHub 身份与权限；未启用时则使用 GitHub App bot 身份。

### 4c. 沙箱快照

沙箱应从预构建快照启动。根 `Dockerfile` 是 API 服务镜像；应从 `Dockerfile.sandbox` 构建沙箱镜像：

```bash
docker buildx build \
  -f Dockerfile.sandbox \
  --platform linux/amd64 \
  -t <your-docker-hub>/<name-of-your-image> \
  --push .
```

在 LangSmith UI 创建快照，或运行：

```bash
uv run python scripts/create_sandbox_snapshot.py \
  --name open-swe-gh-cli-amd64 \
  --image <your-docker-hub>/<name-of-your-image>
```

配置：

```bash
DEFAULT_SANDBOX_SNAPSHOT_ID="<snapshot-uuid>"
DEFAULT_SANDBOX_IDLE_TTL_SECONDS="7200"
DEFAULT_SANDBOX_DELETE_AFTER_STOP_SECONDS="2592000"
REPO_SNAPSHOT_BASE_IMAGE="<your-docker-hub>/<name-of-your-image>"
```

使用 `SANDBOX_TYPE=langsmith` 时，`DEFAULT_SANDBOX_SNAPSHOT_ID` 是必填项。

## 5. 配置触发器与访问控制

- GitHub Webhook 指向 `/webhooks/github`，并设置 `GITHUB_WEBHOOK_SECRET`；
- Linear（可选）和 Slack（可选）分别指向对应 `/webhooks/linear`、`/webhooks/slack` 路由，并使用各自签名密钥；
- 用 `ALLOWED_GITHUB_ORGS`、`ALLOWED_REPOS` 或仓库启用列表限制可触发范围；
- 远程评论一律是不可信输入，路由层必须保留签名校验和仓库门禁。

## 6. 配置环境变量

将密钥保存在未提交的 `.env` 或生产 Secret Manager 中。至少配置 GitHub App、LangSmith、模型提供商、`DASHBOARD_JWT_SECRET`、`TOKEN_ENCRYPTION_KEY` 和 Webhook 签名密钥。常用仪表盘配置：

```bash
DASHBOARD_API_BASE_URL="http://localhost:2024"
DASHBOARD_BASE_URL="http://localhost:3000"
DASHBOARD_JWT_SECRET="<openssl-rand-hex-32>"
DASHBOARD_ALLOWED_ORIGINS="http://localhost:3000"
CONFIGURED_ADMINS="alice,bob@my-org.com"
LANGGRAPH_URL="http://localhost:2024"
SANDBOX_TYPE="langsmith"
DEFAULT_SANDBOX_SNAPSHOT_ID="<snapshot-uuid>"
```

生产环境所有 URL 必须使用 HTTPS。前后端跨域时，`DASHBOARD_ALLOWED_ORIGINS` 必须明确列出前端来源；不能用 `*`。

## 7. 启动本地后端

```bash
make dev
# 或 uv run langgraph dev --no-browser
```

`langgraph dev` 会在 `http://localhost:2024` 同时提供 LangGraph 图和 `agent.webapp:app`：Webhook、仪表盘 API、计划审批和健康检查均在此服务上。

`make run` 只启动 `uvicorn agent.webapp:app --port 8000`，没有 LangGraph 运行时；仅适合调试 HTTP 路由，不适合作为完整本地开发入口。

## 8. 启动本地仪表盘

```bash
cd ui
pnpm install
cat > .env <<'EOF'
VITE_DASHBOARD_API_BASE_URL="http://localhost:2024"
EOF
pnpm run dev
```

浏览器访问 `http://localhost:3000`。UI 请求会携带 Cookie，因此后端必须允许 `http://localhost:3000` 的凭据型 CORS。

## 9. 验证

在已配置的 GitHub、Linear 或 Slack 中发送 `@openswe what files are in this repo?`。应能看到响应标记、LangSmith 追踪以及来源渠道中的 Agent 回复。仪表盘应可完成 GitHub 登录并显示个人设置；`CONFIGURED_ADMINS` 中的用户可访问管理页面。

## 10. 生产部署

生产环境将后端和仪表盘分开部署：

1. 准备托管 PostgreSQL 和 Redis。PostgreSQL 保存 LangGraph 线程、运行、检查点和 Store 记录；Redis 提供队列与跨进程协调。
2. 构建根 `Dockerfile` 的后端镜像，并部署为常驻服务，不要 scale-to-zero。
3. 通过私网或 API Gateway 将 HTTPS 流量转发到容器的 `8000` 端口。
4. 将密钥存入平台 Secret Manager；配置 webhook、OAuth 回调和公网域名。
5. 部署 Dashboard，并让 `/dashboard/api/*` 同源转发到后端。

示例：

```bash
docker build -t open-swe .

docker run \
  --env-file .env \
  -p 8123:8000 \
  -e DATABASE_URI="postgres://postgres:postgres@host.docker.internal:5432/postgres?sslmode=disable" \
  -e REDIS_URI="redis://host.docker.internal:6379" \
  -e LANGGRAPH_URL="https://<your-backend-url>" \
  -e DASHBOARD_API_BASE_URL="https://<your-dashboard-or-backend-url>" \
  open-swe
```

生产还需要 `LANGGRAPH_CLOUD_LICENSE_KEY`，并通常需要 `LANGSMITH_API_KEY`。示例的 `sslmode=disable` 仅用于本地网络示例；真实生产数据库必须启用 TLS。

不要将 `LANGGRAPH_AUTH_TYPE=noop` 暴露到公网。若 LangGraph 内置 API 可被访问，必须使用私网、API Gateway 或自定义 LangGraph 鉴权。

Dashboard 推荐部署到 Vercel：保持 `VITE_DASHBOARD_API_BASE_URL` 为空，让 `ui/vercel.json` 将 `/dashboard/api/*` 重写到后端；将 `DASHBOARD_API_BASE_URL` 和 GitHub OAuth 回调设置为 Dashboard 域名。也可以跨域直连，但需明确设置 `VITE_DASHBOARD_API_BASE_URL` 与 `DASHBOARD_ALLOWED_ORIGINS`。

## 常见故障

- **Webhook 未触发：**检查公网 URL、签名密钥、事件订阅、仓库门禁和入口日志。
- **Dashboard 登录失败：**检查 GitHub Client ID/Secret、回调 URL、`DASHBOARD_JWT_SECRET`、HTTPS Cookie 与允许来源。
- **UI 无法访问后端：**本地确认后端是 `make dev` 的 `:2024`，并检查 `VITE_DASHBOARD_API_BASE_URL`。
- **沙箱创建失败：**检查 LangSmith API key、配额、`DEFAULT_SANDBOX_SNAPSHOT_ID` 是否存在且为 `ready` 状态。

# Prompt Sidecar

独立于主会话的提示词润色面板。通过通用 Bridge Registry 接入多个 agent
框架，当前兼容 Codex 和 DeepSeek Harness（DSH）。

## 功能

- 从显式绑定会话读取最近若干轮用户和助手消息
- 去掉 Skill、system-reminder 和任务导演注入块
- 单独调用本地 OpenAI-compatible 模型代理
- 支持快速、结构化、Agent、系统提示词四种模式
- 支持恢复原文和复制结果
- 润色过程不写入主会话；用户确认后可通过支持发送的桥接投递结果

### 任务继续 / 下一步

点击“下一步 / 继续任务”后，Sidecar 会读取当前绑定会话的上下文，结合最近消息、
工具调用和验证结果，生成继续任务建议。建议会列出目标、已完成内容及证据、未完成事项、
阻塞、主要下一步、最多两个备选方案、范围外建议和完成条件。

上下文不足时，结果会列出缺少的信息；任务等待用户决定时，优先生成澄清问题。

生成的 task prompt 会放入可编辑的结果框，包含目标、当前状态、本轮范围、完成条件、
验证方式、限制、未解决问题和期望 Agent 返回的结果。用户确认后，可按目标 bridge
支持的 queue / steer 方式发送；Sidecar 不会自动发送，也不保存跨会话的任务状态。

润色使用 `POST /api/polish`，任务继续使用 `POST /api/next-step`，
确认后的发送使用 `POST /api/send`。读取上下文和发送都以显式绑定的
`source`、`session_id` 为目标。

## 项目目录

```text
prompt-sidecar/
├── backend/
│   ├── server.py                 # HTTP 服务、润色与任务继续 API
│   └── bridges/
│       ├── __init__.py
│       ├── contracts.py          # 桥接契约
│       └── registry.py           # Bridge Registry
├── frontend/
│   ├── index.html                # 页面入口
│   ├── app.js                    # 页面交互
│   ├── styles.css
│   ├── favicon.svg
│   └── prompt-flow.svg
├── config/
│   ├── sidecar.config.example.json
│   ├── sidecar.config.json        # 首次启动生成的本机配置，Git 忽略
│   └── launch-agent.plist.template
├── scripts/
│   ├── start.sh                  # macOS LaunchAgent 启动
│   ├── stop.sh                   # macOS LaunchAgent 停止
│   ├── generate_launch_agent.py   # 生成本机 plist
│   ├── check.sh                  # 完整本地检查
│   ├── secret-scan.sh
│   └── prompt_sidecar_mcp.py      # 只读 stdio MCP 服务
├── requirements/
│   └── test.txt
├── tests/
├── dsh-plugin/prompt-sidecar-bridge/
├── .gitignore
└── README.md
```

服务入口、页面资源、配置和脚本分别放在上述目录中。`config/sidecar.config.json`
保存本机桥接配置；已有文件会保留，新克隆首次启动时才从示例生成。

## 启动

运行服务需要 Python 3.11+。完整测试还需要 Node.js（包含 `node:test`）和 Bash；
推送前扫描需要 `gitleaks`。前端使用原生 JavaScript，无需 npm 安装或构建。
OpenClaw 桥额外需要提供 `websockets.sync` 的 `websockets` 包。

新克隆后，在项目根目录运行以下命令以前台方式启动：

```bash
python3 backend/server.py
```

打开：`http://127.0.0.1:9876`

### HTTPS Agent 侧边栏

面向普通用户的产品形态是“HTTPS 面板 + 本机连接器”。Agent 客户端的侧边栏可以打开
部署后的 `https://wln-na.github.io/prompt-sidecar/`；网页面板通过本机连接器读取历史会话和调用本地
桥接。网页本身不能直接访问用户电脑中的 Agent 文件，因此用户仍需先安装并启动本机
Prompt Sidecar 服务。

如果面板和连接器使用不同端口，可以通过 `api` 查询参数指定本机 API，例如：

```text
https://wln-na.github.io/prompt-sidecar/?api=http%3A%2F%2F127.0.0.1%3A9876
```

`api` 只接受 HTTP(S) loopback 地址；未指定时，本机页面使用当前来源，远程 HTTPS 面板
默认尝试 `http://127.0.0.1:9876`。面板启动时先请求 `/api/bootstrap`，确认本机连接器
在线后再读取桥接和会话。

本机连接器默认允许 GitHub Pages 和示例自定义域来源。自部署面板需要在
`config/sidecar.config.json` 的 `panel.allowed_origins` 中加入完整来源，或设置环境变量：

```bash
PROMPT_SIDECAR_ALLOWED_ORIGINS=https://sidecar.example.com \
    python3 backend/server.py
```

来源必须是完整的 `http` 或 `https` origin，不包含路径、查询参数或凭据。连接器只监听
loopback；允许的 HTTPS 来源只能访问本机连接器，历史会话不会因为打开面板而自动上传到
Prompt Sidecar 服务端。

#### GitHub Pages 部署

仓库提供 `.github/workflows/deploy-pages.yml`，会把 `frontend/` 发布为 GitHub Pages
静态站点。工作流运行成功后，在仓库 Settings → Pages 中确认 Source 使用 GitHub
Actions。GitHub Pages 会提供免费的项目子域名
`https://wln-na.github.io/prompt-sidecar/`，不需要购买域名或配置 DNS。

如果以后购买自己的域名，再在 Pages 的 Custom domain 中配置，例如
`app.promptsidecar.com`，并在域名服务商添加 CNAME 记录。仅提交 `CNAME` 文件不会自动
完成自定义域名配置。

自定义域名的 DNS 记录示例：

```text
app  CNAME  wln-na.github.io
```

DNS 生效后再开启 Enforce HTTPS，并用下面的命令验证：

```bash
dig +short app.promptsidecar.com CNAME
curl -I https://app.promptsidecar.com/
```

公网面板部署和本机连接器是两件事。更新 `frontend/` 后需要重新运行 GitHub Actions；
更新 `backend/server.py` 后需要重启每台用户电脑上的本机连接器。

以上及后续命令均从项目根目录执行。前台自定义端口使用
`python3 backend/server.py --port 9876`；macOS 常驻服务使用
`PORT=9876 ./scripts/start.sh`。DSH 插件默认端口为 `8766`，独立于面板服务端口。

> DSH 桥的会话日志解析依赖 Python 3.14+ 标准库 `compression.zstd`
> （或 pip 安装 `zstandard`）。DSH 不可用时不影响 Codex 桥。

### 首次配置润色模型

润色服务读取 `$CODEX_HOME/config.toml`，未设置 `CODEX_HOME` 时读取
`~/.codex/config.toml`。复用其中的 `model`、`model_provider`、对应 provider 的
`base_url` 和 `env_key`；调用的是该 endpoint 的 `/responses` 接口。
请先确认所选模型和代理支持此接口。没有配置时会使用代码中的默认模型和
OpenAI endpoint，不代表已有可用凭据。

认证优先读取 provider 的 `experimental_bearer_token`，再读取 `env_key`
指定的环境变量；OpenAI endpoint 还支持 `OPENAI_API_KEY`。推荐使用已有的
环境变量凭据，勿将凭据写入仓库配置、示例、截图或日志。
设置好模型配置和凭据后，在同一终端运行 `python3 backend/server.py`，
先确认能完成一次润色。桥接配置 `config/sidecar.config.json` 与这份润色模型配置用途不同。

服务用于本机单用户场景。GET 和 POST 都校验本机来源及浏览器同源请求，
静态服务仅暴露 `frontend/` 中的页面资源。
公开源代码不等于部署公网服务；当前没有多用户身份认证或公网部署流程。

## 启停

新克隆后，macOS 可在项目根目录运行 `./scripts/start.sh` 启动常驻服务。
以下脚本仅适用于 macOS，且要求本机已安装 Codex CLI。其他环境使用上面的前台启动命令；
各 Agent 桥是否可用还取决于本机程序和数据格式。

启动脚本会动态生成并加载通用标签 `com.prompt-sidecar.agent` 的用户级
LaunchAgent，面板服务会常驻；日志写入 `~/Library/Logs/PromptSidecar/`，
仅为当前用户可读。
若该服务曾被系统禁用，`scripts/start.sh` 会先重新启用它，再加载服务，无需 `sudo`。

```bash
./scripts/start.sh
./scripts/stop.sh
```

LaunchAgent 会记录启动时的 `CODEX_HOME` 路径覆盖值，但不会复制 shell 中的凭据环境变量。
如果模型或桥接依赖这类变量，先使用同一终端的前台启动方式；仅在终端 `export`
后运行 `scripts/start.sh` 不会让常驻服务自动获得凭据。
更换路径配置后，需要先停止再启动服务以重新生成 plist。

## 测试

完整测试包含 DSH 压缩日志夹具。Python 3.11–3.13 必须安装 `zstandard`，
即使运行时没有启用 DSH；Python 3.14+ 使用标准库实现。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/test.txt
```

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

完整本地发布前检查（Python/JavaScript 回归、语法、diff 和工作区/历史 secret 扫描）：

```bash
PYTHON_BIN=.venv/bin/python bash scripts/check.sh
```

当前没有仓库级 formatter、linter 或 CI 流水线配置；本地通过不能替代目标仓库的审查要求。

## Bridge Registry

公开示例保存在 `config/sidecar.config.example.json`；首次启动会复制为已忽略的
`config/sidecar.config.json`。`bridges` 是按来源 ID 索引的注册表，每个条目绑定
一个 adapter：

桥接契约位于 `backend/bridges/contracts.py`，adapter 注册位于
`backend/bridges/registry.py`。`/api/config` 只返回配置，`/api/health` 独立执行运行时
探测，避免打开设置页时重复触发远程健康检查。

```json
{
    "bridges": {
        "codex": {
            "enabled": true,
            "adapter": "codex",
            "transport": "auto"
        }
    }
}
```

通用字段：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否注册该桥；默认禁用的条目不参与列表、探测或发送 |
| `adapter` | adapter 实现 ID，如 `codex`、`dsh`、`claude`、`workbuddy` |
| `label` | 面板和会话列表显示的名称 |
| `transport` | 通信方式，如 `auto`、`cli`、`http`、`http+acp` |
| `surface` | 会话所在界面，如 `desktop`、`cli`、`cloud_task` |
| `endpoint` | 网络桥地址；不含凭据，CLI adapter 可留空 |
| `auth_ref` | 环境变量名，只引用凭据，不保存凭据值 |
| `session_binding` | 绑定方式；外部桥使用 `explicit`，Codex 右侧面板可使用 `route` 预选候选 |
| `read_only` | 是否只读；缺省为 `true` |
| `timeout_ms` | 单次桥操作的超时时间，单位毫秒 |
| `model_selection` | 目标会话的模型选择；`bridge_default` 或 `explicit` |

adapter 可以附加自己的路径字段。公开示例保留 Codex 的 `home`，以及 DSH
兼容需要的 `home`、`sessions_dir`、`workspace_storage`、`bridge_url` 和
`token_file`。这些字段只放路径或地址，不放密钥。

DSH、Claude、WorkBuddy Local 和 Gemini 未明确配置的子路径会随 `home` 推导。
已经写入文件的子路径（包括复制示例时写入的值）按显式配置保留；更换 `home`
并希望自动派生时，需同时清空或删除这些子路径字段。保存不会再固化未配置的派生路径。
设置页中的子路径留空表示跟随主目录；清空已有子路径并保存可恢复这一行为。

## capabilities 与 health

`capabilities` 是运行时由 adapter 返回的能力声明，不写入配置文件：

- `list_sessions`：列出可选会话
- `read_context`：读取指定会话上下文
- `send`：向指定会话发送结果

`/api/config` 只返回持久化配置，不触发运行时探测；`/api/health` 返回已启用桥的
`capabilities` 和 `health`。设置页并行读取两者并合并展示，因此打开设置不会因
配置读取而重复触发远程健康检查。

## 模型选择

`model_selection` 控制发送到目标桥接时使用的模型，不是 Sidecar 自身用于润色的
全局模型。持久化结构统一为：

```json
{"model_selection": {"mode": "bridge_default", "model_id": ""}}
```

- `dynamic`：Claude Code 当前唯一开放的动态选择，使用 CLI `--model`；留空时跟随
  会话默认模型，模型 ID 也可手动填写。
- `fixed`：Codex queue 固定使用当前线程模型；DSH、WorkBuddy、OpenClaw、
  OpenHands、Cursor、Gemini 和 Devin 暂按固定模型处理。
- `unsupported`：Copilot Tasks 与豆包工作当前不支持选择模型；固定或只读桥接会
  拒绝 `explicit` 配置。

“跟随会话模型”表示 Sidecar 只能跟随目标会话的模型，不表示模型不可用。顶栏的
“润色模型”是 Sidecar 自身调用的模型，与目标会话模型分别展示。
“目标模型”读取 Codex 的 `turn_context` / `thread_settings_applied`、Claude 的
`assistant.message.model` 或 WorkBuddy 本地的 `assistant.providerData.model`，
并标注“最近记录”；悬停可查看来源和记录时间（如有）。日志中的模型不保证是
下一次执行的模型。没有明确记录或尚未绑定会话时显示“未知”，不会从润色模型、
默认配置或 `current-thread` / `session-default` 等发送策略值推断模型名称。
`/api/context` 的可选 `model_id`、`model_source`、`model_timestamp` 字段传递这份记录。

## 凭据边界

`auth_ref` 只接受环境变量名；它仅用于需要云端认证的可选 adapter。
WorkBuddy 本地桥不读取云端 access token。运行时从环境读取值，配置、日志、
API 响应和仓库文件都不保存该值。

- 不在配置中写 bearer token、Cookie、密码或完整认证头
- 不把凭据值拼进 `endpoint`、URL 查询参数或发送内容
- DSH 的 `token_file` 仅保存文件路径；token 由本地插件生成，并已被
  `.gitignore` 排除
- `config/sidecar.config.json` 是本机配置，不应提交

## 超时、重试与鉴权错误

- `timeout_ms` 统一控制 App Server、CLI、HTTP 和 WebSocket 调用时限
- `retry_count` 只用于可安全重试的读取/连接失败；OpenClaw 发送复用同一
  `idempotencyKey`，OpenHands 与非幂等 CLI 发送不自动重试，避免重复消息
- `401/403` 和缺失 `auth_ref` 统一归类为鉴权错误；临时网络错误统一归类为桥接不可用

## 内置兼容与可选桥

示例配置包含十一个已注册来源。`claude` 默认启用且允许显式发送；`workbuddy_local`
直接读取本机 WorkBuddy 数据库和 transcript，不需要云端 token；
`openclaw` 和 `openhands` 默认禁用，配置 endpoint 与鉴权后再启用。条目存在
不代表外部程序已经就绪，实际状态以运行时 health 为准。

### Codex

- 配置使用 `adapter: "codex"`、`transport: "auto"` 和 `home: "~/.codex"`
- 会话列表：优先 Codex App Server `thread/list`，失败时回退 `state_5.sqlite`
- 上下文：读取 rollout transcript；保留现有过滤和 18,000 字符预算
- 右侧面板路由可以提供绑定候选，最终仍落到显式的
  `source=codex` + `session_id`
- 回写：`codex queue --thread <id> --message <润色结果>`（只支持排队）
- 也可用 `CODEX_HOME` 环境变量覆盖 home；回写路径同样跟随它

### DSH

- 会话列表与上下文：解析
  `<sessions_dir>/<workspace>/session-*/session.v4.jsonl.zstd`
  （zstd JSONL，事件含 `session` 头、`session/title`、`user/message`、
  `assistant/message`）；归档状态读 `workspace.json`。纯文件读取，
  不需要 DSH 在运行。
- 回写：需要安装 DSH 插件 `dsh-plugin/prompt-sidecar-bridge/`
  （DeepSeek Harness → Plugins → Add plugin，选择该目录）。插件在
  Desktop 内监听 `127.0.0.1:8766`，用 SessionController 的 prompt API
  支持 `queue`（追加排队轮次）和 `steer`（打断当前轮）两种发送方式，
  冷会话自动 resume。token 由插件写入 `token_file`，Sidecar 读取后鉴权。
- 插件未安装/未运行时，顶部状态徽标会显示“DSH 桥未安装/未运行”，
  发送会得到明确错误提示。

### Claude

- 示例使用 `adapter: "claude"`、`transport: "cli"`、
  `home: "~/.claude"`、`bin: "claude"`
- 会话列表与上下文：读取本地 `projects/**/*.jsonl` transcript
- 默认启用且 `read_only: false`；通过 `claude -p --resume <session-id>` 排队发送
- 不支持把普通 resume 调用冒充 `steer`；需要 steer 时使用支持该语义的上游接口

### WorkBuddy Local

- 使用 `adapter: "workbuddy"`、`surface: "local"`、`transport: "local"`
- 会话列表：只读 `<home>/workbuddy.db` 的 `sessions` 表
- 上下文：读取 `<projects_dir>/<encoded-cwd>/<session-id>.jsonl`
- 回写：调用本机 WorkBuddy 内置 CLI 的
  `codebuddy-lite-wb.mjs -p --resume <session-id>`；不需要云端 token
- `script` 留空时自动查找 macOS WorkBuddy.app 内置 CLI，也可在设置中指定路径
- `surface: "cloud_task"` 的 Open API/ACP adapter 仍保留为可选扩展，但默认配置
  不要求也不启用它
- 云端 ACP 的 `acp_cwd` 是远端主机的工作目录，兼容默认值为 `/workspace`；
  若远端使用其他目录，请在该 bridge 配置中明确设置。它不表示本机仓库位置，
  也不能据此判断远端目录实际存在。

### OpenClaw

- 使用 `adapter: "openclaw"`、`transport: "websocket"`，默认 endpoint 为
  `ws://127.0.0.1:18789`
- 列表：`sessions.list`；上下文：`chat.history`
- 发送：`chat.send`，`queue` 映射为 `followup`，`steer` 映射为 `steer`；使用
  `idempotencyKey` 支持安全重试
- 鉴权通过 `auth_ref` 指向的环境变量读取 Gateway token
- 默认禁用；未运行 Gateway 或缺少 token 时不会影响其它桥

### OpenHands

- 使用 `adapter: "openhands"`、`transport: "http"`，默认 endpoint 为
  `http://127.0.0.1:3000`
- 列表：`GET /api/conversations/search`；上下文：
  `GET /api/conversations/{id}/events/search`
- 发送：`POST /api/conversations/{id}/events`，以 `run: true` 启动一轮
- 鉴权通过 `auth_ref` 指向的 `X-Session-API-Key` 环境变量读取
- 当前只声明 `queue`，不把普通消息注入误称为 `steer`
- 默认禁用；自托管和云端部署的 endpoint/权限需要分别验证

### Cursor Cloud

- 使用 Cloud Agents API，默认 endpoint 为 `https://api.cursor.com`
- 列表：`GET /v1/agents`；上下文：读取近期 run 的最终 result；发送：
  `POST /v1/agents/{id}/runs`，支持 `queue` 和 `steer`
- 使用 `auth_ref` 指向的 Cursor API key，默认禁用
- 本地 Cursor IDE 的当前会话不在公开 Cloud API 范围内，不能把它声称为已桥接

### GitHub Copilot Tasks

- 使用 GitHub agent tasks API，默认 endpoint 为 `https://api.github.com`
- 列表和任务元数据来自 `/agents/tasks`；发送能力关闭，因为当前公开 API
  没有向既有 Copilot task 注入消息的稳定接口
- 使用 `GITHUB_TOKEN`，默认禁用

### Gemini CLI

- 使用 `--list-sessions` 兼容的本地会话文件发现和 `--resume <session-id>` 发送
- 会话文件位于 `~/.gemini/tmp/<project_hash>/chats/`
- 默认禁用；不同 Gemini CLI 版本的会话 JSON 结构需要实际验证

### Devin

- 使用 Devin v3 organizations API；列表、消息历史和发送都需要 `organization_id`
- 发送使用 `POST /v3/organizations/{org}/sessions/{session}/messages`
- 使用 `DEVIN_API_KEY`，默认禁用；没有 organization 权限时不回退到未授权接口

### 豆包工作只读 MCP

- 不读取或注入豆包现有会话，也不声称它们是 Sidecar 会话
- 提供 `scripts/prompt_sidecar_mcp.py`：仅暴露 `prompt_sidecar_health`、
  `prompt_sidecar_sessions`、`prompt_sidecar_context` 三个只读 MCP 工具
- 在豆包工作中把该脚本作为 stdio MCP server 启动；它只读取本地 Sidecar HTTP API
- 自定义服务端口时，在 MCP 进程环境中设置 `PROMPT_SIDECAR_URL`，例如
  `http://127.0.0.1:9876`；默认地址为 `http://127.0.0.1:9876`，仅允许本机地址。

## 会话绑定

Bridge Registry 不接受“当前会话”这种隐式猜测。列表、上下文和发送都使用
显式的 `source` + `session_id`：

1. URL 中的显式参数（如 `?source=dsh&session_id=…`）
2. Codex 右侧面板路由可以预选候选，但仍生成显式绑定
3. 点“⚙ 会话”在统一会话列表（含来源徽标，支持标题/路径过滤）中选择

DSH、Claude 和 WorkBuddy Local 没有可靠的“当前活动会话”自动检测，必须从
列表选择或通过显式 URL 绑定。

## 会话列表刷新

会话列表只在页面加载、切换绑定或窗口重新获得焦点时刷新。也可以点击
“⚙ 会话”弹窗里的“刷新列表”或绑定栏“刷新”按钮手动重新拉取
最近 50 个未归档会话。

## 会话显示名

- Codex：优先 `threads.name`，回退 `threads.title`。
- DSH：优先 `session/title` 事件，回退首条用户消息。
- 其他 adapter：使用 adapter 返回的标题，缺失时回退会话 ID。

## 发送到当前会话

按绑定会话的来源分发：

- Codex：`codex queue`，追加一个新的 queue turn。
- DSH：插件 prompt API；默认“排队”，可选“插入”（打断当前轮）。
- Claude：示例允许通过 CLI `--resume` 排队发送；配置为只读时不可发送。
- WorkBuddy Local：调用本机 CLI `--resume`，不需要云端 token。
- OpenClaw：支持 `queue` 和 `steer`，使用 Gateway `chat.send.queueMode`。
- OpenHands：支持 `queue`，通过 `run: true` 触发会话执行。

实际可发送需要同时满足 `read_only: false` 和 `capabilities.send: true`。
`read_only` 缺省为 `true`；配置遗漏该字段时按只读处理。

它不会替换 Composer。如果会话正在运行，投递遵循各框架的队列语义；
不要把它等同于中断当前 turn。

## 输出规则

润色请求现在包含两个独立字段：

- `output_style`: `concise` / `standard` / `detailed`
- `output_structure`: 默认 `结论,变更,验证,限制,下一步`

这两个字段会作为内部输出契约传给润色模型，但最终提示词中会转换为自然中文的
“输出要求”，不会暴露 `OUTPUT_CONTRACT`、`style:`、`structure:` 等内部标签。

## 提示词配置

高级选项旁的“配置提示词”可以编辑四种模式模板、三种风格模板、通用偏好和默认选项。
保存后下一次润色读取新配置，已经生成的结果不会重新计算。

- 内置默认模板位于 `backend/prompt_templates.py`。
- 本机覆盖保存在被 Git 忽略的 `config/sidecar.config.json` 的
  `prompt_settings.overrides` 下；与内置模板相同的值不会写入。
- `GET /api/prompt-config` 返回内置值、覆盖值、生效值和预览；
  `POST /api/prompt-config` 接收生效模板并自动压缩为覆盖项。
- 基础保真规则、上下文边界和 Agent 执行协议仍由 `backend/server.py` 统一维护。
  Agent 结果不会再由后处理强行补回用户删除的模板内容。

后续增加自定义模式时，需要在 `backend/prompt_templates.py` 注册模式键，在后端校验集合、
模板配置接口和 `build_request_prompt` 的模式分支中接入，再在前端模式按钮和配置弹窗增加标签。
模板导入导出可以复用 `prompt_settings.overrides` 结构，增加 JSON 文件下载、上传和 schema
版本字段，并在服务端校验未知键、长度和版本后再保存。

## 日志与隐私

- HTTP 访问日志只记录 method、无查询参数的路径、状态码和响应大小；
  `session_id` 等查询参数不会写入日志。
- `config/sidecar.config.json`、LaunchAgent plist、日志、PID、Python 缓存和 token
  文件均不应提交 Git。
- 公开示例只使用 `~`、环境变量名和公开 endpoint，不写真实凭据、用户名或
  本机绝对路径。
- 运行 `./scripts/start.sh` 时，`scripts/generate_launch_agent.py` 会从
  `config/launch-agent.plist.template` 生成本机 plist，其中路径不会进入公开模板。

## 首次 push 检查

仓库使用 `.githooks/pre-push`：

新克隆需要显式启用钩子（此 Git 配置不会随 clone 自动生效）：

```bash
git config --local core.hooksPath .githooks
```

- 推送前扫描当前工作区与全部本地 Git 历史；发现疑似 secret 时阻止 push。
- 每次推送都会检查目标 remote；已有分支时阻止 push，要求先单独检查其旧提交。
- 工具依赖：`gitleaks`；人工核查后确需覆盖时设置
  `PROMPT_SIDECAR_ALLOW_REMOTE_HISTORY=1`。

# Prompt Sidecar

独立于主会话的提示词润色面板。支持多 agent 框架桥接：Codex 和
DeepSeek Harness（DSH）。

## 功能

- 自动读取当前绑定会话最近若干轮用户和助手消息
- 去掉 Skill、system-reminder 和任务导演注入块
- 单独调用本地 OpenAI-compatible 模型代理
- 支持快速、结构化、Agent、系统提示词四种模式
- 支持恢复原文和复制结果
- 不向主会话写入任何消息

## 启动

```bash
python3 server.py
```

打开：`http://127.0.0.1:8765`

> DSH 桥的会话日志解析依赖 Python 3.14+ 标准库 `compression.zstd`
> （或 pip 安装 `zstandard`）。DSH 不可用时不影响 Codex 桥。

## 启停

启动脚本会动态生成并加载通用标签 `com.prompt-sidecar.agent` 的用户级
LaunchAgent，面板服务会常驻；日志写入 `~/Library/Logs/PromptSidecar/`，
仅为当前用户可读。

```bash
./start.sh
./stop.sh
```

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

## 桥接

每个 agent 框架是一个“桥”，统一接口：列出会话、读取上下文、回写。
公开示例保存在 `sidecar.config.example.json`；首次启动会复制为已忽略的
`sidecar.config.json`。路径使用 `~` 或环境变量，不提交本机绝对路径：

```json
{
    "bridges": {
        "codex": {"enabled": true, "home": "~/.codex"},
        "dsh": {
            "enabled": true,
            "home": "~/.dsh",
            "sessions_dir": "~/.dsh/sessions",
            "workspace_storage": "~/.dsh/storages/workspace.json",
            "bridge_url": "http://127.0.0.1:8766",
            "token_file": "~/.dsh/prompt-sidecar-bridge.token"
        }
    }
}
```

### Codex 桥

- 会话列表与上下文：读 `state_5.sqlite` + rollout transcript；
  右侧面板自动绑定基于 `.codex-global-state.json`。
- 回写：`codex queue --thread <id> --message <润色结果>`（只支持排队）。
- 也可用 `CODEX_HOME` 环境变量覆盖 home；回写路径同样跟随它。

### DSH 桥

- 会话列表与上下文：解析 `<sessions_dir>/<workspace>/session-*/session.v4.jsonl.zstd`
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

## 会话绑定

绑定优先级：

1. URL 中的显式 `source` + `session_id`（如 `?source=dsh&session_id=…`）
2. Codex 右侧面板路由 `thread-tab-routes-v1:<threadId>` 的自动匹配
   （仅 Codex 桥）
3. 点“⚙ 会话”在统一会话列表（含来源徽标，支持标题/路径过滤）中选择

DSH 会话没有“当前活动会话”的自动检测，需要从列表选择。

## 会话列表刷新

会话列表只在页面加载、切换绑定或窗口重新获得焦点时刷新。也可以点击
“⚙ 会话”弹窗里的“刷新列表”或绑定栏“刷新”按钮手动重新拉取
最近 50 个未归档会话。

## 会话显示名

- Codex：优先 `threads.name`，回退 `threads.title`。
- DSH：优先 `session/title` 事件，回退首条用户消息。

## 发送到当前会话

按绑定会话的来源分发：

- Codex：`codex queue`，追加一个新的 queue turn。
- DSH：插件 prompt API；默认“排队”，可选“插入”（打断当前轮）。

它不会替换 Composer。如果会话正在运行，投递遵循各框架的队列语义；
不要把它等同于中断当前 turn。

## 输出规则

润色请求现在包含两个独立字段：

- `output_style`: `concise` / `standard` / `detailed`
- `output_structure`: 默认 `结论,变更,验证,限制,下一步`

这两个字段会作为内部输出契约传给润色模型，但最终提示词中会转换为自然中文的
“输出要求”，不会暴露 `OUTPUT_CONTRACT`、`style:`、`structure:` 等内部标签。

## 日志与隐私

- HTTP 访问日志只记录 method、无查询参数的路径、状态码和响应大小；
  `session_id` 等查询参数不会写入日志。
- `sidecar.config.json`、LaunchAgent plist、日志、PID、Python 缓存和 token
  文件均不应提交 Git。
- 运行 `./start.sh` 时会从 `launch-agent.plist.template` 生成本机 plist，
  其中路径不会进入公开模板。

## 首次 push 检查

仓库使用 `.githooks/pre-push`：

- 推送前扫描当前工作区与全部本地 Git 历史；发现疑似 secret 时阻止 push。
- 目标 remote 已存在分支时阻止首次 push，要求先单独检查其旧提交。
- 工具依赖：`gitleaks`；人工核查后确需覆盖时设置
  `PROMPT_SIDECAR_ALLOW_REMOTE_HISTORY=1`。

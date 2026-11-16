# Prompt Sidecar · DeepSeek Harness 桥接设计

日期：2026-11-16
状态：已确认（用户逐段确认通过）

## 背景与目标

Prompt Sidecar 目前只支持 Codex：读取 `~/.codex/state_5.sqlite` + rollout
transcript 提供会话上下文，润色后用 `codex queue` 回写。本次目标：接入
DeepSeek Harness（DSH Desktop 0.2.2-rc.3），达到与 Codex 桥对等的能力：

- 读取 DSH 会话上下文参与润色
- 把润色结果回写进 DSH 会话（queue / steer）
- 面板内统一选择 Codex 与 DSH 会话

已确认的设计决策：

1. 范围：读上下文 + 回写会话（完整对等，需要 DSH 插件做回写通道）。
2. 读取方式：统一走文件解析，路径可配置；Prompt Sidecar 后续还会接入
   其他 agent 框架，桥接口与配置为多框架扩展设计。
3. UI：统一会话列表；绑定栏增加设置按钮，点击弹出会话列表弹窗。

## 已探明的 DSH 事实（实现依据）

- 会话日志：`~/.dsh/sessions/<workspace-slug>/session-<uuid>/session.v4.jsonl.zstd`，
  zstd 压缩 JSONL。Python 3.14 标准库 `compression.zstd` 可直接解压。
  事件类型：`session`（头：id/cwd/createdAt/agentPreset）、`session/title`、
  `user/message`、`assistant/message`、`tool/call`、`tool/result` 等。
- 会话索引：`~/.dsh/storages/workspace.json` 提供 workspace → sessionIds、
  全局 `archivedSessionIds`。
- 回写通道：`SessionController` 暴露 `@Remote('prompt')`，
  `SessionPromptRequest` 带 `mode: 'queue' | 'steer'`，冷会话会自动 resume；
  与 `codex queue` 语义对等。该 RPC 是 GUI 内部协议（Typert over /api），
  Python 侧不直接消费，改由进程内插件调用。
- 插件开发：bundle 在工作区编写，经 Plugins > Add plugin 或 install_bundle
  安装进 Desktop profile；Host-only 插件可注册常驻服务。

## 架构

### 桥接抽象（server.py 内部）

每个 agent 框架一个桥实现，HTTP API 只面向接口：

```python
class SessionBridge:
    source: str                                            # "codex" / "dsh" / 未来新框架
    def list_sessions(self, limit=50) -> list[SessionInfo]  # 含 source 字段
    def build_context_packet(self, session_id) -> dict      # 复用现有 packet 形状
    def send(self, session_id, message, mode="queue") -> dict
```

- Codex 桥：现有逻辑原样迁入（sqlite 绑定、rollout 解析、`codex queue`）。
- 多框架 session id 以来源区分，前端传 `source` + `session_id`。

### 配置（sidecar.config.json，项目根）

不存在时用内置默认值自动生成；所有路径可自定义：

```json
{
  "bridges": {
    "codex":  { "enabled": true, "home": "~/.codex" },
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

### DSH 读取桥（纯文件解析，不依赖插件）

- 扫描 `sessions_dir` 下各工作区目录的 `session-*/session.v4.jsonl.zstd`；
- `compression.zstd` 解压，按事件流提取 `session` 头（cwd 等）、
  `session/title`（标题，缺省回退首条用户消息）、
  `user/message` + `assistant/message`（上下文消息，复用注入块剥离，补
  `<system-reminder>`）；
- 归档状态读 `workspace.json` 的 `archivedSessionIds`；
- 排序取最大事件 time，缺省回退文件 mtime；
- zstd 不可用时该桥禁用并给出明确错误，不影响 Codex 桥。

### DSH 回写插件（prompt-sidecar-bridge，Host-only）

- 随 Desktop 常驻，启动仅绑定 `127.0.0.1` 的 HTTP 服务（端口 8766，
  插件 Config 可改）。
- `GET /health` 供探活；`POST /prompt` 接收 `{sessionId, message, mode}`，
  进程内调用 SessionController 的 `prompt`（queue 追加排队轮次，steer
  打断当前轮）。
- 鉴权：首次启动生成随机 token 写入 `~/.dsh/prompt-sidecar-bridge.token`
  （600 权限），Sidecar 按配置路径读取；无 token 请求拒绝。
- Sidecar `/api/send` 按 `source` 分发：codex → `codex queue`；
  dsh → 插件 HTTP。插件缺失/超时/无 token/会话不存在映射为可读错误，
  UI 给出安装指引。

### UI

- 绑定栏增加 ⚙ 设置按钮，点击弹出会话列表弹窗：统一列表、来源徽标
  （codex/dsh）、按 cwd 分组、标题模糊过滤、最近 50 个未归档、手动刷新。
- 绑定栏展示当前绑定（含来源徽标）。
- URL 显式绑定支持 `?source=dsh&session_id=…`（缺省 codex 保持兼容）。
- 润色流程、输出规则、发送按钮完全复用；发送提供 queue/steer 选择。

## 测试

- `tests/` 新增 unittest：
  - DSH 日志解析（测试内现场构造 zstd fixture）；
  - workspace.json 归档解析；
  - 配置加载与缺省生成；
  - 桥注册、混合来源列表、send 按 source 分发；
  - 现有 Codex 测试保持通过。

## 风险与边界（未验证项）

- `session.v4.jsonl.zstd` 与 `@Remote('prompt')` 均为 DSH 内部接口：
  v4 → v5 或 RPC 变更时需适配；配置化路径 + 桥抽象即为此隔离设计。
- 插件安装、health、实际 queue/steer 回写需在实现后真机验证。

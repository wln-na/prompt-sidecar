# Prompt Sidecar bridge

在 DeepSeek Harness Desktop 内运行一个仅绑定 `127.0.0.1` 的桥接服务，让本地
Prompt Sidecar 面板（`http://127.0.0.1:8765`）可以把润色后的提示词送进 DSH 会话。

## 行为

- 启动时确保 token 文件存在（默认 `~/.dsh/prompt-sidecar-bridge.token`，权限
  0600；不存在则生成随机 hex token）。
- 监听 `127.0.0.1:8766`（可用插件配置 `port` 修改）。
- `GET /health`：返回 `{ok: true, version}`，供 Sidecar 探活。
- `POST /prompt`：请求头带 `Authorization: Bearer <token>`，请求体
  `{sessionId, message, mode}`，`mode` 为 `queue`（追加排队轮次）或
  `steer`（打断当前轮）。插件通过 SessionController 的 prompt API 提交，
  与 GUI 发送语义一致；冷会话会自动 resume。

## 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `port` | `8766` | 桥接服务端口 |
| `tokenFile` | `~/.dsh/prompt-sidecar-bridge.token` | token 文件路径 |

## 限制

- 仅绑定回环地址；token 泄露等于允许向本机 DSH 会话注入消息，请勿外传。
- `session.v4` 会话格式与 prompt API 属于 DSH 内部接口，DSH 升级后可能需要适配。

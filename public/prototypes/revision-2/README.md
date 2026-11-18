# Prompt Sidecar 原型 · 第二轮

生成日期：2026-11-18。仅静态图片，未修改运行界面。

- prototype-1.png：会话信息横条与双栏编辑。
- prototype-2.png：会话信息侧栏与双栏编辑。
- prototype-3.png：展示任务继续建议的双栏状态。

Figma：https://www.figma.com/design/IUa7s2EbkAgpLf06amCrtf
节点分别为 2:2、2:3、2:4；节点内容为生成的位图，并非逐控件可编辑的设计稿。

使用 Figma generate_image（gpt-image-2.5-flare）生成。图片实际尺寸为 1280×960。
文字、任务状态、消息数及模型名均为原型示例，不代表实时运行结果。
现有功能清单来自本项目 index.html 与 app.js；模式必须常驻，未绑定或失败状态仍需后续设计。

共享生成要求：

```text
Create realistic, production-quality UI design for Prompt Sidecar, a Chinese desktop web app, exact target 1440 x 1024. Current date 2026-11-18. This is a single-screen review mock, not a live frontend. Preserve existing product blue accent #3157d5, pale blue #edf1ff, white/slate #172033 surfaces, small purple accents, readable 14–16px Chinese typography. Purpose: explicitly bind an Agent conversation, polish a prompt with optional conversation context OR analyze the next task step, edit the resulting message, then user explicitly sends it. No automatic sending. Strong grouping, whitespace and light dividers; avoid excessive nested cards and ornamental empty-state art. Show a realistic FILLED screen, all essential controls fit without clipping. Header 'Prompt Sidecar' subtitle '结合会话上下文，写好下一条消息', '润色模型 · gpt-6-astra', button '桥接设置'. Canonical target identity ONCE ONLY: platform 'Codex', title '分析本地 Agent 中枢架构', project 'AgentXRay', short ID '01a1ed9d', abbreviated path '…/aicode_library/AgentXRay', switch button '选择会话'. Secondary details link for complete session ID and binding method, don't duplicate title or platform. Status '上下文已载入 · 34 条消息 · 17,880 字', '查看上下文', '刷新上下文'; checkbox '参考当前会话上下文'. Show '目标模型 · 跟随会话' separately from global polishing model. Draft section '原始草稿' text '帮我梳理本地 Agent 中枢架构，指出当前实现的缺口，并给出下一步建议。' with '填入示例', character count, shortcut '⌘ Enter 润色'. CRITICAL four persistent visible mode controls, not hidden: '快速', '结构化', 'Agent', '系统提示词'. Mark '结构化' selected. Output rules visible with style selector '精简' and structure text '结论、变更、验证、限制、下一步'. Primary '润色草稿', secondary '下一步 / 继续任务', optional collapsed row '补充 AgentXRay 任务信息（可选）', helper '导入任务状态，用于下一步分析'. Result heading '可发送版本', badge '已生成 · 可编辑', editable realistic resulting prompt with labels '任务目标','分析范围','输出要求' and 4 short paragraphs about architecture, implementation gaps, evidence and next steps. Under result include checkbox '完成后自动复制', buttons '复制结果', '恢复原文', '重新润色'. Delivery section must show '发送方式：排队' (Codex only offers queue, DO NOT add steer), concise note '加入目标会话的待处理队列', primary action '发送到当前会话', notice '仅点击发送后，才会向目标会话新增消息'. Persistent slot '实际使用模型：发送后显示' (do not invent actual model before sending). Compact read/send health status, full bridge capability internals only through settings. Existing continuation details accessed through '下一步 / 继续任务' include goal, completed evidence, pending, blockers, recommendations, conflicts and missing context. Standard mock does NOT show empty 'AgentXRay 集成模式' banner; AgentXRay imported context is optional, collapsed. No extra invented functions, no histograms/graphs, no browser chrome.
```

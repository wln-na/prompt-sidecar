# 原型 3 修订 · 2026-11-18

本目录是独立设计预览，未接入主应用 API，也不会发送真实消息。
未修改主应用的 index.html、app.js 或 styles.css。

## 查看

打开本目录 index.html。也可在项目根运行：

```sh
python3 -m http.server 8767 --bind 127.0.0.1 --directory public/prototypes/revision-4
```

顶部“原型预览”切换默认状态、分析结果、需要决定三种状态。
default-state.jpg 与 decision-state.jpg 是浏览器实际渲染截图，均为 1280×960。

## 本轮调整

- 沿用第三轮原型 3 的两栏结构。
- 默认右侧复用主应用的原始 SVG 及动画样式，见 prompt-flow.svg。
- Enter 换行；⌘+Enter 或 Ctrl+Enter 润色，不绑定发送。
- 默认状态不出现空的进展、建议或待决定事项，发送按钮禁用。
- 普通分析隐藏待决定事项；只有需要用户决定的示例状态显示。
- 四种模式始终可见；会话搜索、提示词编辑与恢复等交互可试用。

## 素材来源

prompt-flow.svg 从项目 index.html 提取，动画样式从 styles.css 提取。
增加独立 SVG 所需 xmlns、字体变量与减弱动态效果设置，没有重画图形。
codex.png、progress.png、next.png、edit.png 从用户选中的第三轮原型 3 裁出。
这是原型素材，未新增对外部图片服务的依赖。

## 验证与范围

interaction-checks.json 记录本次浏览器交互核对结果；design-qa.md 记录视觉比较。
已实际验证 Enter、两种组合键、会话搜索与切换、模式选择、条件展示、编辑和恢复。
node --check prototype.js 与 git diff --check 通过。
未调用真实模型、未验证生产复制权限、未执行真实发送。
模式与输出规则展示可交互，但不连接模型，因此不代表生成策略实现完成。

顶部状态选择器只用于设计评审；正式页面不需要保留。

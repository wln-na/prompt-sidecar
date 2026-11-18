# 视觉与交互核对

日期：2026-11-18。范围：独立原型设计评审，不包含主应用实现。

## 对照证据

- source visual truth path: ../revision-3/prototype-3.png
- 用户默认状态参考：/var/folders/jb/jbnnwmpj0qn4ydwkss16jzkw0000gn/T/codex-clipboard-9f5b6f2f-4882-4dc5-ae53-9061dfc90151.png
- implementation screenshot path: decision-state.jpg、default-state.jpg
- viewport: 1280 × 960 CSS px
- source pixels: 1280 × 960；implementation pixels: 1280 × 960；截图密度 1，无缩放。
- state: decision-state 对照用户所选原型 3；default-state 对照用户空状态截图。
- full-view comparison evidence: comparison.png，将同尺寸参考与实际页面截图并排。
- focused region comparison evidence: empty-comparison.png，将用户截图与新原型右侧并排；
  用户截图原尺寸 1264 × 742，按比例缩放，右侧为较高的新结果区，容器高度差为预期。

## 比较与调整

第一轮发现：主要区块缺少选中原型中的来源及进展图标，进展区垂直间距偏大。
调整：从选中的原型提取来源、进展、建议和编辑图标；缩小进展区的底部留白。
第二轮证据：comparison.png。左右栏分界、会话选择、草稿、四模式、输出规则、
双操作、可折叠补充资料，以及右侧分析、条件提示和可编辑结果保持原型 3 的结构。

## 五项视觉核对

- 字体：系统中文无衬线，正文 14–15px、标题 16–21px；关键信息无截断。
- 布局：严格两栏，1280px 下分界约 610px；无横向溢出，底部操作可见。
  快捷键移至输入框下单独一行，因此左栏控件略向下移动，为本轮预期调整。
- 颜色：白底、浅蓝边框和蓝色主操作；需决定事项保持浅琥珀色区分。
- 资产：空状态使用源 SVG 与源动画；来源及主要结果图标复用选中图像素材。
  不拉伸、不替换流程图。静态 JPG 不包含动画，HTML 预览保留动画。
- 文案：Enter 换行、组合键润色；进展与建议为样例，需决定事项仅在对应状态出现；
  未重复展示消息条数、字符数或会话 ID。

## 交互与限制

已通过浏览器实际操作验证：Enter 保留换行且仍是默认状态；Meta+Enter、Control+Enter
均进入示例润色结果；四模式切换；普通分析隐藏待决定事项；可编辑提示词并恢复原文；
会话搜索过滤及选择后回到默认状态；发送入口显示原型说明，无真实发送。
浏览器采集到的 error 日志为空。interaction-checks.json 保存核对摘要。

正式设置为入口演示；复制权限、真实模型和真实消息投递未验证。
顶部预览状态切换是评审工具，正式页面应移除。少量辅助按钮图标尚未复刻，属 P3。
自动复制默认沿用现有应用的关闭状态，不以生成图中的示例勾选作为产品默认要求。

无待解决 P0/P1/P2 问题。后续实现时需接回真实会话与模型数据，并替换位图图标为正式资源。

final result: passed

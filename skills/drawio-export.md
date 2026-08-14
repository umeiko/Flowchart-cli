---
name: drawio-export
description: 把当前流程图导入 draw.io（diagrams.net）人工编辑的操作指引。用户想把图导入 drawio、Visio 类工具手动修改、二次编辑时使用
---

用户想把流程图导入 draw.io 手动编辑时，按以下步骤引导：

1. 用 get_current_diagram 获取当前流程图的 .mmd 文件路径（若还没有图，先按用户需求创建）；
2. 告诉用户操作步骤：
   - 打开 draw.io（网页版 https://app.diagrams.net 或桌面版）；
   - 菜单「Arrange（调整图形）」→「Insert（插入）」→「Advanced（高级）」→「Mermaid」；
   - 粘贴 .mmd 文件的全部内容，点击插入；
3. 插入后图形即可在 draw.io 中人工拖拽、改字、调色；
4. 明确提醒用户：draw.io 的 Mermaid 支持是"导入式快照"，在 draw.io 里的后续修改
   不会回写 .mmd 文件；如果之后还想用本工具继续迭代，应改 .mmd 后重新导入；
5. 也可以直接把产物目录里的 SVG 文件（current.svg）拖进 draw.io 作为矢量图使用，
   但 SVG 方式导入后不能按流程图结构编辑，仅适合微调外观。

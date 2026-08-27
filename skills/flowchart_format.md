---
name: flowchart_format
description: flowchart format skill for draw.io engine, use it when user demand
---

用户想要根据文档制作流程图时，输入文档请总结以后做简要流程，并且颜色按照必须流程（主色）和可选流程（辅助色）进行设置。除了必须用英文的术语，或用户明确要求外，请使用中文制作流程图。

框的尺寸规范：矩形框以及开始结束框宽 172px、高 28px、流程框上下间距28px。调用 create_diagram 时通过布局参数传入：`node_width=172`、`node_height=28`、`gap_y=28`（这是最小高度，文字换行较多时框高会自动增加，不会溢出）；层间距保持默认即可。如果出图验证失败需要重画，可以在 172 附近微调 node_width 或加大 gap_x/gap_y 间距再试。

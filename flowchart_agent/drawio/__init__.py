"""drawio 出图引擎：LLM 直出 draw.io 原生 XML → 确定性布局 → 桌面版渲染。

- xml.py         XML 提取与清洗（含字体后处理 apply_font）
- layout.py      架构图布局器（层容器/子容器/组件网格）
- layout_flow.py 流程图布局器（分层 + 分支并排 + 直角走线规范化）——
                 排查走线问题用 scripts/route_report.py 生成逐边路由报告
- render.py      draw.io 桌面版 CLI 渲染与可用性检查
"""

from .layout import apply_layout
from .layout_flow import FlowGrid, apply_flow_layout, make_flow_grid
from .render import DrawioNotFoundError, check_drawio_available, render_drawio
from .xml import apply_font, extract_xml, sanitize_xml

__all__ = [
    "apply_font",
    "apply_layout",
    "apply_flow_layout",
    "DrawioNotFoundError",
    "FlowGrid",
    "check_drawio_available",
    "make_flow_grid",
    "render_drawio",
    "extract_xml",
    "sanitize_xml",
]

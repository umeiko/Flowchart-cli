"""Prompt 模板包：按子 Agent 分文件管理，此处汇总 re-export。

- generate.py  生成/修复（FlowchartAgent 生成端）
- verify.py    三档检视（FlowchartAgent 验证端）
- style.py     风格生成（StyleAgent）
- restyle.py   风格转换（RestyleAgent）
- ocr.py       OCR 工具
- route.py     一级路由（生成图 / 检查图 / 闲聊）+ 图型二级路由（流程图 / 架构图）
- drawio.py    drawio 引擎生成/修复（架构图 / 流程图两套模板）
- check/       检查管线（图像描述、二级分类、四类检查清单）

既有调用方保持 `from . import prompts` 用法不变。
"""

from .generate import (
    GENERATE_SYSTEM,
    GENERATE_USER,
    REVISE_USER,
    RENDER_ERROR_FEEDBACK,
)
from .verify import (
    SUBMIT_RESULT_TOOL,
    VERIFY_PROMPT,
    VERIFY_LAYOUT_PROMPT,
    VERIFY_CODE_PROMPT,
)
from .style import STYLE_GENERATE_SYSTEM, STYLE_GENERATE_USER, STYLE_REVISE_USER
from .restyle import RESTYLE_SYSTEM, RESTYLE_USER, RESTYLE_REVISE_USER
from .ocr import OCR_PROMPT
from .route import ROUTE_SYSTEM, ROUTE_USER, DIAGRAM_TYPE_SYSTEM, DIAGRAM_TYPE_USER
from .drawio import (
    DRAWIO_SYSTEM,
    DRAWIO_USER,
    DRAWIO_REVISE_USER,
    drawio_system_prompt,
)
from . import check

__all__ = [
    "GENERATE_SYSTEM",
    "GENERATE_USER",
    "REVISE_USER",
    "RENDER_ERROR_FEEDBACK",
    "VERIFY_PROMPT",
    "VERIFY_LAYOUT_PROMPT",
    "VERIFY_CODE_PROMPT",
    "SUBMIT_RESULT_TOOL",
    "STYLE_GENERATE_SYSTEM",
    "STYLE_GENERATE_USER",
    "STYLE_REVISE_USER",
    "RESTYLE_SYSTEM",
    "RESTYLE_USER",
    "RESTYLE_REVISE_USER",
    "OCR_PROMPT",
    "ROUTE_SYSTEM",
    "ROUTE_USER",
    "DIAGRAM_TYPE_SYSTEM",
    "DIAGRAM_TYPE_USER",
    "DRAWIO_SYSTEM",
    "DRAWIO_USER",
    "DRAWIO_REVISE_USER",
    "drawio_system_prompt",
    "check",
]

"""Prompt 模板包：按子 Agent 分文件管理，此处汇总 re-export。

- generate.py  生成/修复（FlowchartAgent 生成端）
- verify.py    三档检视（FlowchartAgent 验证端）
- style.py     风格生成（StyleAgent）
- restyle.py   风格转换（RestyleAgent）
- ocr.py       OCR 工具
- route.py     一级路由（生成图 / 检查图 / 闲聊）
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
    VERIFY_PROMPT,
    VERIFY_LAYOUT_PROMPT,
    VERIFY_CODE_PROMPT,
)
from .style import STYLE_GENERATE_SYSTEM, STYLE_GENERATE_USER, STYLE_REVISE_USER
from .restyle import RESTYLE_SYSTEM, RESTYLE_USER, RESTYLE_REVISE_USER
from .ocr import OCR_PROMPT
from .route import ROUTE_SYSTEM, ROUTE_USER
from . import check

__all__ = [
    "GENERATE_SYSTEM",
    "GENERATE_USER",
    "REVISE_USER",
    "RENDER_ERROR_FEEDBACK",
    "VERIFY_PROMPT",
    "VERIFY_LAYOUT_PROMPT",
    "VERIFY_CODE_PROMPT",
    "STYLE_GENERATE_SYSTEM",
    "STYLE_GENERATE_USER",
    "STYLE_REVISE_USER",
    "RESTYLE_SYSTEM",
    "RESTYLE_USER",
    "RESTYLE_REVISE_USER",
    "OCR_PROMPT",
    "ROUTE_SYSTEM",
    "ROUTE_USER",
    "check",
]

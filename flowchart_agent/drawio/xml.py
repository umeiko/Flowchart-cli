"""drawio XML 的提取与清洗：从 LLM 输出中取出 mxfile XML 并修复常见违规。"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def extract_xml(text: str) -> str:
    """从 LLM 输出提取 XML：优先 ```xml 代码块，否则取第一个 <mxfile> 起到结尾。"""
    m = re.search(r"```(?:xml)?\s*\n(.*?)```", text, re.S)
    candidate = m.group(1) if m else text
    start = candidate.find("<mxfile")
    if start == -1:
        return ""
    end = candidate.rfind("</mxfile>")
    if end == -1:
        return ""
    return candidate[start : end + len("</mxfile>")]


def sanitize_xml(text: str) -> str:
    """修复模型的常见违规：value 属性里原样写了 <br/>（非法 XML），自动转义。"""
    def _fix(m: re.Match) -> str:
        fixed = re.sub(r"<br\s*/?>", "&lt;br/&gt;", m.group(1))
        if fixed != m.group(1):
            logger.warning("自动转义 value 属性中的字面 <br/>")
        return f'value="{fixed}"'
    return re.sub(r'value="([^"]*)"', _fix, text)

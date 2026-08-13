"""从 LLM 输出中提取 Mermaid 代码块，容忍多余的解释文字。"""

from __future__ import annotations

import re

# 优先匹配 ```mermaid ... ```，其次匹配任意 ``` 包裹、且以图型关键字开头的内容
_FENCED_MERMAID = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FENCED_ANY = re.compile(r"```\s*\n(.*?)```", re.DOTALL)
_GRAPH_KEYWORDS = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "gantt",
    "pie",
)


def extract_mermaid(text: str) -> str:
    """提取 Mermaid 源码；找不到时返回空串。"""
    match = _FENCED_MERMAID.search(text)
    if match:
        return match.group(1).strip()
    for match in _FENCED_ANY.finditer(text):
        body = match.group(1).strip()
        if body.startswith(_GRAPH_KEYWORDS):
            return body
    # 兜底：全文本身就是 mermaid 代码
    stripped = text.strip()
    if stripped.startswith(_GRAPH_KEYWORDS):
        return stripped
    return ""

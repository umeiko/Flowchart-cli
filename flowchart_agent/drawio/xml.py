"""drawio XML 的提取与清洗：从 LLM 输出中取出 mxfile XML 并修复常见违规。"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

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


def apply_font(xml_text: str, family: str | None, size: str | None) -> str:
    """给所有 cell 的 style 注入/覆盖 fontFamily 与 fontSize。

    确定性后处理（不信任 LLM 照抄骨架里的字体字段）：family/size 为
    None 的项不注入。字号按原样写入（drawio 接受小数如 10.5），
    无法解析为数字时打 warning 并跳过字号注入。
    注意：字体需本机已安装，未安装时 draw.io 静默回退默认字体。
    """
    props: dict[str, str] = {}
    if family:
        props["fontFamily"] = family
    if size:
        try:
            float(size)
        except ValueError:
            logger.warning("DRAWIO_FONT_SIZE=%r 不是数字，跳过字号注入", size)
        else:
            props["fontSize"] = size
    if not props:
        return xml_text

    root = ET.fromstring(xml_text)
    for cell in root.iter("mxCell"):
        style = cell.get("style")
        if not style:
            continue
        parts = [p for p in style.split(";") if p]
        keys = [p.split("=", 1)[0] for p in parts]
        for k, v in props.items():
            if k in keys:
                parts[keys.index(k)] = f"{k}={v}"
            else:
                parts.append(f"{k}={v}")
        cell.set("style", ";".join(parts) + ";")
    logger.info("[drawio] 字体注入：%s", " ".join(f"{k}={v}" for k, v in props.items()))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)

"""draw.io XML 生成 PoC：从文档直接生成 .drawio 文件。

用法：
    uv run python scripts/gen_drawio.py <文档路径> [-o 输出.drawio]

不经过 Mermaid/渲染循环，产出后用 draw.io「File → Open」手动导入查看。
"""

from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flowchart_agent.config import load_settings
from flowchart_agent.drawio import (
    apply_layout,
    extract_xml,
    render_drawio,
    sanitize_xml,
)
from flowchart_agent.llm.client import LLMClient
from flowchart_agent.prompts.drawio import DRAWIO_SYSTEM, DRAWIO_USER

logger = logging.getLogger("gen_drawio")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="从文档生成 draw.io XML（PoC）")
    parser.add_argument("document", help="需求文档路径")
    parser.add_argument("-o", "--output", help="输出 .drawio 路径", default=None)
    args = parser.parse_args()

    doc_path = Path(args.document)
    document = doc_path.read_text(encoding="utf-8")
    out_path = Path(args.output) if args.output else doc_path.with_suffix(".drawio")

    settings = load_settings()
    llm = LLMClient(settings.text_model)
    reply = llm.chat([
        {"role": "system", "content": DRAWIO_SYSTEM},
        {"role": "user", "content": DRAWIO_USER.format(document=document)},
    ])

    xml_text = sanitize_xml(extract_xml(reply))
    try:
        xml_text = apply_layout(xml_text)  # 注入确定性几何（等大、网格对齐）
        ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as e:
        raw_path = out_path.with_suffix(".raw.txt")
        raw_path.write_text(reply, encoding="utf-8")
        logger.error("XML 校验/布局失败：%s（原始输出已存 %s）", e, raw_path)
        return 1

    out_path.write_text(xml_text, encoding="utf-8")
    logger.info("已生成 %s（%d 字符）", out_path, len(xml_text))

    png_path = render_drawio(
        out_path, out_path.with_suffix(".png"),
        settings.drawio_path, fmt="png", scale=int(settings.render_scale),
    )
    if png_path:
        logger.info("已渲染 %s", png_path)
    else:
        logger.warning("PNG 渲染失败，.drawio 文件仍可手动导入 draw.io 查看")
    return 0


if __name__ == "__main__":
    sys.exit(main())

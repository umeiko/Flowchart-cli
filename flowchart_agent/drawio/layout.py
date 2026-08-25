"""draw.io 架构图的确定性布局器。

drawio 子 Agent 的 LLM 只输出结构（层容器 / 子容器 / 组件 + 样式），不写坐标；
本模块按固定网格公式计算所有 mxGeometry 并注入 XML，保证：
- 所有组件框严格等大（W×H）；
- 层容器等宽、左对齐、纵向堆叠；
- 层内支持一层子容器（如 DataService 层 → 智能服务层）：子容器占满层内宽，
  内部组件同样网格排列；松散的层内组件按最多 COLS 列的矩阵排列，每行居中。

坐标算术不交给 LLM——它不擅长且容易悄悄出错。
层级识别不靠样式：parent="1" 的 vertex 是层容器；层容器下带子节点的 vertex
是子容器；其余是组件。挂得比"层 → 子容器"更深的节点会被上提拍平。
"""

from __future__ import annotations

import logging
import math
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# 网格常量（与 prompts/drawio.py 的说明保持一致）
W = 180        # 组件宽
H = 60         # 组件高
GAP = 20       # 组件间距 / 容器内边距 / 层间距
TITLE = 30     # 容器标题区高度（标题显示在色块顶部）
COLS = 4       # 每行最多组件数
X0 = 40        # 整图左边距
Y0 = 40        # 整图顶边距


def apply_layout(xml_text: str) -> str:
    """给不带几何信息的 mxfile XML 注入计算好的 mxGeometry，返回新 XML。"""
    root = ET.fromstring(xml_text)
    model = root.find("diagram/mxGraphModel")
    if model is None:
        raise ValueError("XML 中未找到 diagram/mxGraphModel")
    cells = model.find("root")
    if cells is None:
        raise ValueError("XML 中未找到 root 元素")

    layers = [
        c for c in cells
        if c.get("vertex") == "1" and c.get("parent") == "1"
    ]
    if not layers:
        raise ValueError("XML 中没有层容器（parent=\"1\" 的 vertex）")
    layer_ids = {c.get("id") for c in layers}

    children: dict[str, list[ET.Element]] = {}
    for c in cells:
        if c.get("vertex") == "1" and c.get("id") not in layer_ids:
            children.setdefault(c.get("parent"), []).append(c)

    # 子容器 = 层容器下带子节点的 vertex
    sub_ids = {
        item.get("id")
        for lid in layer_ids
        for item in children.get(lid, [])
        if children.get(item.get("id"))
    }

    # 层级修正：挂错位置的一律上提到最近的 层/子容器 祖先
    by_id = {c.get("id"): c for c in cells}
    for c in cells:
        if c.get("vertex") != "1" or c.get("id") in layer_ids:
            continue
        target = _resolve_container(c, by_id, layer_ids, sub_ids)
        if target is None:
            logger.warning("组件「%s」挂不到任何容器下，已跳过布局", c.get("value"))
            continue
        if c.get("parent") != target:
            logger.warning("「%s」层级挂错或过深，已上提", c.get("value"))
            c.set("parent", target)
            # 换 parent 后重新登记到 children 映射
            for lst in children.values():
                if c in lst:
                    lst.remove(c)
                    break
            children.setdefault(target, []).append(c)

    # 组装每层的布局块序列（松散组件凑行，子容器独占整宽）
    layer_blocks: dict[str, list[tuple]] = {}
    for layer in layers:
        blocks: list[tuple] = []
        buffer: list[ET.Element] = []

        def flush() -> None:
            nonlocal buffer
            if buffer:
                blocks.append(("row", buffer))
                buffer = []

        for item in children.get(layer.get("id"), []):
            if item.get("id") in sub_ids:
                flush()
                blocks.append(("sub", item, children.get(item.get("id"), [])))
            else:
                buffer.append(item)
                if len(buffer) == COLS:
                    flush()
        flush()
        layer_blocks[layer.get("id")] = blocks

    def block_width(b: tuple) -> int:
        if b[0] == "row":
            return len(b[1]) * W + (len(b[1]) + 1) * GAP
        comps = b[2]
        cols = min(max(len(comps), 1), COLS)
        return cols * W + (cols + 1) * GAP + 2 * GAP

    # 所有层等宽：取各层内容宽度的最大值
    layer_w = max(
        (max(block_width(b) for b in blocks) if blocks else W + 2 * GAP)
        for blocks in layer_blocks.values()
    )

    y = Y0
    for layer in layers:
        blocks = layer_blocks[layer.get("id")]
        if not blocks:
            logger.warning("层容器「%s」没有组件（模型漏了内容？）", layer.get("value"))
        y_in = TITLE + GAP
        for b in blocks:
            if b[0] == "row":
                row = b[1]
                row_w = len(row) * W + (len(row) - 1) * GAP
                x = (layer_w - row_w) / 2
                for comp in row:
                    _set_geometry(comp, x, y_in, W, H)
                    x += W + GAP
                y_in += H + GAP
            else:
                _, sub, comps = b
                sub_w = layer_w - 2 * GAP
                rows = max(math.ceil(len(comps) / COLS), 1)
                sub_h = TITLE + rows * H + (rows + 1) * GAP
                _set_geometry(sub, GAP, y_in, sub_w, sub_h)
                for i, comp in enumerate(comps):
                    row_i, col = divmod(i, COLS)
                    row_items = min(len(comps) - row_i * COLS, COLS)
                    row_w = row_items * W + (row_items - 1) * GAP
                    cx = (sub_w - row_w) / 2 + col * (W + GAP)
                    cy = TITLE + GAP + row_i * (H + GAP)
                    _set_geometry(comp, cx, cy, W, H)
                y_in += sub_h + GAP
        layer_h = max(y_in, TITLE + 2 * GAP)
        _set_geometry(layer, X0, y, layer_w, layer_h)
        y += layer_h + GAP

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _resolve_container(
    cell: ET.Element,
    by_id: dict[str, ET.Element],
    layer_ids: set[str],
    sub_ids: set[str],
) -> str | None:
    """沿 parent 链向上找最近的 层容器/子容器 id；找不到返回 None。"""
    parent = cell.get("parent")
    seen: set[str] = set()
    while parent is not None and parent not in layer_ids and parent not in sub_ids:
        if parent in ("0", "1") or parent in seen:
            return None
        seen.add(parent)
        ancestor = by_id.get(parent)
        if ancestor is None:
            return None
        parent = ancestor.get("parent")
    return parent


def _set_geometry(cell: ET.Element, x: float, y: float, w: float, h: float) -> None:
    """覆盖写入 mxCell 的 mxGeometry 子元素。"""
    for old in cell.findall("mxGeometry"):
        cell.remove(old)
    geo = ET.SubElement(cell, "mxGeometry")
    geo.set("x", _num(x))
    geo.set("y", _num(y))
    geo.set("width", _num(w))
    geo.set("height", _num(h))
    geo.set("as", "geometry")


def _num(v: float) -> str:
    """整数不带小数点，保持 XML 干净。"""
    return str(int(v)) if v == int(v) else str(round(v, 2))

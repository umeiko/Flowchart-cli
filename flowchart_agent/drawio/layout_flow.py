"""draw.io 流程图的确定性布局器（与 drawio/layout.py 架构图布局器并列的后处理管线）。

drawio 引擎画流程图时，LLM 只输出结构（节点 + 带 source/target 的连线 +
颜色样式），不写坐标；本模块按图结构确定性计算布局并注入 mxGeometry：
- 分层：从入度为 0 的起点做最长路分层（自上而下，一层一行）；
- 层内排序：barycenter 启发式（两遍向下 + 一遍向上），平局保持 XML 书写
  顺序——判断节点先写的分支排左、后写的排右；
- 坐标：所有节点严格等大（W×H），每层水平居中于最宽层；
- 连线：强制规范化为直角正交走线（orthogonalEdgeStyle;rounded=0），
  按相对位置写 exit/entry 附着点，模型写的走线样式与几何一律清除。

环（回退边）不参与分层：拓扑排序残留的节点按已分层前驱的最大层 +1 落位。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# 网格常量（节点宽与 prompts/drawio.py 流程图模板的换行说明保持一致）
W = 220        # 节点宽
H = 70         # 节点高
GAP_X = 60     # 层内节点水平间距（给分支标签留位）
GAP_Y = 60     # 层间垂直间距
X0 = 40        # 整图左边距
Y0 = 40        # 整图顶边距

_EDGE_STYLE = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor=#666666;"


def apply_flow_layout(xml_text: str) -> str:
    """给不带几何信息的流程图 mxfile XML 注入计算好的布局，返回新 XML。"""
    root = ET.fromstring(xml_text)
    model = root.find("diagram/mxGraphModel")
    if model is None:
        raise ValueError("XML 中未找到 diagram/mxGraphModel")
    cells = model.find("root")
    if cells is None:
        raise ValueError("XML 中未找到 root 元素")

    vertices = [c for c in cells if c.get("vertex") == "1"]
    edges = [
        c for c in cells
        if c.get("edge") == "1" and c.get("source") and c.get("target")
    ]
    if not vertices:
        raise ValueError("XML 中没有节点（vertex）")
    if not edges:
        raise ValueError("流程图没有连线（edge）——流程图必须用带 source/target "
                         "的连线表达步骤先后与分支")

    ids = {c.get("id") for c in vertices}
    by_id = {c.get("id"): c for c in vertices}
    # 丢弃指向不存在节点的边（模型笔误），避免后续 KeyError
    edges = [e for e in edges if e.get("source") in ids and e.get("target") in ids]
    if not edges:
        raise ValueError("流程图没有有效连线（source/target 必须指向已定义的节点 id）")

    preds: dict[str, list[str]] = {i: [] for i in ids}
    succs: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        succs[s].append(t)
        preds[t].append(s)

    order = [c.get("id") for c in vertices]  # XML 书写顺序（平局时的稳定次序）
    layers_of = _assign_layers(ids, preds, succs, order)
    layered = _order_layers(ids, layers_of, preds, succs, order)
    pos = _positions(layered)

    for node_id, (x, y) in pos.items():
        _set_geometry(by_id[node_id], x, y)
    for e in edges:
        _normalize_edge(e, pos)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _assign_layers(
    ids: set[str],
    preds: dict[str, list[str]],
    succs: dict[str, list[str]],
    order: list[str],
) -> dict[str, int]:
    """最长路分层：layer[v] = max(layer[u] + 1)，入度 0 的起点在第 0 层。

    Kahn 拓扑；有环时残留节点按已分层前驱的最大层 +1 落位（回边不抬高层级）。
    完全孤立的节点（没有任何连线）追加到最后一层并打 warning。
    """
    layer: dict[str, int] = {}
    remaining = set(ids)
    while remaining:
        ready = [i for i in order if i in remaining and all(p in layer for p in preds[i])]
        if not ready:
            # 环：取已分层前驱最多的节点打破僵局
            node = max(remaining, key=lambda i: sum(p in layer for p in preds[i]))
            logger.warning("流程图存在环，节点「%s」按回边处理", node)
            ready = [node]
        for i in ready:
            layer[i] = max((layer[p] for p in preds[i] if p in layer), default=-1) + 1
            remaining.discard(i)
    connected = {i for i in ids if preds[i] or succs[i]}
    orphans = [i for i in order if i not in connected]
    if orphans:
        last = max(layer.values(), default=-1) + 1
        for i in orphans:
            layer[i] = last
            logger.warning("节点「%s」没有任何连线，已追加到最后一层", i)
    return layer


def _order_layers(
    ids: set[str],
    layer_of: dict[str, int],
    preds: dict[str, list[str]],
    succs: dict[str, list[str]],
    order: list[str],
) -> list[list[str]]:
    """按层分组并用 barycenter 启发式排序（减少交叉，分支自然分左右）。"""
    n_layers = max(layer_of.values()) + 1
    layered: list[list[str]] = [[] for _ in range(n_layers)]
    for i in order:  # 初始顺序 = XML 书写顺序
        layered[layer_of[i]].append(i)

    def barycenter(ids_: list[str], neighbors: dict[str, list[str]],
                   ref_pos: dict[str, int]) -> list[str]:
        def key(i: str) -> tuple[float, int]:
            refs = [ref_pos[p] for p in neighbors[i] if p in ref_pos]
            return (sum(refs) / len(refs), order.index(i)) if refs else (float("inf"), order.index(i))
        return sorted(ids_, key=key)

    for _ in range(2):  # 两遍向下 + 一遍向上
        for lv in range(1, n_layers):
            ref = {n: k for k, n in enumerate(layered[lv - 1])}
            layered[lv] = barycenter(layered[lv], preds, ref)
        for lv in range(n_layers - 2, -1, -1):
            ref = {n: k for k, n in enumerate(layered[lv + 1])}
            layered[lv] = barycenter(layered[lv], succs, ref)
    return layered


def _positions(layered: list[list[str]]) -> dict[str, tuple[float, float]]:
    """每层水平居中于最宽层，节点严格等大。"""
    max_w = max(len(lv) * W + (len(lv) - 1) * GAP_X for lv in layered)
    pos: dict[str, tuple[float, float]] = {}
    for lv, nodes in enumerate(layered):
        row_w = len(nodes) * W + (len(nodes) - 1) * GAP_X
        x = X0 + (max_w - row_w) / 2
        y = Y0 + lv * (H + GAP_Y)
        for n in nodes:
            pos[n] = (x, y)
            x += W + GAP_X
    return pos


def _set_geometry(cell: ET.Element, x: float, y: float) -> None:
    """覆盖写入节点的 mxGeometry（先清掉模型可能写的旧几何）。"""
    for old in cell.findall("mxGeometry"):
        cell.remove(old)
    geo = ET.SubElement(cell, "mxGeometry")
    geo.set("x", _num(x))
    geo.set("y", _num(y))
    geo.set("width", str(W))
    geo.set("height", str(H))
    geo.set("as", "geometry")


def _normalize_edge(edge: ET.Element, pos: dict[str, tuple[float, float]]) -> None:
    """规范化连线：强制直角正交走线，按相对位置写 exit/entry 附着点。"""
    sx, sy = pos[edge.get("source")]
    tx, ty = pos[edge.get("target")]
    dx, dy = tx - sx, ty - sy
    if dy > 0:  # 向下：默认下出上进；左右分叉时从侧边出
        exit_p = (0.5, 1) if abs(dx) < 1 else ((1, 0.5) if dx > 0 else (0, 0.5))
        entry_p = (0.5, 0)
    elif dy < 0:  # 回边：从侧边出、侧边进，避免穿过中间节点
        exit_p = entry_p = (1, 0.5) if dx >= 0 else (0, 0.5)
    else:  # 同层：下出下进
        exit_p = entry_p = (0.5, 1)
    style = _EDGE_STYLE + (
        f"exitX={exit_p[0]};exitY={exit_p[1]};exitDx=0;exitDy=0;"
        f"entryX={entry_p[0]};entryY={entry_p[1]};entryDx=0;entryDy=0;"
    )
    if edge.get("value"):
        style += "fontColor=#3E4144;fontSize=12;"
    edge.set("style", style)
    for old in edge.findall("mxGeometry"):
        edge.remove(old)
    geo = ET.SubElement(edge, "mxGeometry")
    geo.set("relative", "1")
    geo.set("as", "geometry")


def _num(v: float) -> str:
    """整数不带小数点，保持 XML 干净。"""
    return str(int(v)) if v == int(v) else str(round(v, 2))

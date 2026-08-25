"""draw.io 流程图的确定性布局器（与 drawio/layout.py 架构图布局器并列的后处理管线）。

drawio 引擎画流程图时，LLM 只输出结构（节点 + 带 source/target 的连线 +
颜色样式），不写坐标；本模块按图结构确定性计算布局并注入 mxGeometry：
- 分层：从入度为 0 的起点做最长路分层（自上而下，一层一行）；
- 层内排序：barycenter 启发式（两遍向下 + 一遍向上），平局保持 XML 书写
  顺序——判断节点先写的分支排左、后写的排右；
- 坐标：所有节点严格等大（W×H），每层水平居中于最宽层；「结束/终止/End」
  类无出边终节点强制沉底（最末层被流程步骤占用时独占新起一行）；
- 连线：强制规范化为直角正交走线（orthogonalEdgeStyle;rounded=0），
  按相对位置写 exit/entry 附着点，模型写的走线样式与几何一律清除；
  路径由确定性通道路由计算（draw.io 的正交路由器不规避障碍物，
  跳边会穿刺中间节点）：在列间隙/左右页边找垂直通道，绕不开的边
  以 Z/U 形显式拐点（mxPoint）绕行，已布线段占用车道防止线线重叠
  （同向共边的锚点短桩允许总线共享，反向不共享）；判断节点（rhombus）
  的多条出边按 [朝目标侧→底边→另一侧] 动态尝试且不共享车道——
  是/否必然异侧出行；汇合点（入度≥2）的下行入边优先底出连成总线。

环（回退边）不参与分层：拓扑排序残留的节点按已分层前驱的最大层 +1 落位。
"""

from __future__ import annotations

import logging
import re
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

# 通道路由常量
_PAD = 4       # 判定线段是否穿节点时的包围盒外扩
_LANE = 12     # 同一通道内并行走线的车道间距
_MIN_GAP = 28  # 可作为走线通道的最小列间隙
_MARGIN = 30   # 左右页边通道距最外节点的距离
_MERGE = 8     # 共线判定阈值（间距小于此值的同向线段视为重叠）


class _Router:
    """确定性通道路由：为每条边算绕开节点的直角拐点。

    候选垂直通道 = 节点包围盒在 x 轴上的间隙中点（含 ±车道偏移）+
    左右页边。每条边的路径逐段做两种检查：
    - 不与任何节点包围盒内部相交（与目标节点相连的末段豁免目标自身）；
    - 不与已提交线段共线重叠（垂直交叉是正常走线，允许；同向共边在
      节点邻域内的重叠视为总线共享——但判断节点（decisions）不豁免，
      是/否分支必须从不同的边出去，走向才明确）。
    """

    def __init__(self, pos: dict[str, tuple[float, float]],
                 decisions: set[str] | None = None):
        self.decisions = decisions or set()
        self.rects = {i: (x, y, x + W, y + H) for i, (x, y) in pos.items()}
        bounds = sorted({v for r in self.rects.values() for v in (r[0], r[2])})
        self.xlo, self.xhi = bounds[0], bounds[-1]
        self.cands: list[float] = []
        for a, b in zip(bounds, bounds[1:]):
            if b - a >= _MIN_GAP:
                mid = (a + b) / 2
                for off in (0, -_LANE, _LANE, -2 * _LANE, 2 * _LANE):
                    x = mid + off
                    if a + _PAD + 2 <= x <= b - _PAD - 2:
                        self.cands.append(x)
        for off in (0, _LANE, 2 * _LANE):
            self.cands.append(self.xlo - _MARGIN - off)
            self.cands.append(self.xhi + _MARGIN + off)
        self.committed: list[tuple[float, float, float, float, str, str]] = []

    # ---- 几何判定 ----

    @staticmethod
    def _hits_rect(seg: tuple, rect: tuple, pad: float = _PAD) -> bool:
        x1, y1, x2, y2 = seg
        l, t, r, b = rect
        if x1 == x2:  # 垂直段
            ylo, yhi = sorted((y1, y2))
            return l + pad < x1 < r - pad and yhi > t + pad and ylo < b - pad
        if y1 == y2:  # 水平段
            xlo, xhi = sorted((x1, x2))
            return t + pad < y1 < b - pad and xhi > l + pad and xlo < r - pad
        return True  # 非轴对齐段不合法，视为阻挡

    def _clear(self, seg: tuple, exclude: tuple = ()) -> bool:
        """线段不穿过任何（未被豁免的）节点内部。"""
        return not any(
            i not in exclude and self._hits_rect(seg, r)
            for i, r in self.rects.items()
        )

    def _vicinity(self, i: str) -> tuple[float, float]:
        """节点的邻域 y 区间（自身 + 上下各一个行间隙带）。

        同向共边（committed 边与本边同源=都离开、或同目标=都进入）在此
        区间内的共线重叠视为锚点短桩共享（总线）；反向共边不豁免——
        方向相反的两个箭头不允许在同一条边上重叠。
        """
        r = self.rects[i]
        return (r[1] - GAP_Y, r[3] + GAP_Y)

    def _free_lane(self, seg: tuple, s: str, t: str) -> bool:
        """线段不与已提交线段共线重叠（允许垂直交叉与锚点短桩共享）。"""
        x1, y1, x2, y2 = seg
        for a, b, c, d, cs, ct in self.committed:
            if x1 == x2 and a == c and abs(x1 - a) < _MERGE:
                lo = max(min(y1, y2), min(b, d))
                hi = min(max(y1, y2), max(b, d))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if (shared is not None and shared not in self.decisions
                        and _inside(self._vicinity(shared), lo, hi)):
                    continue
                return False
            elif y1 == y2 and b == d and abs(y1 - b) < _MERGE:
                lo = max(min(x1, x2), min(a, c))
                hi = min(max(x1, x2), max(a, c))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if shared is not None and shared not in self.decisions:
                    z = self._vicinity(shared)
                    if z[0] <= y1 <= z[1]:
                        continue
                return False
        return True

    def _ok(self, segs: list[tuple[tuple, tuple]], s: str, t: str) -> bool:
        return all(
            self._clear(seg, ex) and self._free_lane(seg, s, t) for seg, ex in segs
        )

    def _penalty(self, cx: float) -> int:
        """页边通道加罚分：有内部通道可用时不贴边绕远。"""
        return 0 if self.xlo <= cx <= self.xhi else 400

    @staticmethod
    def _len(pts: list[tuple]) -> float:
        return sum(abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in zip(pts, pts[1:]))

    def commit(self, pts: list[tuple], s: str, t: str) -> None:
        """把最终路径的各段登记为占用车道。"""
        for p, q in zip(pts, pts[1:]):
            if p != q:
                self.committed.append((p[0], p[1], q[0], q[1], s, t))

    # ---- 各类走线（统一返回 (拐点, exit 附着点, entry 附着点)，失败返回 None）----

    def route_down_bottom(self, s: str, t: str) -> tuple[list, tuple, tuple] | None:
        """底出顶进：同列优先直连，否则经行间隙带 Z/U 形绕行。"""
        sr, tr = self.rects[s], self.rects[t]
        x0, x1 = (sr[0] + sr[2]) / 2, (tr[0] + tr[2]) / 2
        p0, p1 = (x0, sr[3]), (x1, tr[1])
        y0 = sr[3] + GAP_Y / 2  # 源节点下方的行间空隙带（竖直方向无节点）
        y1 = tr[1] - GAP_Y / 2
        best = None
        seen = set()
        for cx in [x0, x1, *self.cands]:
            if cx in seen:
                continue
            seen.add(cx)
            pts = [p0, (x0, y0), (cx, y0), (cx, y1), (x1, y1), p1]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            if self._ok(_segs_of(pts, t), s, t):
                wps = [] if len({p[0] for p in pts}) == 1 else pts[1:-1]
                cand = (self._len(pts) + self._penalty(cx), wps)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is None:
            return None
        return best[1], (0.5, 1), (0.5, 0)

    def route_down_side(self, s: str, t: str,
                        outward: int | None = None) -> tuple[list, tuple, tuple] | None:
        """下行且目标在侧方：从侧边出，经垂直通道，从顶/侧进。

        outward：强制出口侧（1 右 / -1 左）；None 时按目标方位自动选。
        """
        sr, tr = self.rects[s], self.rects[t]
        if outward is None:
            outward = 1 if tr[0] >= sr[0] else -1
        exit_p = (1, 0.5) if outward > 0 else (0, 0.5)
        exit_x = sr[2] if outward > 0 else sr[0]
        exit_y = (sr[1] + sr[3]) / 2
        entry_cx = (tr[0] + tr[2]) / 2  # 目标顶边中心：最优通道，可免末段横移折叠
        best = None
        seen = set()
        for cx in [entry_cx, *self.cands]:
            if cx in seen:
                continue
            seen.add(cx)
            if tr[0] - 2 <= cx <= tr[2] + 2:  # 通道在目标正上方：从顶进
                # 横移在目标上方的行间空隙带完成，末段竖直落入顶边中心，
                # 保证箭头朝下居中（贴着顶边横移会把箭头画成侧向）
                entry_p = (0.5, 0)
                entry = ((tr[0] + tr[2]) / 2, tr[1])
                yb = tr[1] - GAP_Y / 2
                pts = [(exit_x, exit_y), (cx, exit_y), (cx, yb),
                       (entry[0], yb), entry]
            else:  # 通道在目标侧方：从朝通道的侧边进
                entry_p = (1, 0.5) if cx > tr[2] else (0, 0.5)
                entry = ((tr[2] if cx > tr[2] else tr[0]), (tr[1] + tr[3]) / 2)
                pts = [(exit_x, exit_y), (cx, exit_y), (cx, entry[1]), entry]
            if self._ok(_segs_of(pts, t), s, t):
                cand = (self._len(pts) + self._penalty(cx), pts[1:-1], entry_p)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is None:
            return None
        return best[1], exit_p, best[2]

    def route_back(self, s: str, t: str, outward: int) -> tuple[list, tuple, tuple] | None:
        """回边（目标在上方）。两种模板，全局取最短无遮挡：

        A. 侧出侧进：经外侧页边/间隙通道，左右两侧都试（反向边不允许
           与进边共享顶边短桩，自然侧被占时从另一侧绕行）；出/入口
           附着点可在侧边上纵向偏移（0.5/0.3/0.7），避开目标侧边上
           已占用的反向短桩；
        B. 顶出侧进：从源节点顶边垂直上行，经通道转入目标朝通道的侧边
           （同排邻居挡住侧出水平段时的退路）。
        """
        sr, tr = self.rects[s], self.rects[t]
        best = None

        # A. 侧出侧进（两侧 × 附着点偏移）
        for side in (outward, -outward):
            exit_x = sr[2] if side > 0 else sr[0]
            entry_x = tr[2] if side > 0 else tr[0]
            for exf in (0.5, 0.3, 0.7):
                for enf in (0.5, 0.3, 0.7):
                    exit_p = (1 if side > 0 else 0, exf)
                    entry_p = (1 if side > 0 else 0, enf)
                    exit_y = sr[1] + exf * H
                    entry_y = tr[1] + enf * H
                    for cx in self.cands:
                        limit = (max(exit_x, entry_x) + 2 if side > 0
                                 else min(exit_x, entry_x) - 2)
                        if side > 0 and cx < limit:
                            continue
                        if side < 0 and cx > limit:
                            continue
                        pts = [(exit_x, exit_y), (cx, exit_y),
                               (cx, entry_y), (entry_x, entry_y)]
                        if self._ok(_segs_of(pts, t), s, t):
                            cand = (self._len(pts) + self._penalty(cx),
                                    pts[1:-1], exit_p, entry_p)
                            if best is None or cand[0] < best[0]:
                                best = cand

        # B. 顶出侧进
        x0 = (sr[0] + sr[2]) / 2
        yb = sr[1] - GAP_Y / 2  # 源节点上方的行间空隙带（竖直方向无节点）
        ey = (tr[1] + tr[3]) / 2
        seen = set()
        for cx in [x0, *self.cands]:
            if cx in seen or tr[0] - 2 <= cx <= tr[2] + 2:
                continue  # 通道落在目标正上方时侧进会横穿目标，跳过
            seen.add(cx)
            entry_p = (1, 0.5) if cx > tr[2] else (0, 0.5)
            entry_x = tr[2] if cx > tr[2] else tr[0]
            pts = [(x0, sr[1]), (x0, yb), (cx, yb), (cx, ey), (entry_x, ey)]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            if self._ok(_segs_of(pts, t), s, t):
                cand = (self._len(pts) + self._penalty(cx), pts[1:-1],
                        (0.5, 0), entry_p)
                if best is None or cand[0] < best[0]:
                    best = cand

        if best is None:
            return None
        return best[1], best[2], best[3]


def _inside(zone: tuple[float, float], lo: float, hi: float) -> bool:
    return zone[0] <= lo and hi <= zone[1]


def _segs_of(pts: list[tuple], t: str) -> list[tuple[tuple, tuple]]:
    """把点列拆成 (线段, 豁免节点)：末段豁免 target（贴边进入不算穿越）。

    首段不豁免 source——从源节点边框出发后不允许再穿回源节点内部。
    """
    n = len(pts)
    out = []
    for i, (p, q) in enumerate(zip(pts, pts[1:])):
        if p == q:
            continue
        ex = (t,) if i == n - 2 else ()
        out.append(((p[0], p[1], q[0], q[1]), ex))
    return out


def _anchor(rect: tuple, p: tuple) -> tuple:
    """附着点 (比例x, 比例y) 换算成绝对坐标。"""
    return (rect[0] + p[0] * W, rect[1] + p[1] * H)


_TERMINAL_RE = re.compile(r"^\s*(结束|终止|end\b)", re.IGNORECASE)


def _sink_terminals_to_bottom(vertices: list[ET.Element],
                              succs: dict[str, list[str]],
                              layer: dict[str, int]) -> None:
    """名为「结束/终止/End」且无出边的终节点沉到最末层。

    主流程的终点不应出现在图中间（会让大量连线回绕）；结束节点带有
    出边属于建模错误，不强行沉底，打 warning 交给检视环节拦截。
    """
    if not layer:
        return
    terminals = {c.get("id") for c in vertices
                 if _TERMINAL_RE.match(c.get("value") or "")}
    for c in vertices:
        i = c.get("id")
        if i in terminals and succs.get(i):
            logger.warning(
                "结束类节点「%s」带有出边（不应位于流程中段），请检查建模",
                c.get("value"))
    last = max(layer.values())
    # 最末层被非终节点占用时（如循环体比主流程更深），终节点独占新起的一行，
    # 不与流程步骤并排——结束节点必须在图的最底部且独占一行
    if any(l == last and i not in terminals for i, l in layer.items()):
        last += 1
    for i in terminals:
        if not succs.get(i):
            layer[i] = last


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
    _sink_terminals_to_bottom(vertices, succs, layers_of)
    layered = _order_layers(ids, layers_of, preds, succs, order)
    pos = _positions(layered)

    decisions = {c.get("id") for c in vertices if "rhombus" in (c.get("style") or "")}
    router = _Router(pos, decisions)
    join_nodes = {i for i in ids if len(preds[i]) >= 2}
    for node_id, (x, y) in pos.items():
        _set_geometry(by_id[node_id], x, y)
    # 按曼哈顿距离升序路由：短边/同列直连先占位，长跳边后绕——
    # 否则跳边先占了判断节点的底边出口，同列直连的分支反而被迫侧出
    edges_sorted = sorted(
        edges,
        key=lambda e: abs(pos[e.get("target")][0] - pos[e.get("source")][0])
        + abs(pos[e.get("target")][1] - pos[e.get("source")][1]),
    )
    for e in edges_sorted:
        _normalize_edge(e, router, join_nodes)

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


def _normalize_edge(edge: ET.Element, router: _Router,
                    join_nodes: set[str] | None = None) -> None:
    """规范化连线：强制直角正交走线，算好附着点与绕障拐点。

    join_nodes：入度 ≥2 的汇合节点——其下行入边优先底出顶进，
    在汇合带上连成总线后单箭头落入（侧出会形成难看的并行缺口）。

    判断节点（rhombus）的下行出边按 [朝目标侧 → 底边 → 另一侧] 的顺序
    动态尝试；判断节点不享受车道共享豁免，已被兄弟分支占用的出口侧
    会因短桩冲突自动失败——是/否必然异侧出行，走向明确。
    """
    s, t = edge.get("source"), edge.get("target")
    sr, tr = router.rects[s], router.rects[t]
    dx, dy = tr[0] - sr[0], tr[1] - sr[1]
    wps: list[tuple] = []
    if dy > 0:  # 向下：同列底出顶进；左右分叉优先侧边出，侧出无解再底出绕行
        if abs(dx) < 1:
            tries = ["bottom", "right", "left"] if s in router.decisions else ["bottom"]
        else:
            pref = "right" if dx > 0 else "left"
            other = "left" if pref == "right" else "right"
            if s in router.decisions:
                tries = [pref, "bottom", other]
            elif join_nodes and t in join_nodes:
                tries = ["bottom", pref]
            else:
                tries = [pref, "bottom"]
        routed = None
        for how in tries:
            if how == "bottom":
                routed = router.route_down_bottom(s, t)
            else:
                routed = router.route_down_side(s, t, 1 if how == "right" else -1)
            if routed is not None:
                break
        if routed is None:
            logger.warning("边 %s -> %s 找不到无遮挡通道，退回 draw.io 自动走线", s, t)
            exit_p, entry_p = (0.5, 1), (0.5, 0)
        else:
            wps, exit_p, entry_p = routed
    elif dy < 0:  # 回边：从侧边出、同侧进，经外侧通道绕开中间节点
        outward = 1 if dx >= 0 else -1
        routed = router.route_back(s, t, outward)
        if routed is None:
            logger.warning("回边 %s -> %s 找不到无遮挡通道，退回 draw.io 自动走线", s, t)
            exit_p = entry_p = (1 if outward > 0 else 0, 0.5)
        else:
            wps, exit_p, entry_p = routed
    else:  # 同层：下出下进（draw.io 自动向下微绕，行间空隙带内无节点）
        exit_p = entry_p = (0.5, 1)
    # 清理连续重复拐点（通道 x 与入口 x 重合时会产生零长度段）
    pts = [_anchor(sr, exit_p), *wps, _anchor(tr, entry_p)]
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    wps = pts[1:-1]
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
    if wps:
        arr = ET.SubElement(geo, "Array")
        arr.set("as", "points")
        for x, y in wps:
            pt = ET.SubElement(arr, "mxPoint")
            pt.set("x", _num(x))
            pt.set("y", _num(y))
    router.commit(pts, s, t)


def _num(v: float) -> str:
    """整数不带小数点，保持 XML 干净。"""
    return str(int(v)) if v == int(v) else str(round(v, 2))

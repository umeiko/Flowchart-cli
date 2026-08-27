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
  非判断节点的四条边支持双车道：反向箭头（一进一出）抢中点时，
  已布在中点的箭头迁到该边的 1/3 三等分点，新箭头走 2/3（侧边上
  远端在上的走 1/3，顶/底边上远端在左的走 1/3），避免两条反向
  短桩叠在同一条边上。

环（回退边）不参与分层：拓扑排序残留的节点按已分层前驱的最大层 +1 落位。

调试：排查走线问题不要直接改本文件试错——先用 scripts/route_report.py
手动触发一次完整路由并生成逐边报告（复用 _prepare(router_cls=...) 注入
插桩路由器，输出每条边各模板尝试的成败与失败原因）：
    uv run python scripts/route_report.py <file.drawio> [--render]
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
      节点邻域内的重叠视为总线共享——但判断节点（decisions）的出边不
      豁免，是/否分支必须从不同的边出去，走向才明确；进入判断节点的
      入边照常豁免，多路汇合后单箭头落入与普通节点一致）。

    双车道迁移：非判断节点的同一条边（左/右/顶/底），优先单车道
    （附着中点 0.5）；仅当中点被一条反向短桩（一进一出）挡住时，
    把对方迁到 1/3 三等分点、本边走 2/3。菱形周界求交只认顶点，
    不参与迁移。
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
        # 已提交线段：(x1, y1, x2, y2, source, target, edge_id)
        self.committed: list[tuple[float, float, float, float, str, str, str]] = []
        # 每条边的路由记录：双车道迁移时要回改它的附着点/拐点并重写 XML
        self.routes: dict[str, dict] = {}
        # 各边附着登记：(node, 'L'/'R'/'T'/'B') -> [(edge_id, 'exit'/'entry', 沿边比例)]
        self.side_use: dict[tuple[str, str], list[tuple[str, str, float]]] = {}
        self.migrations: list[str] = []  # 双车道迁移记录（供走线报告输出）
        # 已放置的边 label 文字盒 (x1, y1, x2, y2)：新 label 只在与之相叠时才挪位
        self.label_boxes: list[tuple[float, float, float, float]] = []
        self.degraded: list[str] = []  # 车道耗尽被降级为矩形的判断节点
        self.node_cells: dict = {}  # 节点 id -> mxCell（降级时改写 shape）

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
        方向相反的两个箭头不允许在同一条边上重叠。判断节点只限制
        "都离开"的总线（是/否分支必须异侧），"都进入"照常豁免。
        """
        r = self.rects[i]
        return (r[1] - GAP_Y, r[3] + GAP_Y)

    def _lane_conflicts(self, seg: tuple, s: str, t: str) -> list[tuple]:
        """与 seg 共线重叠且不被豁免的已提交线段（垂直交叉是正常走线，不算）。"""
        x1, y1, x2, y2 = seg
        out = []
        for rec in self.committed:
            a, b, c, d, cs, ct = rec[:6]
            if x1 == x2 and a == c and abs(x1 - a) < _MERGE:
                lo = max(min(y1, y2), min(b, d))
                hi = min(max(y1, y2), max(b, d))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if (shared is not None
                        and (ct == t or shared not in self.decisions)
                        and _inside(self._vicinity(shared), lo, hi)):
                    continue
                out.append(rec)
            elif y1 == y2 and b == d and abs(y1 - b) < _MERGE:
                lo = max(min(x1, x2), min(a, c))
                hi = min(max(x1, x2), max(a, c))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if (shared is not None
                        and (ct == t or shared not in self.decisions)):
                    z = self._vicinity(shared)
                    if z[0] <= y1 <= z[1]:
                        continue
                out.append(rec)
        return out

    def _free_lane(self, seg: tuple, s: str, t: str) -> bool:
        """线段不与已提交线段共线重叠（允许垂直交叉与锚点短桩共享）。"""
        return not self._lane_conflicts(seg, s, t)

    @staticmethod
    def _side_of(p: tuple) -> str | None:
        """附着点所在的节点边：'L'/'R'/'T'/'B'；角点以外的组合不会出现。"""
        if p[0] in (0, 1):
            return "R" if p[0] == 1 else "L"
        if p[1] in (0, 1):
            return "B" if p[1] == 1 else "T"
        return None

    @staticmethod
    def _frac_of(p: tuple, side: str) -> float:
        """附着点在该边上的归一化位置（侧边取纵向比例，顶/底边取横向）。"""
        return p[1] if side in ("L", "R") else p[0]

    def _attach_ok(self, s: str, t: str, exit_p: tuple, entry_p: tuple) -> bool:
        """新附着点与同边反向箭头保持车道间距。

        同一条边（左/右/顶/底）上方向相反（一进一出）的两个箭头，沿边
        间距必须 ≥ _LANE；同向箭头不受限（总线共享刻意叠在同一点）。
        这条规则同时挡住"中点塞进两条三等分车道之间"（侧边上 0.5 距
        1/3 仅 11.7px）的第三辆车。
        """
        for node, p, end in ((s, exit_p, "exit"), (t, entry_p, "entry")):
            side = self._side_of(p)
            if side is None:
                continue
            f, span = (p[1], H) if side in ("L", "R") else (p[0], W)
            for _eid, end2, f2 in self.side_use.get((node, side), []):
                if end2 != end and abs(f2 - f) * span < _LANE - 1e-6:
                    return False
        return True

    # ---- 双车道迁移：非判断节点某条边上反向箭头（一进一出）抢中点时的退让 ----

    def degrade(self, n: str) -> None:
        """菱形四顶点车道耗尽的兜底：把判断节点降级为矩形（保留配色）。

        已就位的菱形顶点附着在矩形周界上依然有效，无需回改存量边；
        后续经过该节点的边按普通节点走（分数附着点 + 双车道迁移 +
        总线共享豁免），车道数显著多于菱形。
        """
        self.decisions.discard(n)
        self.degraded.append(n)
        cell = self.node_cells.get(n)
        if cell is not None:
            style = cell.get("style") or ""
            cell.set("style", ";".join(
                "rounded=1" if tok == "rhombus" else tok
                for tok in style.split(";")))

    def _migration_plan(self, pts: list[tuple], s: str, t: str,
                        exit_p: tuple, entry_p: tuple) -> tuple | None:
        """路径仅被一条可迁移的反向短桩挡住时返回迁移方案，否则 None。

        可迁移 = 唯一障碍是本边首段（出 source）或末段（进 target）上的
        一处车道冲突，且冲突段是对方在同节点同一边的短桩：对方在该边
        有且仅有这一条附着、停在中点 0.5、方向与本边相反、节点非菱形。
        返回 (对方 edge_id, 节点, 边, 本边端别, 对方端别, 对方新比例, 本边新比例)
        ——两个箭头各退到该边的 1/3 与 2/3 三等分点：侧边上远端在上的
        走 1/3，顶/底边上远端在左的走 1/3，减少两条短桩交叉。
        """
        segs = _segs_of(pts, t)
        if not all(self._clear(seg, ex) for seg, ex in segs):
            return None
        confs = [(i, rec) for i, (seg, _ex) in enumerate(segs)
                 for rec in self._lane_conflicts(seg, s, t)]
        if len(confs) != 1:
            return None
        i, rec = confs[0]
        seg = segs[i][0]
        # 首段对应出 source、末段对应进 target；直连路径首末同段，两端都试
        ends = []
        if i == 0:
            ends.append((s, "exit", exit_p))
        if i == len(segs) - 1:
            ends.append((t, "entry", entry_p))
        for n, end, p in ends:
            plan = self._plan_at(rec, seg, n, self._side_of(p), end, s, t)
            if plan is not None:
                return plan
        return None  # 中段冲突不是附着点问题，迁移解决不了

    def _plan_at(self, rec: tuple, seg: tuple, n: str, side: str | None,
                 end: str, s: str, t: str) -> tuple | None:
        """_migration_plan 的单端版本：冲突能否归因到 (n, side) 上的中点反向短桩。"""
        if n in self.decisions or side is None:
            return None
        if side in ("L", "R"):
            if seg[1] != seg[3]:  # 侧边短桩应是水平段
                return None
        elif seg[0] != seg[2]:  # 顶/底边短桩应是竖直段
            return None
        use = self.side_use.get((n, side), [])
        ceid = rec[6]
        if len(use) != 1 or use[0][0] != ceid:
            return None  # 该边已有两条及以上附着：最多双车道，不再加塞
        _id, cend, cf = use[0]
        if cend == end or cf != 0.5:
            return None  # 同向共边归总线豁免管；对方不在中点无从迁移
        crec = self.routes.get(ceid)
        if crec is None:
            return None
        cpts = crec["pts"]
        stub = (cpts[0], cpts[1]) if cend == "exit" else (cpts[-2], cpts[-1])
        if tuple(rec[:4]) != (stub[0][0], stub[0][1], stub[1][0], stub[1][1]):
            return None  # 冲突的是对方路径中段，迁它的附着点解决不了
        # 远端在上（侧边比 y）/在左（顶底边比 x）的箭头走 1/3；平局入边占 1/3
        our_far = self.rects[t if end == "exit" else s]
        their_far = self.rects[crec["t"] if cend == "exit" else crec["s"]]
        if side in ("L", "R"):
            our_k = (our_far[1] + our_far[3]) / 2
            their_k = (their_far[1] + their_far[3]) / 2
        else:
            our_k = (our_far[0] + our_far[2]) / 2
            their_k = (their_far[0] + their_far[2]) / 2
        if our_k != their_k:
            f_our, f_their = (1 / 3, 2 / 3) if our_k < their_k else (2 / 3, 1 / 3)
        else:
            f_our, f_their = (1 / 3, 2 / 3) if end == "entry" else (2 / 3, 1 / 3)
        return (ceid, n, side, end, cend, f_their, f_our)

    def _apply_migration(self, plan: tuple, pts: list[tuple], s: str, t: str,
                         exit_p: tuple, entry_p: tuple):
        """执行双车道迁移，成功返回本边的 (拐点, exit, entry)，失败回滚返回 None。

        双方短桩分别平移到两个三等分点后整路径重验（对方换车道后可能与
        更晚提交的线段相碰，必须重跑穿节点 + 车道检查，不过则完整回滚）。
        """
        ceid, n, side, end, cend, f_their, f_our = plan
        crec = self.routes[ceid]
        rect = self.rects[n]
        horiz = side in ("L", "R")
        # 短桩贴边坐标：侧边为 x，顶/底边为 y
        anchor = (rect[2] if side == "R" else rect[0]) if horiz \
            else (rect[3] if side == "B" else rect[1])

        def _shift(old_pts: list[tuple], old_p: tuple, which: str, f: float):
            """把 old_pts 的 which 端（exit=首段 / entry=末段）短桩平移到比例 f。

            相邻段与短桩垂直时拐角直接并入相邻段（少一次折返）；共线时
            （直连/折叠路径）保留原拐点、补一个短折角，保证全程轴对齐。
            """
            if len(old_pts) < 2:
                return None
            off = (rect[1] + f * H) if horiz else (rect[0] + f * W)
            new_p = (old_p[0], f) if horiz else (f, old_p[1])
            if which == "exit":
                first, nxt = old_pts[0], old_pts[1]
                if horiz:
                    if nxt[1] != first[1] or first[0] != anchor:
                        return None
                    new_first, corner = (anchor, off), (nxt[0], off)
                    collinear = len(old_pts) > 2 and old_pts[2][1] == nxt[1]
                else:
                    if nxt[0] != first[0] or first[1] != anchor:
                        return None
                    new_first, corner = (off, anchor), (off, nxt[1])
                    collinear = len(old_pts) > 2 and old_pts[2][0] == nxt[0]
                new = [new_first, corner, *([nxt] if collinear else []),
                       *old_pts[2:]]
            else:
                prev, last = old_pts[-2], old_pts[-1]
                if horiz:
                    if prev[1] != last[1] or last[0] != anchor:
                        return None
                    corner, new_last = (prev[0], off), (anchor, off)
                    collinear = len(old_pts) > 2 and old_pts[-3][1] == prev[1]
                else:
                    if prev[0] != last[0] or last[1] != anchor:
                        return None
                    corner, new_last = (off, prev[1]), (off, anchor)
                    collinear = len(old_pts) > 2 and old_pts[-3][0] == prev[0]
                new = [*(old_pts[:-1] if collinear else old_pts[:-2]),
                       corner, new_last]
            new = [p for i, p in enumerate(new) if i == 0 or p != new[i - 1]]
            return new, new_p

        ours = _shift(pts, exit_p if end == "exit" else entry_p, end, f_our)
        theirs = _shift(crec["pts"],
                        crec["exit_p"] if cend == "exit" else crec["entry_p"],
                        cend, f_their)
        if ours is None or theirs is None:
            return None
        our_pts, our_p = ours
        their_pts, their_p = theirs

        # 试迁移：先摘掉对方旧线段并重验对方新路径，再把对方在侧边登记
        # 挪到新比例（否则本边验证间距时看到的还是旧中点），最后验本边
        old_segs = [r for r in self.committed if r[6] == ceid]
        self.committed = [r for r in self.committed if r[6] != ceid]
        cs, ct = crec["s"], crec["t"]
        ok = all(self._clear(seg, ex) and self._free_lane(seg, cs, ct)
                 for seg, ex in _segs_of(their_pts, ct))
        our_exit, our_entry = exit_p, entry_p
        if ok:
            for p, q in zip(their_pts, their_pts[1:]):
                if p != q:
                    self.committed.append((p[0], p[1], q[0], q[1], cs, ct, ceid))
            self.side_use[(n, side)][:] = [(ceid, cend, f_their)]
            if end == "exit":
                our_exit = our_p
            else:
                our_entry = our_p
            ok = self._attach_ok(s, t, our_exit, our_entry) and all(
                self._clear(seg, ex) and self._free_lane(seg, s, t)
                for seg, ex in _segs_of(our_pts, t))
        if not ok:
            self.committed = [r for r in self.committed if r[6] != ceid] + old_segs
            self.side_use[(n, side)][:] = [(ceid, cend, 0.5)]
            return None

        # 落实：更新对方路由记录，并重写它的 XML
        if cend == "exit":
            crec["exit_p"] = their_p
        else:
            crec["entry_p"] = their_p
        crec["pts"] = their_pts
        if crec.get("elem") is not None:
            crec["pts"] = _write_edge(crec["elem"], their_pts,
                                      crec["exit_p"], crec["entry_p"],
                                      label_pos=crec.get("label_pos"))
        side_name = {"L": "左", "R": "右", "T": "顶", "B": "底"}[side]
        self.migrations.append(
            f"{ceid} 在 {n} {side_name}边的"
            f"{'出口' if cend == 'exit' else '入口'} "
            f"0.5→{self._frac_of(their_p, side):.4g}（双车道）")
        return our_pts[1:-1], our_exit, our_entry

    def _ok(self, segs: list[tuple[tuple, tuple]], s: str, t: str) -> bool:
        if not all(
            self._clear(seg, ex) and self._free_lane(seg, s, t) for seg, ex in segs
        ):
            return False
        # 出/入口短桩不得短于车道宽（约一个箭头长）：更短的贴边段会被
        # 箭头整个盖住，看起来箭头方向与线身方向差 90°
        for seg, _ex in (segs[0], segs[-1]):
            if abs(seg[2] - seg[0]) + abs(seg[3] - seg[1]) < _LANE:
                return False
        return True

    def _penalty(self, cx: float) -> int:
        """页边通道加罚分：有内部通道可用时不贴边绕远。"""
        return 0 if self.xlo <= cx <= self.xhi else 400

    @staticmethod
    def _len(pts: list[tuple]) -> float:
        return sum(abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in zip(pts, pts[1:]))

    # ---- 边 label 防重叠：只在与其他 label 相叠时挪位（不躲线段/节点）----

    @staticmethod
    def _label_size(text: str) -> tuple[float, float]:
        """label 文字盒估算（fontSize=12）：CJK 全宽，其余约 0.6 倍；高 16。"""
        plain = re.sub(r"<[^>]+>", "", text)
        w = sum(12.0 if ord(c) >= 0x2E80 else 7.0 for c in plain)
        return max(w, 12.0), 16.0

    @staticmethod
    def _point_at(pts: list[tuple], dist: float) -> tuple[float, float, float, float]:
        """路径上距起点 dist 的点，及所在段的 draw.io 法向基 (nx, ny)。

        draw.io（mxGraphView.getPoint）的 label 定位：dist = (gx/2+0.5)*总长，
        垂直偏移按 (nx*gy, -ny*gy) 施加，其中 (nx, ny) = (dy/段长, dx/段长)。
        """
        total = 0.0
        for p, q in zip(pts, pts[1:]):
            seg = abs(q[0] - p[0]) + abs(q[1] - p[1])
            if seg > 0 and dist <= total + seg:
                f = (dist - total) / seg
                return (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f,
                        (q[1] - p[1]) / seg, (q[0] - p[0]) / seg)
            total += seg
        return (*pts[-1], 0.0, 0.0)

    def place_label(self, pts: list[tuple], text: str) -> tuple | None:
        """label 默认中点与其他已放置 label 相叠时，返回写入 geometry 的
        (gx, gy)；不相叠返回 None（保持 draw.io 默认，XML 不写 x/y）。

        候选按侵入度递增：先在中点做垂直微挪，再沿路径换位置。所有候选
        都躲不开时保持默认（位置至少可预期）。
        """
        total = self._len(pts)
        if total <= 0:
            return None
        w, h = self._label_size(text)

        def _box(f: float, gy: float) -> tuple:
            x, y, nx, ny = self._point_at(pts, f * total)
            cx, cy = x + nx * gy, y - ny * gy
            return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

        def _hits(box: tuple) -> bool:
            x1, y1, x2, y2 = box
            return any(x1 < b[2] + 2 and x2 + 2 > b[0]
                       and y1 < b[3] + 2 and y2 + 2 > b[1]
                       for b in self.label_boxes)

        cands = [(0.5, 0.0)] + [(0.5, gy) for gy in (12.0, -12.0, 22.0, -22.0)]
        cands += [(f, gy) for f in (0.3, 0.7, 0.15, 0.85)
                  for gy in (0.0, 12.0, -12.0, 22.0, -22.0)]
        for f, gy in cands:
            box = _box(f, gy)
            if not _hits(box):
                self.label_boxes.append(box)
                return None if (f, gy) == (0.5, 0.0) else ((f - 0.5) * 2, gy)
        self.label_boxes.append(_box(0.5, 0.0))
        return None

    def commit(self, pts: list[tuple], s: str, t: str, eid: str,
               exit_p: tuple, entry_p: tuple, elem=None, label_pos=None) -> None:
        """登记最终路径：占用车道 + 记录附着点（供双车道迁移回改）。"""
        self.routes[eid] = {"s": s, "t": t, "pts": list(pts),
                            "exit_p": exit_p, "entry_p": entry_p, "elem": elem,
                            "label_pos": label_pos}
        for p, q in zip(pts, pts[1:]):
            if p != q:
                self.committed.append((p[0], p[1], q[0], q[1], s, t, eid))
        for node, p, end in ((s, exit_p, "exit"), (t, entry_p, "entry")):
            side = self._side_of(p)
            if side is not None:
                self.side_use.setdefault((node, side), []).append(
                    (eid, end, self._frac_of(p, side)))

    # ---- 各类走线（统一返回 (拐点, exit 附着点, entry 附着点)，失败返回 None）----

    def route_down_bottom(self, s: str, t: str) -> tuple[list, tuple, tuple] | None:
        """底出顶进：同列优先直连，否则经行间隙带 Z/U 形绕行。

        正常候选全灭时做双车道迁移重试（如目标顶边中点被一条回边的
        顶出短桩反向占用，双方各退到顶边的 1/3 与 2/3）。
        """
        sr, tr = self.rects[s], self.rects[t]
        x0, x1 = (sr[0] + sr[2]) / 2, (tr[0] + tr[2]) / 2
        p0, p1 = (x0, sr[3]), (x1, tr[1])
        y0 = sr[3] + GAP_Y / 2  # 源节点下方的行间空隙带（竖直方向无节点）
        y1 = tr[1] - GAP_Y / 2
        exit_p, entry_p = (0.5, 1), (0.5, 0)
        cands = []
        seen = set()
        for cx in [x0, x1, *self.cands]:
            if cx in seen:
                continue
            seen.add(cx)
            pts = [p0, (x0, y0), (cx, y0), (cx, y1), (x1, y1), p1]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            cands.append((cx, pts))
        best = None
        for cx, pts in cands:
            if self._ok(_segs_of(pts, t), s, t) \
                    and self._attach_ok(s, t, exit_p, entry_p):
                wps = [] if len({p[0] for p in pts}) == 1 else pts[1:-1]
                cand = (self._len(pts) + self._penalty(cx), wps)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is not None:
            return best[1], exit_p, entry_p
        # 双车道迁移重试：可迁移的候选里取最短
        plans = []
        for cx, pts in cands:
            plan = self._migration_plan(pts, s, t, exit_p, entry_p)
            if plan is not None:
                plans.append((self._len(pts) + self._penalty(cx), pts, plan))
        plans.sort(key=lambda x: x[0])
        for _w, pts, plan in plans:
            routed = self._apply_migration(plan, pts, s, t, exit_p, entry_p)
            if routed is not None:
                return routed
        return None

    def route_down_side(self, s: str, t: str,
                        outward: int | None = None) -> tuple[list, tuple, tuple] | None:
        """下行且目标在侧方：从侧边出，经垂直通道，从顶/侧进。

        outward：强制出口侧（1 右 / -1 左）；None 时按目标方位自动选。
        正常候选全部失败时做双车道迁移重试：若唯一障碍是源/目标节点
        侧边上停在中点的反向短桩，把对方迁到三等分点，本边走另一个。
        """
        sr, tr = self.rects[s], self.rects[t]
        if outward is None:
            outward = 1 if tr[0] >= sr[0] else -1
        exit_p = (1, 0.5) if outward > 0 else (0, 0.5)
        exit_x = sr[2] if outward > 0 else sr[0]
        exit_y = (sr[1] + sr[3]) / 2
        entry_cx = (tr[0] + tr[2]) / 2  # 目标顶边中心：最优通道，可免末段横移折叠
        cands = []
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
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            cands.append((cx, pts, entry_p))
        best = None
        for cx, pts, entry_p in cands:
            if self._ok(_segs_of(pts, t), s, t) \
                    and self._attach_ok(s, t, exit_p, entry_p):
                cand = (self._len(pts) + self._penalty(cx), pts[1:-1], entry_p)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is not None:
            return best[1], exit_p, best[2]
        # 双车道迁移重试：可迁移的候选里取最短
        plans = []
        for cx, pts, entry_p in cands:
            plan = self._migration_plan(pts, s, t, exit_p, entry_p)
            if plan is not None:
                plans.append((self._len(pts) + self._penalty(cx), pts, entry_p, plan))
        plans.sort(key=lambda x: x[0])
        for _w, pts, entry_p, plan in plans:
            routed = self._apply_migration(plan, pts, s, t, exit_p, entry_p)
            if routed is not None:
                return routed
        return None

    def route_back(self, s: str, t: str, outward: int) -> tuple[list, tuple, tuple] | None:
        """回边（目标在上方）。三种模板，全局取最短无遮挡：

        A. 侧出侧进：经外侧页边/间隙通道，左右两侧都试（反向边不允许
           与进边共享顶边短桩，自然侧被占时从另一侧绕行）；出/入口
           附着点可在侧边上纵向偏移（0.5/0.3/0.7），避开目标侧边上
           已占用的反向短桩——但进入判断节点（菱形）只取 0.5 顶点：
           draw.io 对菱形非顶点附着的周界求交不可靠，水平进线会把
           箭头画成竖直方向；
        B. 顶出侧进：从源节点顶边垂直上行，经通道转入目标朝通道的侧边
           （同排邻居挡住侧出水平段时的退路）。
        C. 侧出底进（仅判断节点）：菱形入口只认顶点，而侧顶点常被该
           分支自己的出边短桩反向占用；此时侧出后经通道上行到目标
           下方的行间空隙带，横移到目标中线竖直进入底顶点。

        三个模板的候选全部失败时做双车道迁移重试（如源节点顶边中点
        被正向入边占着，B 模板的顶出短桩与之各退到 1/3 与 2/3）。
        """
        sr, tr = self.rects[s], self.rects[t]
        cands = []  # (权重, pts, exit_p, entry_p)

        def _add(pts, exit_p, entry_p, cx):
            cands.append((self._len(pts) + self._penalty(cx), pts, exit_p, entry_p))

        # A. 侧出侧进（两侧 × 附着点偏移）
        for side in (outward, -outward):
            exit_x = sr[2] if side > 0 else sr[0]
            entry_x = tr[2] if side > 0 else tr[0]
            # 菱形出/入口都只认顶点（0.5）：非顶点附着会画在菱形腰上，
            # 且兄弟分支借偏移挤进同侧（是/否必须异侧）；普通节点出口
            # 保留偏移自由，避开目标侧边上已占用的反向短桩
            for exf in ((0.5,) if s in self.decisions else (0.5, 0.3, 0.7)):
                for enf in ((0.5,) if t in self.decisions else (0.5, 0.3, 0.7)):
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
                        _add(pts, exit_p, entry_p, cx)

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
            _add(pts, (0.5, 0), entry_p, cx)

        # C. 侧出底进（仅判断节点）：底顶点通常是菱形唯一空闲的顶点
        if t in self.decisions and sr[1] >= tr[3]:
            x1 = (tr[0] + tr[2]) / 2
            yb = tr[3] + GAP_Y / 2  # 目标下方的行间空隙带
            for side in (outward, -outward):
                exit_x = sr[2] if side > 0 else sr[0]
                # 源是菱形时同样只吃顶点（0.5），与 A 模板同一规矩
                for exf in ((0.5,) if s in self.decisions else (0.5, 0.3, 0.7)):
                    exit_p = (1 if side > 0 else 0, exf)
                    exit_y = sr[1] + exf * H
                    for cx in self.cands:
                        if side > 0 and cx < exit_x + 2:
                            continue
                        if side < 0 and cx > exit_x - 2:
                            continue
                        pts = [(exit_x, exit_y), (cx, exit_y), (cx, yb),
                               (x1, yb), (x1, tr[3])]
                        pts = [p for i, p in enumerate(pts)
                               if i == 0 or p != pts[i - 1]]
                        _add(pts, exit_p, (0.5, 1), cx)

        best = None
        for w, pts, exit_p, entry_p in cands:
            if self._ok(_segs_of(pts, t), s, t) \
                    and self._attach_ok(s, t, exit_p, entry_p):
                if best is None or w < best[0]:
                    best = (w, pts, exit_p, entry_p)
        if best is not None:
            return best[1][1:-1], best[2], best[3]
        # 双车道迁移重试：可迁移的候选里取最短
        plans = []
        for w, pts, exit_p, entry_p in cands:
            plan = self._migration_plan(pts, s, t, exit_p, entry_p)
            if plan is not None:
                plans.append((w, pts, exit_p, entry_p, plan))
        plans.sort(key=lambda x: x[0])
        for _w, pts, exit_p, entry_p, plan in plans:
            routed = self._apply_migration(plan, pts, s, t, exit_p, entry_p)
            if routed is not None:
                return routed
        return None


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


def _prepare(xml_text: str, router_cls: type["_Router"] | None = None):
    """apply_flow_layout 的前半段：解析、分层、定位、建路由器。

    单独抽出供 scripts/route_report.py 等调试工具复用——那里用
    router_cls 注入插桩子类，两边共享同一套驱动逻辑。
    返回 (root, layered, edges_sorted, router, join_nodes)。
    """
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
    router = (router_cls or _Router)(pos, decisions)
    router.node_cells = by_id
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
    return root, layered, edges_sorted, router, join_nodes


def apply_flow_layout(xml_text: str) -> str:
    """给不带几何信息的流程图 mxfile XML 注入计算好的布局，返回新 XML。"""
    root, _layered, edges_sorted, router, join_nodes = _prepare(xml_text)
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


def _frac(v: float) -> str:
    """附着点比例写最短 4 位小数（三等分车道 0.3333/0.6667）。"""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _write_edge(edge: ET.Element, pts: list[tuple],
                exit_p: tuple, entry_p: tuple,
                label_pos: tuple | None = None) -> list[tuple]:
    """把路径写入 edge 的 style/geometry（附着点 + 拐点），返回去重后的点列。

    双车道迁移会二次调用它重写已布边的 XML，所以写 style/geometry 前
    一律先清空旧值。label_pos 是 label 防重叠的挪位结果 (gx, gy)，写入
    geometry 的 x/y（draw.io：x∈[-1,1] 沿路径定位，y 垂直偏移像素）。
    """
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    wps = pts[1:-1]
    style = _EDGE_STYLE + (
        f"exitX={_frac(exit_p[0])};exitY={_frac(exit_p[1])};exitDx=0;exitDy=0;"
        f"entryX={_frac(entry_p[0])};entryY={_frac(entry_p[1])};entryDx=0;entryDy=0;"
    )
    if edge.get("value"):
        style += "fontColor=#3E4144;fontSize=12;"
    edge.set("style", style)
    for old in edge.findall("mxGeometry"):
        edge.remove(old)
    geo = ET.SubElement(edge, "mxGeometry")
    if label_pos is not None:
        geo.set("x", _frac(label_pos[0]))
        geo.set("y", _num(label_pos[1]))
    geo.set("relative", "1")
    geo.set("as", "geometry")
    if wps:
        arr = ET.SubElement(geo, "Array")
        arr.set("as", "points")
        for x, y in wps:
            pt = ET.SubElement(arr, "mxPoint")
            pt.set("x", _num(x))
            pt.set("y", _num(y))
    return pts


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
    outward = 1 if dx >= 0 else -1

    def _try():
        """按当前 decisions 状态试模板（降级后重试时模板序列会重算）。"""
        if dy > 0:  # 向下：同列底出顶进；左右分叉优先侧边出，侧出无解再底出绕行
            if abs(dx) < 1:
                # 同列优先直连；直连/绕行全灭时退到侧出（如目标是判断+汇合节点，
                # 顶边中点已被占且菱形不享受总线豁免——两侧页边通道往往通畅）
                tries = ["bottom", "right", "left"]
            else:
                pref = "right" if dx > 0 else "left"
                other = "left" if pref == "right" else "right"
                if s in router.decisions:
                    tries = [pref, "bottom", other]
                elif join_nodes and t in join_nodes:
                    tries = ["bottom", pref]
                else:
                    tries = [pref, "bottom"]
            for how in tries:
                routed = (router.route_down_bottom(s, t) if how == "bottom"
                          else router.route_down_side(s, t, 1 if how == "right" else -1))
                if routed is not None:
                    return routed
            return None
        if dy < 0:  # 回边：从侧边出、同侧进，经外侧通道绕开中间节点
            return router.route_back(s, t, outward)
        return None

    wps: list[tuple] = []
    routed = _try()
    if routed is None and dy != 0 \
            and (s in router.decisions or t in router.decisions):
        # 菱形四顶点车道耗尽的兜底：两端（先源后目标）的判断节点降级为
        # 矩形后整体重试——一般判断节点本不该有太多线路出入，矩形车道更多
        for n in (s, t):
            if n in router.decisions:
                router.degrade(n)
                logger.warning(
                    "边 %s -> %s 无路可走，判断节点 %s 降级为矩形重试", s, t, n)
                routed = _try()
                if routed is not None:
                    break
    if dy == 0:  # 同层：下出下进（draw.io 自动向下微绕，行间空隙带内无节点）
        exit_p = entry_p = (0.5, 1)
    elif routed is None:
        logger.warning("边 %s -> %s 找不到无遮挡通道，退回 draw.io 自动走线", s, t)
        if dy > 0:
            exit_p, entry_p = (0.5, 1), (0.5, 0)
        else:
            exit_p = entry_p = (1 if outward > 0 else 0, 0.5)
    else:
        wps, exit_p, entry_p = routed
    # 清理连续重复拐点（通道 x 与入口 x 重合时会产生零长度段）
    pts = [_anchor(sr, exit_p), *wps, _anchor(tr, entry_p)]
    text = edge.get("value") or ""
    label_pos = router.place_label(pts, text) if text else None
    pts = _write_edge(edge, pts, exit_p, entry_p, label_pos=label_pos)
    eid = edge.get("id") or f"{s}->{t}#{len(router.routes)}"
    router.commit(pts, s, t, eid, exit_p, entry_p, elem=edge, label_pos=label_pos)


def _num(v: float) -> str:
    """整数不带小数点，保持 XML 干净。"""
    return str(int(v)) if v == int(v) else str(round(v, 2))

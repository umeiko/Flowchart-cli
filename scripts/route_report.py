"""走线报告工具：手动触发一次流程图布局路由，输出逐边路由过程报告。

用法：
    uv run python scripts/route_report.py <文件.drawio> [-o 输出目录] [--render]

产物（默认写在输入文件旁边）：
    <文件名>.relayout.drawio    重新布局后的 XML
    <文件名>.route_report.txt   走线报告（分层/坐标/候选通道/逐边尝试与失败原因）
    --render 时追加 <文件名>.relayout.png（读 .env 的 DRAWIO_PATH 调 draw.io 桌面版）

实现：复用 layout_flow._prepare（与 apply_flow_layout 同一驱动逻辑），
用 DebugRouter 插桩 _clear/_free_lane/route_*，记录每条边每个模板尝试的
成败与失败原因（穿哪个节点 / 与哪条已提交边车道冲突）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# 允许从项目根直接运行（uv run python scripts/route_report.py ...）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowchart_agent.drawio import layout_flow as lf  # noqa: E402

_PAD = lf._PAD
_MERGE = lf._MERGE


class DebugRouter(lf._Router):
    """插桩路由器：记录每次 _ok 失败的原因与每个模板尝试的结果。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts: list[dict] = []   # 当前边的模板尝试记录
        self._fails: list[tuple[str, str]] = []  # 当前 route 调用内的 (类别, 阻塞者)

    # ---- 失败原因采集（先算布尔，再独立定位阻塞者）----

    def _clear(self, seg, exclude=()):
        ok = super()._clear(seg, exclude)
        if not ok:
            for i, r in self.rects.items():
                if i not in exclude and self._hits_rect(seg, r):
                    self._fails.append(("穿节点", i))
        return ok

    def _free_lane(self, seg, s, t):
        ok = super()._free_lane(seg, s, t)
        if not ok:
            self._fails.append(("车道冲突", self._find_conflict(seg, s, t)))
        return ok

    def _find_conflict(self, seg, s, t) -> str:
        """定位导致车道冲突的已提交线段（跳过会被豁免的同向共边）。"""
        x1, y1, x2, y2 = seg
        for a, b, c, d, cs, ct, _eid in self.committed:
            if x1 == x2 and a == c and abs(x1 - a) < _MERGE:
                lo = max(min(y1, y2), min(b, d))
                hi = min(max(y1, y2), max(b, d))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if (shared is not None and shared not in self.decisions
                        and lf._inside(self._vicinity(shared), lo, hi)):
                    continue
                return f"{cs}→{ct} 竖直段 x={a:g}（重叠 {hi - lo:.0f}px，非豁免）"
            if y1 == y2 and b == d and abs(y1 - b) < _MERGE:
                lo = max(min(x1, x2), min(a, c))
                hi = min(max(x1, x2), max(a, c))
                if hi - lo <= _PAD:
                    continue
                shared = s if cs == s else (t if ct == t else None)
                if shared is not None and shared not in self.decisions:
                    z = self._vicinity(shared)
                    if z[0] <= y1 <= z[1]:
                        continue
                return f"{cs}→{ct} 水平段 y={b:g}（重叠 {hi - lo:.0f}px，非豁免）"
        return "未知冲突"

    # ---- 模板尝试记录 ----

    def _wrap(self, template: str, result):
        rec = {"template": template, "ok": result is not None}
        if result is not None:
            rec["wps"], rec["exit"], rec["entry"] = result
        else:
            rec["reasons"] = Counter(f"{k} {v}" for k, v in self._fails)
        self._fails = []
        self.attempts.append(rec)
        return result

    def route_down_bottom(self, s, t):
        return self._wrap("底出顶进", super().route_down_bottom(s, t))

    def route_down_side(self, s, t, outward=None):
        side = {1: "右", -1: "左"}.get(outward, "自动")
        return self._wrap(f"侧出（{side}）", super().route_down_side(s, t, outward))

    def route_back(self, s, t, outward):
        return self._wrap("回边(A侧进/B顶出/C底进)", super().route_back(s, t, outward))


def _label(cell: ET.Element | None) -> str:
    if cell is None:
        return "?"
    return (cell.get("value") or "").replace("<br/>", "/") or "?"


def _fmt_pt(p) -> str:
    return f"({p[0]:g},{p[1]:g})"


def main() -> int:
    ap = argparse.ArgumentParser(description="流程图走线报告：手动触发一次布局并输出逐边路由过程")
    ap.add_argument("drawio", type=Path, help="输入 .drawio 文件（有无几何信息均可）")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="产物目录，默认与输入文件同目录")
    ap.add_argument("--render", action="store_true",
                    help="追加渲染 PNG（读 .env 的 DRAWIO_PATH 调 draw.io 桌面版）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    src: Path = args.drawio
    out_dir = args.output or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_text = src.read_text(encoding="utf-8")
    root, layered, edges_sorted, router, join_nodes = lf._prepare(
        xml_text, router_cls=DebugRouter
    )
    by_id = {c.get("id"): c for c in root.iter("mxCell") if c.get("vertex") == "1"}

    lines: list[str] = []
    lines.append(f"# 走线报告：{src.name}")
    lines.append(f"网格：节点 {router.g.w}×{router.g.h}，层间距 {router.g.gap_y}，层内列距 {router.g.gap_x}")
    lines.append("")

    lines.append("## 1. 分层与坐标")
    for li, layer in enumerate(layered):
        for nid in layer:
            x, y = router.rects[nid][0], router.rects[nid][1]
            tags = []
            if nid in router.decisions:
                tags.append("判断")
            if nid in join_nodes:
                tags.append("汇合")
            tag = f"（{'/'.join(tags)}）" if tags else ""
            lines.append(f"  层{li}  {nid}「{_label(by_id.get(nid))}」@({x:g},{y:g}){tag}")
    lines.append("")
    lines.append(f"判断节点：{' '.join(sorted(router.decisions)) or '无'}")
    lines.append(f"汇合节点：{' '.join(sorted(join_nodes)) or '无'}")
    lines.append("")

    lines.append("## 2. 候选垂直通道（x 坐标）")
    cands = [f"{c:g}" for c in router.cands]
    lines.append("  " + " ".join(cands))
    lines.append("")

    lines.append("## 3. 逐边路由（按曼哈顿距离升序，先短后长）")
    n_ok = n_fallback = n_same = 0
    for e in edges_sorted:
        eid, s, t = e.get("id"), e.get("source"), e.get("target")
        label = e.get("value") or ""
        sr, tr = router.rects[s], router.rects[t]
        dx, dy = tr[0] - sr[0], tr[1] - sr[1]
        router.attempts = []
        router._fails = []
        n_mig = len(router.migrations)
        n_deg = len(router.degraded)
        lf._normalize_edge(e, router, join_nodes)

        head = f"[{eid}] {label + ' ' if label else ''}{s}「{_label(by_id.get(s))}」" \
               f"→ {t}「{_label(by_id.get(t))}」  Δ=({dx:g},{dy:g})"
        lines.append(head)

        if dy == 0:
            lines.append("  — 同层边：下出下进，交给 draw.io 微绕")
            n_same += 1
            continue
        for att in router.attempts:
            if att["ok"]:
                wps = " ".join(_fmt_pt(p) for p in att["wps"]) or "（无拐点，直连）"
                lines.append(
                    f"  ✓ {att['template']}  exit={att['exit']} entry={att['entry']}  拐点 {wps}"
                )
            else:
                reasons = "；".join(f"{r} ×{n}" for r, n in att["reasons"].most_common())
                lines.append(f"  ✗ {att['template']} — {reasons or '无候选通道'}")
        for mig in router.migrations[n_mig:]:
            lines.append(f"  ↻ 双车道迁移：{mig}")
        for nid in router.degraded[n_deg:]:
            lines.append(
                f"  ▼ 判断节点 {nid}「{_label(by_id.get(nid))}」车道耗尽，降级为矩形后重试")
        if not router.attempts or not router.attempts[-1]["ok"]:
            lines.append("  ⚠ 全部模板失败 → 退回 draw.io 自动走线（可能穿节点/重叠）")
            n_fallback += 1
        else:
            n_ok += 1
    lines.append("")

    lines.append("## 4. 汇总")
    lines.append(f"  边总数 {len(edges_sorted)}：路由成功 {n_ok}，同层自动 {n_same}，回退 {n_fallback}")
    if router.degraded:
        lines.append(f"  降级为矩形的判断节点：{' '.join(router.degraded)}")

    report = "\n".join(lines) + "\n"
    report_path = out_dir / f"{src.stem}.route_report.txt"
    report_path.write_text(report, encoding="utf-8")

    out_xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
    xml_path = out_dir / f"{src.stem}.relayout.drawio"
    xml_path.write_text(out_xml, encoding="utf-8")

    print(f"报告：{report_path}")
    print(f"XML ：{xml_path}")
    print(f"边总数 {len(edges_sorted)}：路由成功 {n_ok}，同层 {n_same}，回退 {n_fallback}")

    if args.render:
        from dotenv import load_dotenv

        load_dotenv()
        from flowchart_agent.drawio import render_drawio

        png = render_drawio(
            xml_path, out_dir / f"{src.stem}.relayout.png",
            __import__("os").getenv("DRAWIO_PATH"),
        )
        print(f"PNG ：{png}" if png else "PNG 渲染失败（检查 DRAWIO_PATH）")
    return 1 if n_fallback else 0


if __name__ == "__main__":
    sys.exit(main())

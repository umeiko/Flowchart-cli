"""Agent 主循环：生成 → 渲染校验 → 多模态视觉验证 → 反馈修复。

双引擎：mermaid（默认，LLM 出 Mermaid 代码 → mmdc 渲染）与
drawio（LLM 出 draw.io 原生 XML → 确定性布局 → draw.io 桌面版渲染）。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .cancellation import CancelCheck, OperationCancelled
from .config import Settings
from .drawio import (
    DrawioNotFoundError,
    FlowGrid,
    apply_flow_layout,
    apply_font,
    apply_layout,
    check_drawio_available,
    extract_xml,
    render_drawio,
    sanitize_xml,
)
from .llm import LLMClient
from .mermaid import extract_mermaid, render_mermaid
from .router import route_diagram_type
from .styles import Style
from . import prompts

logger = logging.getLogger(__name__)


def _file_size(path: Path) -> str:
    """文件大小的可读形式（run.log 用）。"""
    try:
        kb = path.stat().st_size / 1024
    except OSError:
        return "大小未知"
    return f"{kb:.0f} KB" if kb >= 1 else f"{path.stat().st_size} B"


# 失败卡片中代码框的最大字符数：完整代码仍在 run 目录的 round_* 产物中
_FAILURE_CARD_CODE_LIMIT = 3000


def _failure_card_drawio(code: str, feedback: str) -> str:
    """drawio 引擎的失败卡片：合法 mxfile XML，两个宽框分别展示最后一版
    失败代码与失败的可能原因。确定性拼 XML，不需要模型参与。"""

    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("\r\n", "\n").replace("\n", "&lt;br/&gt;")
        )

    code = code.strip()
    if len(code) > _FAILURE_CARD_CODE_LIMIT:
        code = code[:_FAILURE_CARD_CODE_LIMIT] + "\n……（过长已截断，完整代码见过程目录）"
    code_text = esc(code) or "（未能提取到代码）"
    reason_text = esc(feedback.strip()) or "（未知原因，详见 run.log）"

    def box_height(text: str) -> int:
        lines = text.count("&lt;br/&gt;") + 1
        return min(40 + lines * 20, 500)

    style_box = (
        "rounded=0;whiteSpace=wrap;html=1;fillColor=#F5F8FF;strokeColor=#666666;"
        "fontColor=#3E4144;fontSize=12;align=left;verticalAlign=top;spacing=8;"
    )
    h1, h2 = box_height(code_text), box_height(reason_text)
    y2 = 70 + h1 + 20
    layer_h = y2 + h2 + 20
    return f"""<mxfile host="app.diagrams.net">
  <diagram name="生成失败" id="fail1">
    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="0" pageScale="1" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="L1" value="生成失败（所有轮次均未通过）" style="rounded=0;whiteSpace=wrap;html=1;fillColor=#E1E4E6;strokeColor=#000000;fontColor=#3E4144;fontSize=14;fontStyle=1;align=center;verticalAlign=top;spacingTop=6;" vertex="1" parent="1">
          <mxGeometry x="40" y="40" width="800" height="{layer_h}" as="geometry"/>
        </mxCell>
        <mxCell id="c1" value="&lt;b&gt;失败的代码（最后一版）&lt;/b&gt;&lt;br/&gt;{code_text}" style="{style_box}" vertex="1" parent="L1">
          <mxGeometry x="30" y="70" width="740" height="{h1}" as="geometry"/>
        </mxCell>
        <mxCell id="c2" value="&lt;b&gt;失败的可能原因&lt;/b&gt;&lt;br/&gt;{reason_text}" style="{style_box}" vertex="1" parent="L1">
          <mxGeometry x="30" y="{y2}" width="740" height="{h2}" as="geometry"/>
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""


def _failure_card(code: str, feedback: str) -> str:
    """生成兜底"失败卡片"：合法的 flowchart，两个框分别展示最后一版失败代码
    与失败的可能原因。用于全部轮次失败时，保证发布的产物一定是可渲染的图。"""

    def esc(text: str) -> str:
        # Mermaid 节点文字在英文双引号内可含大多数字符；
        # 只需转义双引号本身（#quot;），换行转 <br/>
        return text.replace('"', "#quot;").replace("\r\n", "\n").replace("\n", "<br/>")

    code = code.strip()
    if len(code) > _FAILURE_CARD_CODE_LIMIT:
        code = code[:_FAILURE_CARD_CODE_LIMIT] + "\n……（过长已截断，完整代码见过程目录 round_*.mmd）"
    code_text = esc(code) or "（未能提取到 Mermaid 代码）"
    reason_text = esc(feedback.strip()) or "（未知原因，详见 run.log）"
    return (
        "flowchart TD\n"
        f'    failed_code["<b>失败的代码（最后一版）</b><br/>{code_text}"]\n'
        f'    failed_reason["<b>失败的可能原因</b><br/>{reason_text}"]\n'
        "    failed_code --- failed_reason"
    )


@dataclass
class RoundRecord:
    round_no: int
    mermaid_code: str
    render_ok: bool
    image_path: Path | None = None
    svg_path: Path | None = None
    feedback: str = ""


@dataclass
class AgentResult:
    success: bool
    mermaid_code: str = ""
    image_path: Path | None = None
    rounds: list[RoundRecord] = field(default_factory=list)
    final_feedback: str = ""
    cancelled: bool = False


class FlowchartAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._text_llm = LLMClient(settings.text_model)
        # 视觉模型可选：未配置时视觉检视降级为代码检视（见 _verify）
        self._vision_llm = (
            LLMClient(settings.vision_model) if settings.vision_model else None
        )

    def run(
        self,
        document: str,
        output_dir: str | Path,
        initial_code: str = "",
        initial_feedback: str = "",
        reference_image: str | Path | None = None,
        background: str | None = None,
        style: Style | None = None,
        on_delta=None,
        on_reasoning=None,
        verify_mode: str = "full",
        on_round_start=None,
        on_stage=None,
        action: str = "",
        engine: str = "mermaid",
        flow_grid: FlowGrid | None = None,
        on_candidate=None,
        should_cancel=None,
        on_verify_delta=None,
        on_verify_tick=None,
    ) -> AgentResult:
        """生成-渲染-验证循环。

        initial_code 非空时，第一轮以"修订模式"启动：在 initial_code 基础上
        按 initial_feedback（用户修改意见）修复，用于对话式修改场景。
        reference_image：参考图片（用户提供的需求图，或修订时的当前渲染图），
        仅在 TEXT_MODEL_VISION=true 且第一轮生成时随消息发送。
        background：画布背景色，优先于 style 的背景与 RENDER_BACKGROUND 配置。
        style：风格插件（见 styles.py），mermaid 注入主题指令与 prompt_hint，
        drawio 注入引擎专属规则段（engine_hints，缺失时用提示词内置默认）。
        on_delta：生成阶段的流式文本回调（界面层实时显示），None 为非流式。
        on_reasoning：生成阶段的思考流回调（推理模型的 reasoning_content，
        仅界面提示与用量估算用），None 为不需要。
        verify_mode：检视强度，full=完整检视（排版+内容语义），
        layout=仅基础图形检视，code=源码检视，none=成功渲染后直接返回。
        on_round_start：每轮开始时回调（界面层清空上一轮的流式显示）。
        action：触发本次运行的动作描述（如 create_diagram/modify_diagram
        及其参数摘要），仅用于 run.log 记录任务上下文。
        engine：出图引擎，mermaid（默认）或 drawio（需要配置 DRAWIO_PATH）。
        flow_grid：drawio 流程图的节点尺寸/间距覆盖（FlowGrid，来自
        create_diagram 的可选布局参数），None 用默认 220×70/间距 60。
        """
        engine = engine.strip().lower()
        if engine == "drawio":
            unavailable = check_drawio_available(self._settings.drawio_path)
            if unavailable:
                raise DrawioNotFoundError(unavailable)
        # drawio 引擎的图型二级路由：决定用哪套提示词与哪个布局器。
        # 修订模式且上一版就是 drawio XML 时直接从代码判断（有连线即流程图），
        # 其余情况（新建、或从 mermaid 切引擎后的跨引擎修订）走 LLM 分类。
        diagram_type = ""
        if engine == "drawio":
            if initial_code and "<mxfile" in initial_code:
                diagram_type = (
                    "flowchart" if 'edge="1"' in initial_code else "architecture"
                )
                logger.info("图型：%s（沿用上版代码的图型）", diagram_type)
            else:
                diagram_type = route_diagram_type(
                    self._text_llm, document, should_cancel=should_cancel
                )
                logger.info("图型：%s（LLM 二级路由）", diagram_type)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        handler = self._attach_run_log(output_dir / "run.log")

        result = AgentResult(success=False)
        code = initial_code
        feedback = initial_feedback
        bg = (
            background
            or (style.background if style else None)
            or self._settings.render_background
        )
        # 参考图只在第一轮传给生成模型
        first_round_image = (
            Path(reference_image)
            if reference_image and self._settings.text_model_vision
            else None
        )

        try:
            logger.info("任务开始：%s", action or "生成流程图")
            logger.info(
                "配置：模式=%s 引擎=%s 检视=%s 风格=%s 背景=%s 最大轮次=%d "
                "输出格式=%s 缩放=%s 宽度=%s 布局=%s 需求文档=%d字符",
                "修订" if initial_code else "新建",
                engine,
                verify_mode,
                (style.name if style else "无"),
                bg,
                self._settings.max_rounds,
                self._settings.output_format,
                self._settings.render_scale,
                self._settings.render_width,
                (
                    f"{flow_grid.w}×h{flow_grid.h} 间距{flow_grid.gap_x}/{flow_grid.gap_y}"
                    if flow_grid else "默认220×h70 间距60/60（未读技能包/未传布局参数）"
                ),
                len(document),
            )
            if initial_feedback:
                logger.info("修改意见：%s", initial_feedback[:200])
            if reference_image:
                logger.info(
                    "参考图片：%s%s",
                    reference_image,
                    "" if first_round_image else "（主模型未开启图像输入，仅作记录）",
                )
            logger.info("输出目录：%s", output_dir)
            for round_no in range(1, self._settings.max_rounds + 1):
                if should_cancel and should_cancel():
                    result.cancelled = True
                    result.final_feedback = "用户已停止生成"
                    return result
                logger.info("=== 第 %d/%d 轮 ===", round_no, self._settings.max_rounds)
                if on_round_start:
                    on_round_start(round_no)

                # 1. 生成 / 修复
                if on_stage:
                    on_stage("generating", f"第 {round_no} 轮：正在生成图表代码")
                code, raw = self._generate(
                    document, code, feedback,
                    image=first_round_image if round_no == 1 else None,
                    on_delta=on_delta, on_reasoning=on_reasoning,
                    style=style, engine=engine,
                    diagram_type=diagram_type, flow_grid=flow_grid,
                    should_cancel=should_cancel,
                )
                if should_cancel and should_cancel():
                    result.cancelled = True
                    result.final_feedback = "用户已停止生成"
                    return result
                raw_path = output_dir / f"round_{round_no}_generate_raw.txt"
                raw_path.write_text(raw, encoding="utf-8")
                record = RoundRecord(round_no=round_no, mermaid_code=code, render_ok=False)
                result.rounds.append(record)
                label = "drawio XML" if engine == "drawio" else "Mermaid 代码"
                logger.info(
                    "第 %d 轮：生成完成（原始输出 %d 字符，提取到 %s %d 字符，见 %s）",
                    round_no, len(raw), label, len(code), raw_path.name,
                )
                if not code:
                    feedback = (
                        "你的输出中没有可识别的 drawio mxfile XML，请只输出 ```xml 代码块。"
                        if engine == "drawio"
                        else "你的输出中没有可识别的 Mermaid 代码，请只输出 ```mermaid 代码块。"
                    )
                    logger.warning("第 %d 轮：未能提取到 %s", round_no, label)
                    continue

                # 2. 渲染校验
                if on_stage:
                    on_stage("rendering", f"第 {round_no} 轮：正在渲染图表")
                if engine == "drawio":
                    # 配色规则由 style 引擎段注入提示词；此处注入确定性几何
                    # （架构图网格 / 流程图分层分支布局），XML 非法直接打回重修
                    try:
                        if diagram_type == "flowchart":
                            styled = apply_flow_layout(
                                sanitize_xml(code), grid=flow_grid)
                        else:
                            styled = apply_layout(sanitize_xml(code))
                        # 字体后处理（.env DRAWIO_FONT_FAMILY/DRAWIO_FONT_SIZE，
                        # 未配置时原样返回），让产物 .drawio 也带上规范字体
                        styled = apply_font(
                            styled,
                            self._settings.drawio_font_family,
                            self._settings.drawio_font_size,
                        )
                    except (ET.ParseError, ValueError) as e:
                        feedback = (
                            f"你的输出不是合法的 drawio {diagram_type} XML（{e}）。"
                            "请修正后重新输出完整 XML，不要写 mxGeometry 元素。"
                        )
                        record.feedback = feedback
                        logger.warning("第 %d 轮：XML 校验/布局失败 -> %s", round_no, e)
                        continue
                    record.mermaid_code = styled
                    render = self._render_drawio(
                        styled, output_dir, round_no, should_cancel=should_cancel
                    )
                else:
                    # 应用风格插件（注入主题指令），渲染与产物都用风格化后的代码
                    styled = style.apply(code) if style else code
                    record.mermaid_code = styled
                    render = render_mermaid(
                        styled, output_dir, stem=f"round_{round_no}",
                        fmt=self._settings.output_format, background=bg,
                        chrome_path=self._settings.chrome_path,
                        scale=self._settings.render_scale,
                        width=self._settings.render_width,
                        should_cancel=should_cancel,
                    )
                record.render_ok = render.ok
                record.image_path = render.image_path
                if not render.ok:
                    feedback = prompts.RENDER_ERROR_FEEDBACK.format(error=render.error)
                    record.feedback = render.error
                    logger.warning("第 %d 轮：渲染失败 -> %s", round_no, render.error[:200])
                    continue
                logger.info(
                    "第 %d 轮：渲染成功 -> %s（%s）",
                    round_no, render.image_path, _file_size(render.image_path),
                )

                # 2.5 顺便出一份 SVG（矢量图便于查看与二次编辑；失败不影响主流程）。
                # mermaid：width=auto 的 PNG 渲染已探测出 SVG，直接复用；
                # drawio：SVG 已在 _render_drawio 中一并导出，这里只登记。
                if self._settings.output_format != "svg":
                    if render.svg_path:
                        record.svg_path = render.svg_path
                        if engine == "drawio":
                            logger.info(
                                "第 %d 轮：SVG 产物 -> %s", round_no, render.svg_path
                            )
                    elif engine != "drawio":
                        svg = render_mermaid(
                            styled, output_dir, stem=f"round_{round_no}", fmt="svg",
                            background=bg, chrome_path=self._settings.chrome_path,
                            should_cancel=should_cancel,
                        )
                        if svg.ok:
                            record.svg_path = svg.image_path
                            logger.info(
                                "第 %d 轮：SVG 产物 -> %s", round_no, svg.image_path
                            )
                        else:
                            logger.warning("第 %d 轮：SVG 渲染失败（不影响主流程）-> %s",
                                           round_no, svg.error[:200])

                # 一轮只要已经成功渲染，就立即向会话发布候选图。视觉验证可能耗时，
                # 用户在验证或下一轮生成期间停止时，current.* 也应指向本次最新产物。
                if on_candidate:
                    on_candidate(styled, render.image_path, record.svg_path, round_no)
                if should_cancel and should_cancel():
                    result.cancelled = True
                    result.mermaid_code = styled
                    result.image_path = render.image_path
                    result.final_feedback = "用户已停止生成"
                    return result

                if verify_mode == "none":
                    record.feedback = "已按要求跳过视觉验证"
                    if on_stage:
                        on_stage(
                            "completed",
                            f"第 {round_no} 轮：渲染完成，已跳过视觉验证",
                        )
                    logger.info("第 %d 轮：渲染完成，跳过视觉验证并直接返回", round_no)
                    result.success = True
                    result.mermaid_code = styled
                    result.image_path = render.image_path
                    return result

                # 3. 视觉验证（code 模式 / 未配置视觉模型时审查源码）
                if on_stage:
                    label = "视觉验证" if verify_mode != "code" and self._vision_llm else "代码验证"
                    on_stage("verifying", f"第 {round_no} 轮：正在{label}")
                passed, critique, raw_reply = self._verify(
                    document,
                    render.image_path,
                    verify_mode,
                    code=styled,
                    on_delta=on_verify_delta,
                    on_tick=on_verify_tick,
                    on_reasoning=on_reasoning,
                    should_cancel=should_cancel,
                )
                if should_cancel and should_cancel():
                    result.cancelled = True
                    result.mermaid_code = styled
                    result.image_path = render.image_path
                    result.final_feedback = "用户已停止生成"
                    return result
                raw_path = output_dir / f"round_{round_no}_verify_raw.txt"
                raw_path.write_text(raw_reply, encoding="utf-8")
                record.feedback = critique
                if passed:
                    if on_stage:
                        on_stage("verified", f"第 {round_no} 轮：验证通过")
                    logger.info("第 %d 轮：视觉验证通过", round_no)
                    result.success = True
                    result.mermaid_code = styled
                    result.image_path = render.image_path
                    return result

                feedback = critique
                logger.warning("第 %d 轮：视觉验证不通过 -> %s", round_no, critique[:200])

            # 超出最大轮次，任务失败。兜底发布分两级：
            logger.warning("达到最大轮次 %d，任务失败", self._settings.max_rounds)
            result.final_feedback = result.rounds[-1].feedback

            # 1) 有任何一轮成功渲染出图：发布该版本（未通过检视，仅供参考），
            #    不呈现失败卡片——能看的真图比卡片有用
            rendered = next(
                (r for r in reversed(result.rounds) if r.image_path is not None),
                None,
            )
            if rendered is not None:
                result.mermaid_code = rendered.mermaid_code
                result.image_path = rendered.image_path
                logger.info("兜底发布最后一版可渲染的图（第 %d 轮产物）", rendered.round_no)
                return result

            # 2) 所有轮次连图都渲不出来：发布"失败卡片"（两框：失败代码 +
            #    失败原因），保证 current/final 一定是可渲染的图；
            #    原始各轮产物仍在 run 目录
            result.mermaid_code = next(
                (r.mermaid_code for r in reversed(result.rounds) if r.mermaid_code),
                "",
            )
            if engine == "drawio":
                card_xml = _failure_card_drawio(result.mermaid_code, result.final_feedback)
                card_path = output_dir / "failure_card.drawio"
                card_path.write_text(card_xml, encoding="utf-8")
                card_img = render_drawio(
                    card_path,
                    output_dir / f"failure_card.{self._drawio_fmt()}",
                    self._settings.drawio_path, fmt=self._drawio_fmt(),
                    scale=int(self._settings.render_scale or "2"),
                    should_cancel=should_cancel,
                )
                if card_img:
                    result.mermaid_code = card_xml
                    result.image_path = card_img
                    logger.info("失败卡片已生成：%s", card_img)
                else:  # 卡片渲染失败不应发生
                    logger.warning("失败卡片渲染失败（不应发生）")
                return result
            card_code = _failure_card(result.mermaid_code, result.final_feedback)
            card = render_mermaid(
                card_code, output_dir, stem="failure_card",
                fmt=self._settings.output_format, background=bg,
                chrome_path=self._settings.chrome_path,
                scale=self._settings.render_scale,
                width=self._settings.render_width,
                should_cancel=should_cancel,
            )
            if card.ok:
                result.mermaid_code = card_code
                result.image_path = card.image_path
                logger.info("失败卡片已生成：%s", card.image_path)
            else:  # 卡片渲染失败不应发生
                logger.warning("失败卡片渲染失败（不应发生）：%s", card.error[:200])
            return result
        except OperationCancelled:
            result.cancelled = True
            result.final_feedback = "用户已停止生成"
            rendered = next(
                (r for r in reversed(result.rounds) if r.image_path is not None),
                None,
            )
            if rendered is not None:
                result.mermaid_code = rendered.mermaid_code
                result.image_path = rendered.image_path
            return result
        finally:
            self._detach_run_log(handler)

    @staticmethod
    def _attach_run_log(log_path: Path) -> logging.FileHandler:
        """把步骤日志同时写入 <output_dir>/run.log，便于复盘分步生成过程。"""
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        )
        logging.getLogger("flowchart_agent").addHandler(handler)
        return handler

    @staticmethod
    def _detach_run_log(handler: logging.FileHandler) -> None:
        logging.getLogger("flowchart_agent").removeHandler(handler)
        handler.close()

    def _drawio_fmt(self) -> str:
        """drawio 引擎的出图格式：draw.io CLI 支持 png/svg/pdf。"""
        fmt = self._settings.output_format
        return fmt if fmt in ("png", "svg", "pdf") else "png"

    def _render_drawio(
        self, xml_text: str, output_dir: Path, round_no: int,
        should_cancel: CancelCheck = None,
    ):
        """渲染一轮 drawio 产物（.drawio + 图片 + SVG），返回与 RenderResult 同形的对象。"""
        fmt = self._drawio_fmt()
        xml_path = output_dir / f"round_{round_no}.drawio"
        xml_path.write_text(xml_text, encoding="utf-8")
        image = render_drawio(
            xml_path, output_dir / f"round_{round_no}.{fmt}",
            self._settings.drawio_path, fmt=fmt,
            scale=int(self._settings.render_scale or "2"),
            should_cancel=should_cancel,
        )
        if image is None:
            return SimpleNamespace(
                ok=False, image_path=None, svg_path=None,
                error="draw.io 渲染失败（详见上方 [drawio] 日志）",
            )
        svg_path = None
        if fmt != "svg":
            svg_path = render_drawio(
                xml_path, output_dir / f"round_{round_no}.svg",
                self._settings.drawio_path, fmt="svg",
                should_cancel=should_cancel,
            )
        return SimpleNamespace(ok=True, image_path=image, svg_path=svg_path, error=None)

    def _generate(
        self,
        document: str,
        prev_code: str,
        feedback: str,
        image: Path | None = None,
        on_delta=None,
        on_reasoning=None,
        style: Style | None = None,
        engine: str = "mermaid",
        diagram_type: str = "",
        flow_grid: FlowGrid | None = None,
        should_cancel: CancelCheck = None,
    ) -> tuple[str, str]:
        """返回 (提取出的图表代码, 模型原始输出)。image 为参考图（多模态模型时）。

        style 的正文说明（prompt_hint）并入生成 prompt，让默认/显式风格都被
        生成模型看到；chat 显式风格已把 hint 写进需求（session.create），
        这里按内容去重避免重复注入。drawio 引擎改为注入 style 的引擎专属
        规则段（engine_hints），缺失时提示词回落内置默认配色。
        """
        if engine == "drawio":
            style_rules = (
                style.engine_hint("drawio", diagram_type) if style else ""
            )
            if style and not style_rules:
                logger.info("风格插件 %s 没有 drawio:%s 规则段，使用内置默认配色",
                            style.name, diagram_type)
            system = prompts.drawio_system_prompt(
                diagram_type, style_rules,
                node_w=flow_grid.w if flow_grid else 220,
            )
            if not prev_code:
                user = prompts.DRAWIO_USER.format(document=document)
                if image:
                    user += "\n\n（附带的图片是需求参考，请结合图片内容理解需求。）"
            else:
                user = prompts.DRAWIO_REVISE_USER.format(
                    document=document, code=prev_code, feedback=feedback
                )
                if image:
                    user += "\n\n（附带的图片是当前图的渲染结果，请在此基础上修改。）"
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if image:
                messages = self._text_llm.with_images(messages, [image])
            if on_delta is not None:
                raw = self._text_llm.chat_stream(
                    messages, on_delta, on_reasoning, should_cancel=should_cancel
                )
            else:
                raw = self._text_llm.chat(messages, should_cancel=should_cancel)
            return extract_xml(raw), raw

        if not prev_code:
            user = prompts.GENERATE_USER.format(document=document)
            if image:
                user += "\n\n（附带的图片是需求参考，请结合图片内容理解流程。）"
        else:
            user = prompts.REVISE_USER.format(
                document=document, code=prev_code, feedback=feedback
            )
            if image:
                user += "\n\n（附带的图片是当前流程图的渲染结果，请在此基础上修改。）"
        if style and style.prompt_hint and style.prompt_hint not in document:
            user += (
                f"\n\n作图风格要求（{style.name}——{style.description}），"
                f"请严格遵循：\n{style.prompt_hint}"
            )
        messages = [
            {"role": "system", "content": prompts.GENERATE_SYSTEM},
            {"role": "user", "content": user},
        ]
        if image:
            messages = self._text_llm.with_images(messages, [image])
        if on_delta is not None:
            raw = self._text_llm.chat_stream(
                messages, on_delta, on_reasoning, should_cancel=should_cancel
            )
        else:
            raw = self._text_llm.chat(messages, should_cancel=should_cancel)
        return extract_mermaid(raw), raw

    def _verify(
        self,
        document: str,
        image_path: Path,
        mode: str = "full",
        code: str = "",
        on_delta=None,
        on_tick=None,
        on_reasoning=None,
        should_cancel: CancelCheck = None,
    ) -> tuple[bool, str, str]:
        """返回 (是否通过, 问题列表, 模型原始回复)。

        mode=layout 只做基础图形检视（排版/遮挡/连线），prompt 不带文档，
        避免视觉模型因文字识别错误反复误判；mode=full 额外核对内容与逻辑；
        mode=code 或视觉模型未配置时，退回文本模型直接审查 Mermaid 源码
        （最兜底，查不了排版问题）。

        判定结果通过 submit_result 工具（function calling）返回，避免思维链
        泄露/寒暄导致"首行 PASS"解析失败；工具调用失败或模型不配合时退回
        纯文本 PASS/FAIL 解析兜底。
        """
        if self._vision_llm is None:
            if mode != "code":
                logger.warning("未配置视觉模型（VISION_MODEL_*），检视降级为 code 模式")
            mode = "code"
        if mode == "code":
            logger.info("检视模式：code（文本模型 %s 审查 Mermaid 源码）",
                        self._text_llm.model_name)
            messages = [
                {
                    "role": "user",
                    "content": prompts.VERIFY_CODE_PROMPT.format(
                        document=document, code=code
                    ),
                }
            ]
            passed, issues, reply = self._judge(
                self._text_llm, messages, on_delta, on_tick, on_reasoning,
                should_cancel,
            )
        else:
            if mode == "layout":
                # 无参 .format()：模板里的 JSON 示例是 {{}} 转义写法，
                # 三个检视 prompt 统一过一次 format 还原成单花括号
                prompt = prompts.VERIFY_LAYOUT_PROMPT.format()
                logger.info("检视模式：layout（视觉模型 %s 仅做基础图形检视）",
                            self._vision_llm.model_name)
            else:
                prompt = prompts.VERIFY_PROMPT.format(document=document)
                logger.info("检视模式：full（视觉模型 %s 完整检视）",
                            self._vision_llm.model_name)
            messages = LLMClient.with_images(
                [{"role": "user", "content": prompt}], [image_path])
            passed, issues, reply = self._judge(
                self._vision_llm, messages, on_delta, on_tick, on_reasoning,
                should_cancel,
            )
        verdict = "PASS" if passed else "FAIL"
        logger.info("检视结论：%s（理由 %d 字符）", verdict, len(issues or reply))
        return passed, issues, reply

    def _judge(
        self,
        llm: LLMClient,
        messages: list[dict],
        on_delta=None,
        on_tick=None,
        on_reasoning=None,
        should_cancel: CancelCheck = None,
    ) -> tuple[bool, str, str]:
        """一次请求拿检视结论：工具与正文 JSON 是平行通道，不强制、不重试。

        优先级：submit_result 工具调用 → 正文 JSON（模板要求先 reason
        后 passed，强迫模型先分析再下结论）→ 纯文本 PASS/FAIL 兜底。
        不发 tool_choice="required"——不少网关直接拒绝该参数，
        强制 + 重试会让每次检视白打两次请求。端点报错明确提到工具
        不支持时（如 vLLM 未开 --enable-auto-tool-choice），给该
        LLMClient 打 _no_tools 标记，本会话后续检视直接走纯文本，
        不再每轮白打一次注定失败的工具请求。
        """
        def plain_reply() -> str:
            if on_delta is not None or on_tick is not None or on_reasoning is not None:
                return llm.chat_stream(
                    messages, on_delta or (lambda _text: None), on_reasoning,
                    should_cancel=should_cancel,
                ).strip()
            return llm.chat(messages, should_cancel=should_cancel).strip()

        if getattr(llm, "_no_tools", False):
            return self._judge_content(plain_reply())
        try:
            if on_delta is not None or on_tick is not None or on_reasoning is not None:
                msg = llm.chat_with_tools_stream(
                    messages,
                    [prompts.SUBMIT_RESULT_TOOL],
                    on_delta=on_delta or (lambda _text: None),
                    on_tick=on_tick,
                    on_reasoning=on_reasoning,
                    should_cancel=should_cancel,
                )
            else:
                msg = llm.chat_with_tools(
                    messages, [prompts.SUBMIT_RESULT_TOOL],
                    should_cancel=should_cancel,
                )
        except OperationCancelled:
            raise
        except Exception as e:
            logger.warning("submit_result 工具调用失败（%s），退回纯文本检视", e)
            if "tool" in str(e).lower():
                llm._no_tools = True  # 端点不支持工具，本会话不再尝试
                logger.info("该端点似乎不支持工具调用，后续检视将直接请求纯文本")
            # 与正常路径同一条解析管线：模型可能照样吐了 JSON（比如端点
            # 不支持 tools，但提示词的正文 JSON 模板它看得懂）
            return self._judge_content(plain_reply())
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            raw_args = calls[0].function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                logger.warning("submit_result 参数不是合法 JSON：%s", raw_args[:100])
            else:
                passed = self._coerce_passed(args.get("passed"))
                issues = str(args.get("issues") or "").strip()
                return passed, issues, raw_args
        content = (msg.content or "").strip()
        if content:
            return self._judge_content(content)
        logger.warning("检视响应既无工具调用也无文本，按不通过处理")
        return False, "检视模型未给出有效结论", ""

    def _judge_content(self, content: str) -> tuple[bool, str, str]:
        """正文解析管线：JSON 优先（_judge_json），不行再纯文本 PASS/FAIL。"""
        parsed = self._judge_json(content)
        if parsed is not None:
            return parsed
        logger.warning("检视响应不是 JSON，按纯文本解析")
        return self._judge_text(content)

    @staticmethod
    def _coerce_passed(value) -> bool:
        """passed 字段宽松取值：弱模型/网关有时吐字符串 "true"/"false"。"""
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "pass")

    @classmethod
    def _judge_json(cls, reply: str) -> "tuple[bool, str, str] | None":
        """正文 JSON 解析：模板要求 {"reason":…, "passed":…, "issues":…}
        （reason 在前，强迫模型先分析再下结论）。容忍代码块包裹、泄露的
        思维链等前后杂文字（从右往左找第一个能解析出含 passed 的对象——
        思维链里可能有花括号，取最靠后的），以及字符串里的原始换行
        （strict=False 放开控制字符）。"""
        decoder = json.JSONDecoder(strict=False)
        starts = [m.start() for m in re.finditer(r"\{", reply)]
        for pos in reversed(starts):
            try:
                args, _end = decoder.raw_decode(reply[pos:])
            except json.JSONDecodeError:
                continue
            if isinstance(args, dict) and "passed" in args:
                passed = cls._coerce_passed(args.get("passed"))
                issues = str(args.get("issues") or "").strip()
                return passed, issues, reply
        return None

    @staticmethod
    def _judge_text(reply: str) -> tuple[bool, str, str]:
        """纯文本兜底解析：首行 PASS 通过；判定标志被思维链/寒暄挤到后面时，
        找最后一个独占一行的 PASS/FAIL（FAIL 之后的行作为问题列表）。"""
        lines = [l for l in reply.splitlines() if l.strip()]
        if not lines:
            return False, reply, reply
        if lines[0].upper().startswith("PASS"):
            return True, "", reply
        for idx in range(len(lines) - 1, -1, -1):
            u = lines[idx].strip().upper()
            if u == "PASS":
                return True, "", reply
            if u == "FAIL":
                issues = "\n".join(lines[idx + 1:]).strip()
                return False, issues or reply, reply
        lines = [l for l in lines if l.strip().upper() != "FAIL"]
        return False, "\n".join(lines) or reply, reply

"""Agent 主循环：生成 → 渲染校验 → 多模态视觉验证 → 反馈修复。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .llm import LLMClient
from .mermaid import extract_mermaid, render_mermaid
from .styles import Style
from . import prompts

logger = logging.getLogger(__name__)


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
        verify_mode: str = "full",
        on_round_start=None,
    ) -> AgentResult:
        """生成-渲染-验证循环。

        initial_code 非空时，第一轮以"修订模式"启动：在 initial_code 基础上
        按 initial_feedback（用户修改意见）修复，用于对话式修改场景。
        reference_image：参考图片（用户提供的需求图，或修订时的当前渲染图），
        仅在 TEXT_MODEL_VISION=true 且第一轮生成时随消息发送。
        background：画布背景色，优先于 style 的背景与 RENDER_BACKGROUND 配置。
        style：风格插件（见 styles.py），注入主题指令并提供默认背景色。
        on_delta：生成阶段的流式文本回调（界面层实时显示），None 为非流式。
        verify_mode：视觉检视强度，full=完整检视（排版+内容语义），
        layout=仅基础图形检视（视觉模型识字能力弱时用，防止误判死循环）。
        on_round_start：每轮开始时回调（界面层清空上一轮的流式显示）。
        """
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
            logger.info("任务开始：输出目录 %s", output_dir)
            for round_no in range(1, self._settings.max_rounds + 1):
                logger.info("=== 第 %d/%d 轮 ===", round_no, self._settings.max_rounds)
                if on_round_start:
                    on_round_start(round_no)

                # 1. 生成 / 修复
                code, raw = self._generate(
                    document, code, feedback,
                    image=first_round_image if round_no == 1 else None,
                    on_delta=on_delta,
                )
                raw_path = output_dir / f"round_{round_no}_generate_raw.txt"
                raw_path.write_text(raw, encoding="utf-8")
                record = RoundRecord(round_no=round_no, mermaid_code=code, render_ok=False)
                result.rounds.append(record)
                logger.info(
                    "第 %d 轮：生成完成（原始输出见 %s）", round_no, raw_path.name
                )
                if not code:
                    feedback = "你的输出中没有可识别的 Mermaid 代码，请只输出 ```mermaid 代码块。"
                    logger.warning("第 %d 轮：未能提取到 Mermaid 代码", round_no)
                    continue

                # 应用风格插件（注入主题指令），渲染与产物都用风格化后的代码
                styled = style.apply(code) if style else code
                record.mermaid_code = styled

                # 2. 渲染校验
                render = render_mermaid(
                    styled, output_dir, stem=f"round_{round_no}",
                    fmt=self._settings.output_format, background=bg,
                    chrome_path=self._settings.chrome_path,
                    scale=self._settings.render_scale,
                    width=self._settings.render_width,
                )
                record.render_ok = render.ok
                record.image_path = render.image_path
                if not render.ok:
                    feedback = prompts.RENDER_ERROR_FEEDBACK.format(error=render.error)
                    record.feedback = render.error
                    logger.warning("第 %d 轮：渲染失败 -> %s", round_no, render.error[:200])
                    continue
                logger.info("第 %d 轮：渲染成功 -> %s", round_no, render.image_path)

                # 2.5 顺便出一份 SVG（矢量图便于查看与二次编辑；失败不影响主流程）。
                # width=auto 的 PNG 渲染已探测出 SVG，直接复用，不重复渲染。
                if self._settings.output_format != "svg":
                    if render.svg_path:
                        record.svg_path = render.svg_path
                    else:
                        svg = render_mermaid(
                            styled, output_dir, stem=f"round_{round_no}", fmt="svg",
                            background=bg, chrome_path=self._settings.chrome_path,
                        )
                        if svg.ok:
                            record.svg_path = svg.image_path
                        else:
                            logger.warning("第 %d 轮：SVG 渲染失败（不影响主流程）-> %s",
                                           round_no, svg.error[:200])

                # 3. 视觉验证（code 模式 / 未配置视觉模型时审查源码）
                passed, critique, raw_reply = self._verify(
                    document, render.image_path, verify_mode, code=styled
                )
                raw_path = output_dir / f"round_{round_no}_verify_raw.txt"
                raw_path.write_text(raw_reply, encoding="utf-8")
                record.feedback = critique
                if passed:
                    logger.info("第 %d 轮：视觉验证通过", round_no)
                    result.success = True
                    result.mermaid_code = styled
                    result.image_path = render.image_path
                    return result

                feedback = critique
                logger.warning("第 %d 轮：视觉验证不通过 -> %s", round_no, critique[:200])

            # 超出最大轮次：保留最后一轮状态
            logger.warning("达到最大轮次 %d，任务失败", self._settings.max_rounds)
            last = result.rounds[-1]
            result.mermaid_code = last.mermaid_code
            result.image_path = last.image_path
            result.final_feedback = last.feedback
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

    def _generate(
        self,
        document: str,
        prev_code: str,
        feedback: str,
        image: Path | None = None,
        on_delta=None,
    ) -> tuple[str, str]:
        """返回 (提取出的 Mermaid 代码, 模型原始输出)。image 为参考图（多模态模型时）。"""
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
        messages = [
            {"role": "system", "content": prompts.GENERATE_SYSTEM},
            {"role": "user", "content": user},
        ]
        if image:
            messages = self._text_llm.with_images(messages, [image])
        if on_delta is not None:
            raw = self._text_llm.chat_stream(messages, on_delta)
        else:
            raw = self._text_llm.chat(messages)
        return extract_mermaid(raw), raw

    def _verify(
        self, document: str, image_path: Path, mode: str = "full", code: str = ""
    ) -> tuple[bool, str, str]:
        """返回 (是否通过, 问题列表, 模型原始回复)。

        mode=layout 只做基础图形检视（排版/遮挡/连线），prompt 不带文档，
        避免视觉模型因文字识别错误反复误判；mode=full 额外核对内容与逻辑；
        mode=code 或视觉模型未配置时，退回文本模型直接审查 Mermaid 源码
        （最兜底，查不了排版问题）。
        """
        if self._vision_llm is None:
            if mode != "code":
                logger.warning("未配置视觉模型（VISION_MODEL_*），检视降级为 code 模式")
            mode = "code"
        if mode == "code":
            logger.info("检视模式：code（文本模型审查 Mermaid 源码）")
            reply = self._text_llm.chat(
                [
                    {
                        "role": "user",
                        "content": prompts.VERIFY_CODE_PROMPT.format(
                            document=document, code=code
                        ),
                    }
                ]
            ).strip()
        else:
            if mode == "layout":
                prompt = prompts.VERIFY_LAYOUT_PROMPT
                logger.info("检视模式：layout（仅基础图形检视）")
            else:
                prompt = prompts.VERIFY_PROMPT.format(document=document)
            reply = self._vision_llm.chat_with_image(prompt, image_path).strip()
        if reply.upper().startswith("PASS"):
            return True, "", reply
        # FAIL：去掉首行标志，保留问题列表
        lines = [l for l in reply.splitlines() if l.strip() and not l.strip().upper() == "FAIL"]
        return False, "\n".join(lines) or reply, reply

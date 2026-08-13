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
        self._vision_llm = LLMClient(settings.vision_model)

    def run(
        self,
        document: str,
        output_dir: str | Path,
        initial_code: str = "",
        initial_feedback: str = "",
        reference_image: str | Path | None = None,
        background: str | None = None,
        style: Style | None = None,
    ) -> AgentResult:
        """生成-渲染-验证循环。

        initial_code 非空时，第一轮以"修订模式"启动：在 initial_code 基础上
        按 initial_feedback（用户修改意见）修复，用于对话式修改场景。
        reference_image：参考图片（用户提供的需求图，或修订时的当前渲染图），
        仅在 TEXT_MODEL_VISION=true 且第一轮生成时随消息发送。
        background：画布背景色，优先于 style 的背景与 RENDER_BACKGROUND 配置。
        style：风格插件（见 styles.py），注入主题指令并提供默认背景色。
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

                # 1. 生成 / 修复
                code, raw = self._generate(
                    document, code, feedback,
                    image=first_round_image if round_no == 1 else None,
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
                )
                record.render_ok = render.ok
                record.image_path = render.image_path
                if not render.ok:
                    feedback = prompts.RENDER_ERROR_FEEDBACK.format(error=render.error)
                    record.feedback = render.error
                    logger.warning("第 %d 轮：渲染失败 -> %s", round_no, render.error[:200])
                    continue
                logger.info("第 %d 轮：渲染成功 -> %s", round_no, render.image_path)

                # 3. 多模态视觉验证
                passed, critique, raw_reply = self._verify(document, render.image_path)
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
            raw = self._text_llm.chat_with_images(messages, [image])
        else:
            raw = self._text_llm.chat(messages)
        return extract_mermaid(raw), raw

    def _verify(self, document: str, image_path: Path) -> tuple[bool, str, str]:
        """返回 (是否通过, 问题列表, 模型原始回复)。"""
        reply = self._vision_llm.chat_with_image(
            prompts.VERIFY_PROMPT.format(document=document), image_path
        ).strip()
        if reply.upper().startswith("PASS"):
            return True, "", reply
        # FAIL：去掉首行标志，保留问题列表
        lines = [l for l in reply.splitlines() if l.strip() and not l.strip().upper() == "FAIL"]
        return False, "\n".join(lines) or reply, reply

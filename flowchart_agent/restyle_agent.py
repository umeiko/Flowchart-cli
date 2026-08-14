"""风格转换子 Agent：调整当前流程图的风格，严禁改动内容与结构。

与生成主循环不同，这里不靠视觉模型判断"内容有没有被改"——而是用机械校验：
剥掉新旧代码中所有样式层语句（%%{init}%% 指令、classDef/class/style/
linkStyle 行、:::class 标记、注释行），剩下的结构骨架必须逐行一致，
否则打回重生成。从机制上保证内容零改动。渲染校验（mmdc）保证结果可渲染。
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .config import Settings
from .llm import LLMClient
from .mermaid import extract_mermaid, render_mermaid
from .styles import Style

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 3

# 样式层语句：比较骨架时整体剔除
_STYLE_LINE_RE = re.compile(r"^(classDef|class|style|linkStyle)\b")
# 节点定义上的 :::className 标记
_CLASS_MARK_RE = re.compile(r":::[\w-]+")


def _attach_run_log(log_path: Path) -> logging.FileHandler:
    """把风格转换过程同时写入 <output_dir>/run.log（与生成主循环同一约定）。"""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger("flowchart_agent").addHandler(handler)
    return handler


def _detach_run_log(handler: logging.FileHandler) -> None:
    logging.getLogger("flowchart_agent").removeHandler(handler)
    handler.close()


@dataclass
class RestyleResult:
    ok: bool
    code: str = ""
    image_path: Path | None = None
    error: str = ""
    rounds: int = 0


def structural_skeleton(code: str) -> list[str]:
    """剥掉样式层后的结构骨架：节点、文字、连线、方向声明。

    两份代码骨架逐行一致 = 内容与结构零改动。
    """
    lines: list[str] = []
    for raw in code.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):  # init 指令与注释
            continue
        if _STYLE_LINE_RE.match(line):  # classDef/class/style/linkStyle
            continue
        line = _CLASS_MARK_RE.sub("", line)  # :::class 标记
        line = re.sub(r"\s+", " ", line).rstrip(";").strip()
        if line:
            lines.append(line)
    return lines


class RestyleAgent:
    """风格转换子 Agent：LLM 只重写样式层，骨架校验 + 渲染校验把关。"""

    def __init__(self, settings: Settings):
        self._llm = LLMClient(settings.text_model)
        self._settings = settings

    def restyle(
        self,
        code: str,
        style: Style | None = None,
        spec: str = "",
        background: str | None = None,
        output_dir: str | Path = ".",
    ) -> RestyleResult:
        """style 为风格插件（含权威 init/background），spec 为自由风格文本；二者必居其一。"""
        style_spec = self._build_spec(style, spec)
        base_skeleton = structural_skeleton(code)
        bg = (style.background if style else None) or background or self._settings.render_background
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        handler = _attach_run_log(output_dir / "run.log")

        previous = ""
        feedback = ""
        try:
            logger.info(
                "任务开始：restyle_diagram(风格=%s，背景=%s，原代码 %d 字符)",
                style.name if style else f"自由描述（{len(spec)} 字符）",
                bg, len(code),
            )
            for round_no in range(1, _MAX_ROUNDS + 1):
                logger.info("风格转换 第 %d/%d 轮", round_no, _MAX_ROUNDS)
                raw = self._generate(code, style_spec, previous, feedback)
                new_code = extract_mermaid(raw)
                if not new_code:
                    feedback = "你的输出中没有可识别的 Mermaid 代码，请只输出 ```mermaid 代码块。"
                    logger.warning("风格转换 第 %d 轮：未提取到代码", round_no)
                    previous = raw
                    continue

                # 1. 骨架校验：样式层之外的任何改动都拒绝
                diff = _skeleton_diff(base_skeleton, structural_skeleton(new_code))
                if diff:
                    feedback = (
                        "你改动了图表的内容或结构，这是不允许的。只允许添加/替换样式层语句"
                        "（init 指令、classDef/class/style/linkStyle、:::class 标记）。\n"
                        f"差异如下：\n{diff}"
                    )
                    logger.warning("风格转换 第 %d 轮：骨架不一致\n%s", round_no, diff[:200])
                    previous = new_code
                    continue

                # 2. 渲染校验
                render = render_mermaid(
                    new_code, output_dir, stem=f"restyle_r{round_no}",
                    fmt=self._settings.output_format, background=bg,
                    chrome_path=self._settings.chrome_path,
                    scale=self._settings.render_scale,
                    width=self._settings.render_width,
                )
                if not render.ok:
                    feedback = prompts.RENDER_ERROR_FEEDBACK.format(error=render.error)
                    logger.warning("风格转换 第 %d 轮：渲染失败 -> %s", round_no, render.error[:200])
                    previous = new_code
                    continue

                logger.info("风格转换 第 %d 轮：校验通过 -> %s", round_no, render.image_path)
                return RestyleResult(
                    ok=True, code=new_code, image_path=render.image_path, rounds=round_no
                )

            logger.warning("风格转换达到最大轮次 %d，任务失败", _MAX_ROUNDS)
            return RestyleResult(
                ok=False, rounds=_MAX_ROUNDS,
                error=f"{_MAX_ROUNDS} 轮后仍未通过校验，最后的问题：{feedback}",
            )
        finally:
            _detach_run_log(handler)

    @staticmethod
    def _build_spec(style: Style | None, spec: str) -> str:
        if style is not None:
            parts = [f"风格模板 {style.name}：{style.description}"]
            if style.prompt_hint:
                parts.append(f"风格说明：{style.prompt_hint}")
            if style.init_directive:
                parts.append(f"主题指令（原样使用）：{style.init_directive}")
            if style.background:
                parts.append(f"画布背景色：{style.background}")
            return "\n".join(parts)
        return spec

    def _generate(self, code: str, spec: str, previous: str, feedback: str) -> str:
        if not previous:
            user = prompts.RESTYLE_USER.format(code=code, spec=spec)
        else:
            user = prompts.RESTYLE_REVISE_USER.format(
                code=code, spec=spec, previous=previous, feedback=feedback
            )
        return self._llm.chat(
            [
                {"role": "system", "content": prompts.RESTYLE_SYSTEM},
                {"role": "user", "content": user},
            ]
        )


def _skeleton_diff(base: list[str], new: list[str]) -> str:
    """返回骨架差异文本（给模型修复用）；空串表示一致。"""
    if base == new:
        return ""
    diff = difflib.unified_diff(base, new, fromfile="原始骨架", tofile="你的骨架",
                                lineterm="", n=1)
    return "\n".join(list(diff)[:30])

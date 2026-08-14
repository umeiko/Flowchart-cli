"""对话会话状态：持有当前流程图与累积需求，供 Skill 处理器读写。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .agent import FlowchartAgent
from .config import Settings
from .images import validate_image
from .mermaid import render_mermaid
from .styles import Style, get_style, load_styles


class DiagramSession:
    """一次 chat 会话中的流程图状态。

    - requirement：累积的需求描述（初始需求 + 每轮修改意见），用于生成与视觉验证；
    - current_code / current_image：最近一次验证通过的 Mermaid 代码与渲染图；
    - style：当前作图风格插件（styles/ 目录下的 markdown 文件），None 为默认；
    - 每次生成/修订在 output_dir/v<n>/ 下留存完整过程产物，
      成功结果同步到 output_dir/current.mmd / current.png / current.svg。
    """

    def __init__(self, settings: Settings, output_dir: str | Path):
        self._agent = FlowchartAgent(settings)
        self._chrome_path = settings.chrome_path
        self._output_dir = Path(output_dir)
        self._default_bg = settings.render_background
        self.requirement = ""
        self.current_code = ""
        self.current_image: Path | None = None
        self.version = 0
        self.style: Style | None = None  # 当前风格插件；None = 默认风格
        self._background_override: str | None = None  # 用户显式指定的画布背景色
        # 界面层的流式文本回调（生成阶段实时显示）；None = 非流式
        self.on_delta: Callable[[str], None] | None = None

    @property
    def has_diagram(self) -> bool:
        return bool(self.current_code)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def effective_style(self) -> Style | None:
        """实际生效的风格插件：显式选择的 > styles/default.md。

        default 插件始终被注入，用户编辑 styles/default.md 即可定制默认风格；
        default.md 不存在时退化为无风格（背景用 RENDER_BACKGROUND 配置）。
        """
        return self.style or load_styles().get("default")

    @property
    def background(self) -> str:
        """解析后的画布背景色：显式指定 > 生效风格插件 > RENDER_BACKGROUND 配置。"""
        style = self.effective_style
        return (
            self._background_override
            or (style.background if style else None)
            or self._default_bg
        )

    def list_styles(self) -> str:
        styles = load_styles()
        if not styles:
            return "styles 目录下没有可用的风格模板。"
        current_name = self.effective_style.name if self.effective_style else ""
        lines = []
        for s in styles.values():
            current = "（当前）" if s.name == current_name else ""
            lines.append(f"- {s.name}：{s.description}{current}")
        return "可用风格模板：\n" + "\n".join(lines)

    def set_style(self, name: str) -> str:
        try:
            self.style = get_style(name)
        except ValueError as e:
            return f"错误：{e}"
        return (
            f"已切换风格：{self.style.name}（{self.style.description}）。"
            "后续生成与修改将使用该风格。"
        )

    def create(
        self,
        requirement: str,
        image_path: str | None = None,
        background: str | None = None,
        style: str | None = None,
    ) -> str:
        self.requirement = requirement
        reference = None
        if image_path:
            try:
                reference = validate_image(image_path)
            except ValueError as e:
                return f"错误：参考图片不可用：{e}"
            self.requirement += f"\n\n（需求参考图片：{reference}）"
        if style:
            try:
                self.style = get_style(style)
            except ValueError as e:
                return f"错误：{e}"
            hint = f"\n{self.style.prompt_hint}" if self.style.prompt_hint else ""
            self.requirement += (
                f"\n\n（作图风格要求：{self.style.name}——{self.style.description}{hint}）"
            )
        if background:
            self._background_override = background
            self.requirement += f"\n\n（画布背景色要求：{background}）"
        return self._run(initial_code="", initial_feedback="", reference_image=reference)

    def modify(self, instruction: str) -> str:
        if not self.has_diagram:
            return "还没有生成过流程图，请先描述需求创建一张图。"
        self.requirement += f"\n\n追加修改要求：{instruction}"
        # 修订时把当前渲染图作为参考，让多模态生成模型"看到"要改什么
        return self._run(
            initial_code=self.current_code,
            initial_feedback=instruction,
            reference_image=self.current_image,
        )

    def _run(
        self,
        initial_code: str,
        initial_feedback: str,
        reference_image: Path | None = None,
    ) -> str:
        self.version += 1
        run_dir = self._output_dir / f"v{self.version}"
        result = self._agent.run(
            self.requirement,
            run_dir,
            initial_code=initial_code,
            initial_feedback=initial_feedback,
            reference_image=reference_image,
            background=self._background_override,
            style=self.effective_style,
            on_delta=self.on_delta,
        )
        if not result.success:
            feedback = result.final_feedback or "未知原因"
            return (
                f"生成失败：{len(result.rounds)} 轮后仍未通过验证。\n"
                f"最后的验证意见：{feedback}\n过程日志见 {run_dir}/run.log"
            )
        self.current_code = result.mermaid_code
        self.current_image = result.image_path
        # 同步到固定文件名（mmd/png/svg），方便用户查看"当前这张图"
        final_mmd = self._output_dir / "current.mmd"
        final_mmd.write_text(self.current_code, encoding="utf-8")
        current_img = None
        if result.image_path:
            current_img = self._output_dir / f"current{result.image_path.suffix}"
            shutil.copy(result.image_path, current_img)
        svg = render_mermaid(
            result.mermaid_code, self._output_dir,
            stem="current", fmt="svg", background=self.background,
            chrome_path=self._chrome_path,
        )
        svg_note = f"\nSVG：{svg.image_path}" if svg.ok else ""
        return (
            f"成功（{len(result.rounds)} 轮通过验证）。\n"
            f"Mermaid 代码：{final_mmd}\n"
            f"渲染图片：{current_img}{svg_note}\n"
            f"过程日志：{run_dir}/run.log"
        )

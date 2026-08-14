"""对话会话状态：持有当前流程图与累积需求，供 Skill 处理器读写。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from .agent import FlowchartAgent
from .config import Settings
from .images import validate_image
from .mermaid import render_mermaid
from .styles import Style, get_style, load_styles
from .skillpacks import get_skill_pack, load_skill_packs

logger = logging.getLogger(__name__)


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
        self._settings = settings
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
        # 界面层的轮次开始回调（清空上一轮流式显示）；None = 不需要
        self.on_round_start: Callable[[int], None] | None = None
        # 视觉检视强度：full=完整（排版+内容语义），layout=仅基础图形检视
        self.verify_mode = settings.verify_mode

    def set_verify_mode(self, mode: str) -> str:
        """切换检视强度（set_verification 工具的 handler）。"""
        mode = mode.strip().lower()
        if mode not in ("full", "layout", "code"):
            return f"错误：未知检视强度 {mode!r}，可选：full、layout、code。"
        self.verify_mode = mode
        if mode == "layout":
            return (
                "已切换为基础图形检视（layout）：只检查排版、遮挡、连线结构，"
                "不再逐字核对内容。适用于视觉模型文字识别能力较弱的场景。"
            )
        if mode == "code":
            return (
                "已切换为代码检视（code）：不看渲染图，文本模型直接审查 Mermaid "
                "源码的内容与逻辑。完全没有视觉模型时的兜底方案；"
                "排版/遮挡类渲染问题此模式下查不出来。"
            )
        return "已切换为完整检视（full）：排版结构 + 内容与逻辑核对。"

    @property
    def working_doc_path(self) -> Path:
        """工作文档路径：整合素材信息与初步生成方案的中间产物（markdown）。"""
        return self._output_dir / "working_doc.md"

    def read_working_doc(self) -> str:
        """read_working_doc 工具的 handler。"""
        if not self.working_doc_path.is_file():
            return "（工作文档还没有内容，可用 write_working_doc 创建）"
        return self.working_doc_path.read_text(encoding="utf-8")

    def write_working_doc(self, content: str) -> str:
        """write_working_doc 工具的 handler：整体覆盖写入（先读后改即可局部修订）。"""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self.working_doc_path.write_text(content, encoding="utf-8")
        return f"工作文档已更新（{len(content)} 字符）：{self.working_doc_path}"

    def list_skill_packs(self) -> str:
        """列出 skills/ 目录下所有技能包（list_skill_packs 工具的 handler）。"""
        packs = load_skill_packs()
        if not packs:
            return "skills 目录下没有可用的技能包。"
        return "可用技能包：\n" + "\n".join(
            f"- {p.name}：{p.description}" for p in packs.values()
        )

    def use_skill(self, name: str) -> str:
        """读取技能包完整指引（use_skill 工具的 handler）。"""
        try:
            pack = get_skill_pack(name)
        except ValueError as e:
            return f"错误：{e}"
        return (
            f"以下是技能包 {pack.name} 的操作指引，请严格遵照执行：\n\n"
            f"{pack.instructions}"
        )

    def create_style(self, name: str, description: str) -> str:
        """风格生成子 Agent 入口（create_style 工具的 handler）。

        校验通过的风格落盘到 styles/ 后自动切换为当前风格，即刻生效。
        """
        from .style_agent import StyleAgent  # 延迟导入：仅用到时加载

        result = StyleAgent(self._settings).create(name, description)
        if not result.ok:
            return f"风格生成失败：{result.error}"
        try:
            self.style = get_style(name)  # 重新扫描 styles/，拿到刚落盘的插件
        except ValueError as e:  # 理论上刚校验过不会到这步，兜底
            return f"风格文件已生成（{result.path}），但加载失败：{e}"
        return (
            f"风格插件已生成（{result.rounds} 轮通过校验）：{result.path}\n"
            f"已自动切换为当前风格：{self.style.name}（{self.style.description}）\n"
            "后续生成与修改将使用该风格。"
        )

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
        if initial_code:
            action = f"modify_diagram(修改意见：{initial_feedback[:100]})"
        else:
            action = (
                f"create_diagram(需求 {len(self.requirement)} 字符"
                + (f"，参考图 {reference_image}" if reference_image else "")
                + ")"
            )
        result = self._agent.run(
            self.requirement,
            run_dir,
            initial_code=initial_code,
            initial_feedback=initial_feedback,
            reference_image=reference_image,
            background=self._background_override,
            style=self.effective_style,
            on_delta=self.on_delta,
            verify_mode=self.verify_mode,
            on_round_start=self.on_round_start,
            action=action,
        )
        if not result.success:
            feedback = result.final_feedback or "未知原因"
            return (
                f"生成失败：{len(result.rounds)} 轮后仍未通过验证。\n"
                f"最后的验证意见：{feedback}\n过程日志见 {run_dir}/run.log"
            )
        return self._publish(
            result.mermaid_code, result.image_path, run_dir,
            f"成功（{len(result.rounds)} 轮通过验证）。",
        )

    def restyle(self, style_name: str | None = None, style_document: str | None = None) -> str:
        """风格转换子 Agent 入口（restyle_diagram 工具的 handler）。

        只调整样式层，内容与结构由骨架校验保证零改动。风格来源二选一：
        现有风格模板（style_name）或自由风格文本（style_document）。
        """
        if not self.has_diagram:
            return "还没有生成过流程图，请先描述需求创建一张图。"
        style_obj = None
        spec = ""
        if style_name:
            try:
                style_obj = get_style(style_name)
            except ValueError as e:
                return f"错误：{e}"
        elif style_document and style_document.strip():
            spec = style_document.strip()
        else:
            return "错误：需要 style_name（现有风格模板）或 style_document（风格要求文本）之一。"

        from .restyle_agent import RestyleAgent  # 延迟导入：仅用到时加载

        self.version += 1
        run_dir = self._output_dir / f"v{self.version}"
        result = RestyleAgent(self._settings).restyle(
            self.current_code,
            style=style_obj,
            spec=spec,
            background=self._background_override,
            output_dir=run_dir,
        )
        if not result.ok:
            return f"风格转换失败：{result.error}\n过程产物见 {run_dir}"
        if style_obj:
            self.style = style_obj  # 后续生成/修改也沿用该风格
        return self._publish(
            result.code, result.image_path, run_dir,
            f"风格转换成功（{result.rounds} 轮通过校验，内容与结构未改动）。",
        )

    def _publish(self, code: str, image_path: Path | None, run_dir: Path, note: str) -> str:
        """把最新结果同步为会话当前状态，并落到 current.mmd / current.png / current.svg。"""
        self.current_code = code
        self.current_image = image_path
        final_mmd = self._output_dir / "current.mmd"
        final_mmd.write_text(code, encoding="utf-8")
        current_img = None
        if image_path:
            current_img = self._output_dir / f"current{image_path.suffix}"
            shutil.copy(image_path, current_img)
        svg = render_mermaid(
            code, self._output_dir,
            stem="current", fmt="svg", background=self.background,
            chrome_path=self._settings.chrome_path,
        )
        svg_note = f"\nSVG：{svg.image_path}" if svg.ok else ""
        logger.info(
            "产物发布：%s；%s%s；过程产物 %s",
            note, final_mmd, f" {current_img}" if current_img else "", run_dir,
        )
        return (
            f"{note}\n"
            f"Mermaid 代码：{final_mmd}\n"
            f"渲染图片：{current_img}{svg_note}\n"
            f"过程产物：{run_dir}"
        )

"""对话会话状态：持有当前流程图与累积需求，供 Skill 处理器读写。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from .agent import FlowchartAgent
from .config import Settings
from .drawio import (
    check_drawio_available,
    flow_grid_from_spec,
    make_flow_grid,
    render_drawio,
)
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
        # 绝对路径：工具返回与 CLI 展示的产物位置始终是绝对路径
        self._output_dir = Path(output_dir).resolve()
        self._default_bg = settings.render_background
        self.requirement = ""
        self.current_code = ""
        self.current_image: Path | None = None
        self.version = 0
        self.style: Style | None = None  # 当前风格插件；None = 默认风格
        self._background_override: str | None = None  # 用户显式指定的画布背景色
        # drawio 流程图的节点尺寸/间距覆盖（create_diagram 的可选布局参数，
        # FlowGrid；None = 默认 220×70/间距 60），修改图时沿用
        self._flow_grid = None
        # 已读技能包的 prompt_hint（技能名 → 要求文本），create/modify 时
        # 注入需求文本直通生成子模型
        self._skill_hints: dict[str, str] = {}
        # 界面层的流式文本回调（生成阶段实时显示）；None = 非流式
        self.on_delta: Callable[[str], None] | None = None
        # 界面层的思考流回调（推理模型 reasoning_content，仅提示用）；None = 不需要
        self.on_reasoning: Callable[[str], None] | None = None
        # 界面层的轮次开始回调（清空上一轮流式显示）；None = 不需要
        self.on_round_start: Callable[[int], None] | None = None
        # 视觉检视强度：full=完整（排版+内容语义），layout=仅基础图形检视
        self.verify_mode = settings.verify_mode
        # 出图引擎：mermaid（默认）或 drawio（LLM 直出 draw.io XML，
        # 桌面版渲染，产物可编辑）。默认值来自 .env 的 OUTPUT_ENGINE，
        # 会话中可用 set_output_engine 工具切换。
        self.engine = settings.output_engine
        if self.engine == "drawio":
            unavailable = check_drawio_available(settings.drawio_path)
            if unavailable:
                logger.warning("drawio 引擎不可用：%s", unavailable)

    def set_output_engine(self, engine: str) -> str:
        """切换出图引擎（set_output_engine 工具的 handler）。"""
        engine = engine.strip().lower()
        if engine not in ("mermaid", "drawio"):
            return f"错误：未知出图引擎 {engine!r}，可选：mermaid、drawio。"
        if engine == "drawio":
            unavailable = check_drawio_available(self._settings.drawio_path)
            if unavailable:
                return f"暂时无法切换到 drawio 模式：{unavailable}"
        self.engine = engine
        if engine == "drawio":
            return (
                "已切换为 drawio 模式：后续生成将直接产出 draw.io 原生文件"
                "（组件等大、网格对齐，可导入 draw.io/Visio/亿图二次编辑），"
                "由本机 draw.io 桌面版渲染。流程图/架构图会自动路由到对应的"
                "提示词与布局管线；配色遵循风格模板（styles/）中的 drawio "
                "规则段，模板未提供时使用内置默认配色。"
            )
        return "已切换为 mermaid 模式：后续生成将产出 Mermaid 代码并用 mmdc 渲染。"

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
        """读取技能包完整指引（use_skill 工具的 handler）。

        技能包 frontmatter 带 layout 时（如 node_width=172,gap_y=28），
        此处直接解析存进会话布局（后续 create/modify 自动沿用）——
        在"读技能"这一刻确定性生效，不靠模型长上下文记住传参。
        """
        try:
            pack = get_skill_pack(name)
        except ValueError as e:
            return f"错误：{e}"
        note = ""
        if pack.prompt_hint:
            self._skill_hints[pack.name] = pack.prompt_hint
            note += (
                f"\n\n（该技能的作图要求已直通生成模型：{pack.prompt_hint}）"
            )
        if pack.layout:
            try:
                self._flow_grid = flow_grid_from_spec(pack.layout)
                logger.info("技能包 %s 布局参数已生效：%s", pack.name, pack.layout)
                note = (
                    f"\n\n（该技能的布局参数已自动生效：{pack.layout}，"
                    "后续 create_diagram/modify_diagram 会自动沿用，"
                    "无需再传 node_width 等参数。）"
                )
            except ValueError as e:
                logger.warning("技能包 %s 的 layout 串非法：%s", pack.name, e)
                note = f"\n\n（警告：该技能的 layout 配置非法已忽略：{e}）"
        return (
            f"以下是技能包 {pack.name} 的操作指引，请严格遵照执行：\n\n"
            f"{pack.instructions}{note}"
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
        node_width=None,
        node_height=None,
        gap_x=None,
        gap_y=None,
    ) -> str:
        logger.info(
            "create_diagram 参数：requirement=%d字符 image=%s background=%s "
            "style=%s node_width=%s node_height=%s gap_x=%s gap_y=%s",
            len(requirement), image_path or "无", background or "无",
            style or "无", node_width, node_height, gap_x, gap_y,
        )
        try:
            tool_grid = make_flow_grid(
                node_width, node_height, gap_x, gap_y, base=self._flow_grid)
        except ValueError as e:
            return f"错误：{e}"
        # 仅当模型显式传了布局参数才覆盖：在现有网格（可能是技能包
        # frontmatter 生效的）上微调；不传则原样沿用，绝不能重置回默认
        if tool_grid is not None:
            self._flow_grid = tool_grid
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
        self._inject_skill_hints()
        return self._run(initial_code="", initial_feedback="", reference_image=reference)

    def _inject_skill_hints(self) -> None:
        """把已读技能包的 prompt_hint 注入需求文本（直通生成子模型）。

        技能正文只有主 Agent 可见，子模型只认 requirement；长上下文下靠
        主 Agent 抄录不可靠，故在 use_skill 时收集、此处确定性注入。
        requirement 跨轮次累积，已注入过的（按文本去重）不重复追加。
        """
        hints = [
            h for h in self._skill_hints.values()
            if h and h not in self.requirement
        ]
        if hints:
            self.requirement += "\n\n（附加作图要求：" + "；".join(hints) + "）"

    def modify(self, instruction: str) -> str:
        if not self.has_diagram:
            return "还没有生成过流程图，请先描述需求创建一张图。"
        self.requirement += f"\n\n追加修改要求：{instruction}"
        self._inject_skill_hints()  # 技能可能在首图之后才读，修改时同样注入
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
            on_reasoning=self.on_reasoning,
            verify_mode=self.verify_mode,
            on_round_start=self.on_round_start,
            action=action,
            engine=self.engine,
            flow_grid=self._flow_grid,
        )
        if not result.success:
            feedback = result.final_feedback or "未知原因"
            if not result.mermaid_code:
                return (
                    f"生成失败：{len(result.rounds)} 轮后仍未产出可用的图表代码。\n"
                    f"最后的验证意见：{feedback}\n过程日志见 {run_dir}/run.log"
                )
            # 兜底发布，current.* 不留空：有可渲染版本就发真图，
            # 全部渲不出来时发失败说明卡片（agent.run 已按此二选一）
            rendered = any(r.image_path is not None for r in result.rounds)
            note = (
                "已保留最后一版可渲染的图供参考（未通过检视）。"
                if rendered
                else "所有轮次均未能渲染出图，已生成失败说明卡片（失败代码与原因见图）。"
            )
            return self._publish(
                result.mermaid_code, result.image_path, run_dir,
                f"达到最大轮次（{len(result.rounds)} 轮）未通过验证，{note}\n"
                f"最后的验证意见：{feedback}",
            )
        return self._publish(
            result.mermaid_code, result.image_path, run_dir,
            f"成功（{len(result.rounds)} 轮通过验证）。",
        )

    def restyle(self, style_name: str | None = None, style_document: str | None = None) -> str:
        """风格转换子 Agent 入口（restyle_diagram 工具的 handler）。

        只调整样式层，内容与结构由骨架校验保证零改动。风格来源二选一：
        现有风格模板（style_name）或自由风格文本（style_document）。
        仅 mermaid 引擎支持（drawio 引擎配色内置在生成提示词中）。
        """
        if self.engine == "drawio":
            return (
                "drawio 模式下不支持风格转换（风格模板是 Mermaid 概念）；"
                "可用 set_output_engine 切回 mermaid，或直接描述配色要求重新生成。"
            )
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
        """把最新结果同步为会话当前状态，并落到 current.mmd / current.drawio /
        current.png / current.svg（按当前出图引擎选择源码格式）。"""
        self.current_code = code
        self.current_image = image_path
        if self.engine == "drawio":
            src_label = "drawio 文件"
            final_src = self._output_dir / "current.drawio"
            final_src.write_text(code, encoding="utf-8")
            current_img = None
            if image_path:
                current_img = self._output_dir / f"current{image_path.suffix}"
                shutil.copy(image_path, current_img)
            svg_path = render_drawio(
                final_src, self._output_dir / "current.svg",
                self._settings.drawio_path, fmt="svg",
            )
            svg_note = f"\nSVG：{svg_path}" if svg_path else ""
        else:
            src_label = "Mermaid 代码"
            final_src = self._output_dir / "current.mmd"
            final_src.write_text(code, encoding="utf-8")
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
            note, final_src, f" {current_img}" if current_img else "", run_dir,
        )
        return (
            f"{note}\n"
            f"{src_label}：{final_src}\n"
            f"渲染图片：{current_img}{svg_note}\n"
            f"过程产物：{run_dir}"
        )

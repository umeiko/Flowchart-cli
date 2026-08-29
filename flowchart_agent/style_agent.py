"""风格生成子 Agent：自然语言描述 → styles/ 风格插件文件。

流程：文本模型生成风格 markdown → 结构校验（用风格系统的同一 parser 解析
frontmatter）→ 试渲染校验（把风格应用到示例图跑一遍 mmdc，抓出非法 init
主题指令）→ 失败把错误反馈给模型修复，最多若干轮。产出即标准风格插件，
落盘后即刻可被 list_styles 发现。
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .config import Settings
from .llm import LLMClient
from .mermaid import render_mermaid
from .styles import load_styles, styles_dir

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 3

# 风格名必须是安全的文件名标识（list_styles/get_style 按小写名索引）
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# 试渲染用的示例图：覆盖起止/处理/判断/分支标签，足以暴露 init 与配色问题
_SAMPLE_CODE = """flowchart TD
  A([开始]) --> B["示例步骤"]
  B --> C{"是否通过?"}
  C -->|是| D([结束])
  C -->|否| B
"""


@dataclass
class StyleResult:
    ok: bool
    path: Path | None = None
    error: str = ""
    rounds: int = 0


class StyleAgent:
    """生成风格插件的子 Agent。校验通过才写盘，写盘即可被主 Agent 发现。"""

    def __init__(self, settings: Settings, directory: str | Path | None = None):
        self._llm = LLMClient(settings.text_model)
        self._settings = settings
        self._directory = Path(directory).resolve() if directory is not None else styles_dir()

    def create(self, name: str, description: str) -> StyleResult:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            return StyleResult(
                ok=False,
                error=f"风格标识 {name!r} 不合法：只能含小写字母/数字/下划线/连字符，"
                "且以字母或数字开头（如 handdrawn、business-blue）。",
            )
        target = self._directory / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return StyleResult(
                ok=False,
                error=f"风格 {name!r} 已存在（{target}）。"
                "如需覆盖请先删除该文件，或换一个风格标识。",
            )

        previous = ""
        feedback = ""
        for round_no in range(1, _MAX_ROUNDS + 1):
            logger.info("风格生成 第 %d/%d 轮（%s）", round_no, _MAX_ROUNDS, name)
            raw = self._generate(name, description, previous, feedback)
            content = _extract_style_md(raw)

            error = self._validate(name, content)
            if error is None:
                target.write_text(content, encoding="utf-8")
                logger.info("风格 %s 生成成功 -> %s", name, target)
                return StyleResult(ok=True, path=target, rounds=round_no)
            logger.warning("风格生成 第 %d 轮校验失败：%s", round_no, error[:200])
            previous, feedback = content, error

        return StyleResult(
            ok=False, rounds=_MAX_ROUNDS,
            error=f"{_MAX_ROUNDS} 轮后仍未通过校验，最后的问题：{feedback}",
        )

    def _generate(self, name: str, description: str, previous: str, feedback: str) -> str:
        if not previous:
            user = prompts.STYLE_GENERATE_USER.format(name=name, description=description)
        else:
            user = prompts.STYLE_REVISE_USER.format(
                name=name, description=description, previous=previous, feedback=feedback
            )
        return self._llm.chat(
            [
                {"role": "system", "content": prompts.STYLE_GENERATE_SYSTEM},
                {"role": "user", "content": user},
            ]
        )

    def _validate(self, name: str, content: str) -> str | None:
        """返回 None 表示通过。结构校验 + init 格式检查 + 试渲染校验
        （在临时目录，不污染 styles/）。"""
        with tempfile.TemporaryDirectory(prefix="flowchart_style_") as tmp:
            tmp_path = Path(tmp)
            candidate = tmp_path / f"{name}.md"
            candidate.write_text(content, encoding="utf-8")

            # 结构校验：风格系统自己的 parser 能认出才算合格插件
            parsed = load_styles(tmp_path).get(name)
            if parsed is None:
                return (
                    "文件格式不符合风格插件规范：需要 --- 包裹的 frontmatter，"
                    "且必须包含 name 与 description 两行；"
                    f"name 必须是 {name!r}。请对照示例修正。"
                )

            # init 格式检查：mermaid 对畸形 init 指令很宽容（静默忽略），
            # 渲染器抓不出来，这里做包裹格式与花括号配对检查
            init = parsed.init_directive
            if init and (
                not (init.startswith("%%{init:") and init.endswith("}%%"))
                or init.count("{") != init.count("}")
                or "\n" in init
            ):
                return (
                    f"init 指令格式不完整：{init!r}。"
                    "应为单行 %%{init: {...}}%%，JSON 用单引号且花括号配对；"
                    "不确定时宁可省略 init 行。"
                )

            # 试渲染校验：把风格应用到示例图真实渲染一次，
            # 背景色值等问题在这一步暴露
            render = render_mermaid(
                parsed.apply(_SAMPLE_CODE),
                tmp_path,
                stem="style_check",
                background=parsed.background or self._settings.render_background,
                chrome_path=self._settings.chrome_path,
            )
            if not render.ok:
                return f"风格应用到示例图后渲染失败：{render.error[:500]}"
        return None


def _extract_style_md(raw: str) -> str:
    """容忍模型用 ``` 围栏包裹输出：取第一个代码块内容，否则取全文。"""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", raw, re.DOTALL)
    text = m.group(1) if m else raw
    return text.strip() + "\n"

"""作图风格插件系统：styles/ 目录下的 markdown 文件即插件。

每个 .md 文件用 frontmatter 定义风格（name/description/background/init），
正文是给生成模型的补充风格说明；正文中 `## [engine:type] 标题` 标记的段落
是对应引擎/图型的专属规则（如 [drawio:flowchart]），只注入对应引擎，
不并入通用 prompt_hint。主 Agent 通过 list_styles 自行发现、
按需选用；用户往目录里丢一个 .md 文件即可新增风格，无需改代码。
目录可用 FLOWCHART_STYLE_DIR 环境变量覆盖；默认 ./styles（冻结时为 exe 旁 styles/）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import runtime

# 引擎专属风格段的标题标记：## [engine:type] 人类可读标题
# 这些段落不并入通用 prompt_hint，只在对应引擎生成时注入。
_ENGINE_SECTION = re.compile(r"^##\s+\[([a-z]+:[a-z_]+)\]\s*(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Style:
    name: str
    description: str
    background: str | None = None  # 画布背景色；None = 跟随 RENDER_BACKGROUND 配置
    init_directive: str = ""  # 注入代码开头的 %%{init: ...}%% 主题指令，空串不注入
    prompt_hint: str = ""  # markdown 正文：并入需求描述给生成模型的风格说明
    engine_hints: dict[str, str] | None = None  # 引擎专属规则段，key 如 "drawio:flowchart"

    def engine_hint(self, engine: str, diagram_type: str) -> str:
        """取指定引擎/图型的风格规则段，没有时返回空串（调用方用内置默认）。"""
        return (self.engine_hints or {}).get(f"{engine}:{diagram_type}", "")

    def apply(self, code: str) -> str:
        """把风格指令注入 Mermaid 代码开头；代码里已有 init 指令时不重复注入。"""
        if not self.init_directive or "%%{init" in code:
            return code
        return f"{self.init_directive}\n{code}"


def styles_dir() -> Path:
    """风格目录：FLOWCHART_STYLE_DIR 覆盖 > 冻结时 exe 旁 styles/ > CWD 下 styles/。"""
    env = os.getenv("FLOWCHART_STYLE_DIR")
    if env:
        return Path(env)
    return runtime.app_dir() / "styles" if runtime.is_frozen() else Path("styles")


def load_styles(directory: Path | None = None) -> dict[str, Style]:
    """扫描 styles 目录，返回 {name: Style}。每次调用都重新扫描，新增文件即时生效。"""
    d = directory or styles_dir()
    styles: dict[str, Style] = {}
    if not d.is_dir():
        return styles
    for path in sorted(d.glob("*.md")):
        style = _parse_style_file(path)
        if style is not None:
            styles[style.name] = style
    return styles


def get_style(name: str, directory: Path | None = None) -> Style:
    """按名称取风格插件，不存在时抛 ValueError（附可用风格列表）。"""
    styles = load_styles(directory)
    style = styles.get(name.strip().lower())
    if style is None:
        available = "、".join(styles) or "（styles 目录为空）"
        raise ValueError(f"未知风格 {name!r}，可用风格：{available}")
    return style


def _parse_style_file(path: Path) -> Style | None:
    """解析 frontmatter（--- 包裹的 key: value 行）；无 frontmatter 的文件忽略。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return None
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("'\"")
    if not meta.get("name") or not meta.get("description"):
        return None
    prompt_hint, engine_hints = _split_engine_sections(parts[2].strip())
    return Style(
        name=meta["name"].lower(),
        description=meta["description"],
        background=meta.get("background") or None,
        init_directive=meta.get("init", ""),
        prompt_hint=prompt_hint,
        engine_hints=engine_hints,
    )


def _split_engine_sections(body: str) -> tuple[str, dict[str, str]]:
    """把正文中 `## [engine:type] 标题` 标记的段落拆出来。

    返回 (通用 prompt_hint, {engine:type: 段落文本})。被拆出的段落不再并入
    通用 prompt_hint（避免 mermaid 生成时被 drawio 规则污染）；段落文本保留
    标题中人类可读的部分（去掉 [engine:type] 标记）。
    """
    matches = list(_ENGINE_SECTION.finditer(body))
    if not matches:
        return body, {}
    # 任何二级标题（无论是否带标记）都结束上一个被拆出的段落
    all_heads = [m.start() for m in re.finditer(r"^## ", body, re.MULTILINE)]
    hints: dict[str, str] = {}
    generic_parts: list[str] = []
    prev_end = 0
    for m in matches:
        generic_parts.append(body[prev_end:m.start()])
        end = next((h for h in all_heads if h > m.start()), len(body))
        title = m.group(2).strip()
        text = body[m.start():end].strip()
        if title:
            text = text.replace(m.group(0), f"## {title}", 1)
        hints[m.group(1)] = text
        prev_end = end
    generic = "\n\n".join(p.strip() for p in generic_parts if p.strip())
    return generic, hints

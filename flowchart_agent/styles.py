"""作图风格插件系统：styles/ 目录下的 markdown 文件即插件。

每个 .md 文件用 frontmatter 定义风格（name/description/background/init），
正文是给生成模型的补充风格说明。主 Agent 通过 list_styles 自行发现、
按需选用；用户往目录里丢一个 .md 文件即可新增风格，无需改代码。
目录可用 FLOWCHART_STYLE_DIR 环境变量覆盖，默认 ./styles。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Style:
    name: str
    description: str
    background: str | None = None  # 画布背景色；None = 跟随 RENDER_BACKGROUND 配置
    init_directive: str = ""  # 注入代码开头的 %%{init: ...}%% 主题指令，空串不注入
    prompt_hint: str = ""  # markdown 正文：并入需求描述给生成模型的风格说明

    def apply(self, code: str) -> str:
        """把风格指令注入 Mermaid 代码开头；代码里已有 init 指令时不重复注入。"""
        if not self.init_directive or "%%{init" in code:
            return code
        return f"{self.init_directive}\n{code}"


def styles_dir() -> Path:
    return Path(os.getenv("FLOWCHART_STYLE_DIR", "styles"))


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
    return Style(
        name=meta["name"].lower(),
        description=meta["description"],
        background=meta.get("background") or None,
        init_directive=meta.get("init", ""),
        prompt_hint=parts[2].strip(),
    )

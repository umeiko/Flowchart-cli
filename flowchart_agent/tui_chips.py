"""TUI 图片芯片：拖入（粘贴）的图片路径显示为彩色芯片，可整块退格删除。

工作原理：
- 终端里把文件拖入窗口，本质是 bracketed paste 一段路径文本。拦截粘贴事件，
  若内容是可用的图片路径，则在输入框中插入占位 token（[图片:文件名]），
  并记录 token → 真实路径的映射；
- 显示层用 Processor 给 token 上色，用户一眼认出是图片；
- Backspace 时若光标前刚好是一个 token，则一次整块删除；
- 提交时把 token 还原成路径，并收集图片列表传给主 Agent。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.processors import Processor, Transformation

from .images import validate_image

_CHIP_RE = re.compile(r"\[图片:[^\[\]]+\]")
_CHIP_AT_END_RE = re.compile(r"\[图片:[^\[\]]+\]$")


class ChipRegistry:
    """图片芯片的注册表：token ↔ 真实路径。"""

    def __init__(self):
        self._map: dict[str, Path] = {}
        self._counter = 0

    def register_paste(self, data: str) -> str:
        """处理粘贴文本：是图片路径则返回芯片 token，否则原样返回。"""
        text = data.strip()
        if not text or "\n" in text:
            return data
        path = _normalize_path(text)
        if path is None:
            return data
        try:
            path = validate_image(path)
        except ValueError:
            return data
        name = path.name
        token = f"[图片:{name}]"
        while token in self._map:  # 同名文件重复拖入时去重
            self._counter += 1
            token = f"[图片:{name}#{self._counter}]"
        self._map[token] = path
        return token

    def resolve(self, text: str) -> tuple[str, list[Path]]:
        """提交时调用：token 还原为路径，返回 (还原后文本, 图片路径列表)。"""
        images: list[Path] = []

        def _repl(m: re.Match) -> str:
            path = self._map.get(m.group(0))
            if path is None:
                return m.group(0)
            images.append(path)
            return str(path)

        return _CHIP_RE.sub(_repl, text), images


def _normalize_path(text: str) -> Path | None:
    """终端拖入的路径可能带引号、反斜杠转义或 file:// 前缀，统一还原。"""
    t = text.strip().strip("'\"")
    if t.startswith("file://"):
        t = unquote(urlparse(t).path)
    t = t.replace("\\ ", " ")  # Terminal.app 对空格的转义
    p = Path(t).expanduser()
    return p if p.is_file() else None


class ChipColorProcessor(Processor):
    """把 [图片:xxx] token 渲染成 chip 样式（只改样式不改文本，光标位置不受影响）。"""

    def apply_transformation(self, ti) -> Transformation:
        text = fragment_list_to_text(ti.fragments)
        fragments: list[tuple[str, str]] = []
        last = 0
        for m in _CHIP_RE.finditer(text):
            if m.start() > last:
                fragments.append(("", text[last : m.start()]))
            fragments.append(("class:chip", m.group(0)))
            last = m.end()
        fragments.append(("", text[last:]))
        return Transformation(fragments)


def make_key_bindings(registry: ChipRegistry) -> KeyBindings:
    """粘贴转芯片 + 芯片整块退格删除。"""
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _paste(event):
        event.current_buffer.insert_text(registry.register_paste(event.data))

    @kb.add(Keys.Backspace)
    def _backspace(event):
        buf = event.current_buffer
        before = buf.text[: buf.cursor_position]
        m = _CHIP_AT_END_RE.search(before)
        if m:
            buf.delete_before_cursor(len(m.group(0)))
        else:
            buf.delete_before_cursor(1)

    return kb

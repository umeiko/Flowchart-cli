"""TUI 文件芯片：拖入（粘贴）的文件路径显示为彩色芯片，可整块退格删除。

工作原理：
- 终端里把文件拖入窗口，本质是 bracketed paste 一段路径文本。拦截粘贴事件，
  若内容是存在的文件路径（不限类型，文档/图片/表格均可），则在输入框中插入
  占位 token（[文件:文件名]），并记录 token → 真实路径的映射；
  Windows 上 prompt_toolkit 对单行粘贴识别不全（legacy reader 只认含换行的
  粘贴、VT100 reader 依赖终端发 bracketed paste 标记），由
  create_loose_paste_input() 的突发检测兜底；
- 多文件连拖（引号分隔或无分隔连写）会拆成多个芯片；
- 显示层用 Processor 给 token 上色，用户一眼认出是文件；
- Backspace 时若光标前刚好是一个 token，则一次整块删除；
- 提交时把 token 还原成完整路径留在文本里（模型看到的是真实路径），
  其中图片文件额外收集为图片列表传给主 Agent（视觉模型随消息看图）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.processors import Processor, Transformation

from .images import validate_image

_CHIP_RE = re.compile(r"\[文件:[^\[\]]+\]")
_CHIP_AT_END_RE = re.compile(r"\[文件:[^\[\]]+\]$")

# Windows 绝对路径起点（如 C:\ 或 d:/）；Windows 文件名不允许出现冒号，
# 所以粘贴文本里每个该模式都是一条新路径的起点（多文件无分隔连拖也安全）
_PATH_START = re.compile(r"[a-zA-Z]:[\\/]")

# 一次读取中连续纯文本键达到该数量即视为粘贴（Windows 粘贴检测的突发阈值）
_PASTE_BURST = 8


class ChipRegistry:
    """文件芯片的注册表：token ↔ 真实路径。"""

    def __init__(self):
        self._map: dict[str, Path] = {}
        self._counter = 0

    def register_paste(self, data: str) -> str:
        """处理粘贴文本：识别其中的文件路径并替换为芯片 token。

        整段是单个文件 → 单芯片；多文件连拖（引号分隔或无分隔连写）→
        每个文件各成一个芯片，其余文本原样保留；识别不到文件原样返回。
        """
        text = data.strip()
        if not text or "\n" in text or "\r" in text:
            return data
        path = _normalize_path(text)
        if path is not None:  # 快路径：整段就是单个文件
            return self._make_token(path)
        segments = _extract_paths(text)
        if segments is None:
            return data
        parts: list[str] = []
        for seg in segments:
            parts.append(self._make_token(seg) if isinstance(seg, Path) else seg)
        result = "".join(parts)
        # 引号紧贴芯片时去掉（拖入多文件时常带引号），相邻芯片之间补空格
        # （无分隔连拖场景），保证还原成路径后彼此可分
        result = re.sub(r"['\"](?=\[文件:)", "", result)
        result = re.sub(r"(?<=\])['\"]", "", result)
        result = result.replace("][文件:", "] [文件:")
        return result.strip() or data

    def _make_token(self, path: Path) -> str:
        name = path.name
        token = f"[文件:{name}]"
        while token in self._map:  # 同名文件重复拖入时去重
            self._counter += 1
            token = f"[文件:{name}#{self._counter}]"
        self._map[token] = path
        return token

    def resolve(self, text: str) -> tuple[str, list[Path]]:
        """提交时调用：token 还原为完整路径，返回 (还原后文本, 图片路径列表)。

        所有芯片都以完整路径形式留在文本里（模型的 prompt 拿到的是真实路径）；
        其中能通过图片校验的文件额外进入图片列表，供多模态消息附带。
        """
        images: list[Path] = []

        def _repl(m: re.Match) -> str:
            path = self._map.get(m.group(0))
            if path is None:
                return m.group(0)
            try:
                images.append(validate_image(path))
            except ValueError:
                pass  # 非图片（或超限图片）：路径已在文本里，不附带图片内容
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


def _extract_paths(text: str) -> list[str | Path] | None:
    """扫描文本中的 Windows 绝对路径（支持引号包裹、空格分隔、无分隔连写）。

    返回分段列表（str 原文段 / Path 文件段）；一个文件都没识别到返回 None。
    """
    if not _PATH_START.search(text):
        return None
    segments: list[str | Path] = []
    pos = 0
    found = False
    while True:
        m = _PATH_START.search(text, pos)
        if not m:
            break
        path, end = _longest_existing_path(text, m.start())
        if path is None:
            pos = m.start() + 1  # 不是文件，跳过一个字符继续找
            continue
        if m.start() > pos:
            segments.append(text[pos : m.start()])
        segments.append(path)
        pos = end
        found = True
    if not found:
        return None
    if pos < len(text):
        segments.append(text[pos:])
    return segments


def _longest_existing_path(text: str, start: int) -> tuple[Path | None, int]:
    """从 start 处取最长存在的文件路径，返回 (Path, 结束位置)。

    边界取最近的引号/换行/下一个盘符起点/文本末尾；路径含空格时按空格
    逐级回退，直到找到存在的文件。
    """
    bound = len(text)
    for sep in ('"', "'", "\n", "\r"):
        i = text.find(sep, start)
        if i != -1:
            bound = min(bound, i)
    m = _PATH_START.search(text, start + 3)
    if m:
        bound = min(bound, m.start())
    candidate = text[start:bound].rstrip()
    while candidate:
        p = Path(candidate).expanduser()
        if p.is_file():
            return p, start + len(candidate)
        i = candidate.rfind(" ")
        if i == -1:
            return None, start
        candidate = candidate[:i]
    return None, start


class _BurstPasteReader:
    """win32 控制台输入 reader 的包装：把一次读取中连续的纯文本键突发
    （≥_PASTE_BURST 个）合并为一个 BracketedPaste 事件。

    prompt_toolkit 的 Windows 粘贴识别有两个缺口：legacy ConsoleInputReader
    只把含换行的批量输入识别为粘贴；Vt100ConsoleInputReader 依赖终端发送
    bracketed paste 标记（\x1b[200~），conhost 不发。结果：单行文件路径
    拖入在 Windows 上永远触发不了 BracketedPaste。此处统一在 reader 输出层
    兜底。误判无害——非文件路径的文本经 ChipRegistry.register_paste 原样
    返回，与正常键入效果一致。
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):  # handle/close/flush_keys 等全部透传
        return getattr(self._inner, name)

    def read(self):
        from prompt_toolkit.key_binding.key_processor import KeyPress

        out: list[KeyPress] = []
        run: list[str] = []

        def flush() -> None:
            if len(run) >= _PASTE_BURST:
                out.append(KeyPress(Keys.BracketedPaste, "".join(run)))
            else:
                out.extend(KeyPress(ch, ch) for ch in run)
            run.clear()

        for kp in self._inner.read():
            if not isinstance(kp.key, Keys) and kp.data:
                run.append(kp.data)
            else:
                flush()
                out.append(kp)
        flush()
        return out


def create_loose_paste_input():
    """Windows 专用：构造带突发粘贴检测的 Win32Input；其它平台返回 None
    （POSIX 终端的 bracketed paste 由 prompt_toolkit 原生支持）。"""
    if sys.platform != "win32":
        return None
    try:
        from prompt_toolkit.input.win32 import Win32Input

        inp = Win32Input()
        inp.console_input_reader = _BurstPasteReader(inp.console_input_reader)
        return inp
    except Exception:
        return None


class ChipColorProcessor(Processor):
    """把 [文件:xxx] token 渲染成 chip 样式（只改样式不改文本，光标位置不受影响）。"""

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

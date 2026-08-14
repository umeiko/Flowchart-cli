"""交互式 chat REPL：与主 Agent 对话，生成并实时修改流程图。

界面基于 prompt_toolkit + rich：
- 启动横幅、Claude Code 风格的 ❯ 输入提示与底部快捷键提示栏；
- ↑/↓ 翻阅历史输入（跨会话持久化），历史命令幽灵提示与斜杠命令补全；
- Ctrl+C 取消当前输入或进行中的请求，Ctrl+D 退出；
- 拖入图片文件自动变成彩色 [图片:文件名] 芯片，Backspace 一次整块删除；
- 模型输出流式实时显示：生成 Mermaid 与最终回复都边产出边滚动，
  每轮生成只显示当前一轮的内容，服务商不支持流式时自动退回一次性显示。
"""

from __future__ import annotations

import logging
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PtStyle
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from . import __version__
from .config import Settings
from .main_agent import MainAgent
from .session import DiagramSession
from .tui_chips import ChipColorProcessor, ChipRegistry, make_key_bindings

logger = logging.getLogger(__name__)
console = Console()

_HISTORY_FILE = Path.home() / ".flowchart_agent_history"
_COMMANDS = ["/code", "/path", "/help", "/exit", "/quit"]

_PT_STYLE = PtStyle.from_dict(
    {
        "prompt": "ansicyan bold",
        "bottom-toolbar": "bg:#2b2b2b #888888",
        "auto-suggest": "#555555",
        "chip": "bg:#264f78 #ffffff bold",
    }
)

_TOOLBAR = HTML(
    " <b>Enter</b> 发送 · <b>↑/↓</b> 历史 · <b>Ctrl+C</b> 取消 · <b>Ctrl+D</b> 退出 · 拖入图片即可附图 "
)

_HELP = """\
**命令**

- `/code` — 打印当前流程图的 Mermaid 代码
- `/path` — 打印当前产物目录
- `/help` — 显示本帮助
- `/exit` — 退出（或按 Ctrl+D）

**输入**

- 直接描述需求，如：画一个登录流程，包含验证码校验失败重试
- 或给出文档路径，如：根据 test_datas/1.txt 生成流程图
- 已有图之后，继续说修改意见即可，如：把登录改成验证码登录
- **拖入图片文件**（草图/现有流程图截图）：自动变成彩色 `[图片:文件名]` 芯片，
  随消息一起发给模型（需在 .env 开启 TEXT_MODEL_VISION）；
  光标在芯片后按一次 Backspace 即可整块删除

**快捷键**

- `↑` / `↓` — 翻阅历史输入（跨会话保留）
- `Ctrl+C` — 取消当前输入 / 取消进行中的请求
- `Ctrl+D` — 退出
"""


def run_chat(settings: Settings, output_dir: Path) -> int:
    session = DiagramSession(settings, output_dir)
    display = _StreamDisplay(console)
    session.on_delta = display.show_generation  # 生成循环的 Mermaid 原文流
    session.on_round_start = display.reset_segment  # 每轮清空上一段，避免堆砌
    agent = MainAgent(
        settings, session,
        on_tool_call=_show_tool_call,
        on_delta=display.show_reply,  # 主 Agent 回复流
    )
    _print_banner(output_dir, settings)
    chips = ChipRegistry()
    prompt_session = _make_prompt_session(chips)

    while True:
        try:
            user_input = prompt_session.prompt(
                HTML("<prompt>❯ </prompt>"), bottom_toolbar=_TOOLBAR
            ).strip()
        except KeyboardInterrupt:  # Ctrl+C：清空当前行，继续
            console.print("[dim]（已取消输入）[/dim]")
            continue
        except EOFError:  # Ctrl+D：退出
            console.print("[dim]再见。[/dim]")
            return 0

        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            console.print("[dim]再见。[/dim]")
            return 0
        if user_input == "/help":
            console.print(Markdown(_HELP))
            continue
        if user_input == "/code":
            code = session.current_code
            console.print(Markdown(f"```mermaid\n{code}\n```" if code else "（还没有流程图）"))
            continue
        if user_input == "/path":
            console.print(f"产物目录：[cyan]{session.output_dir.resolve()}[/cyan]")
            continue

        # 芯片 token 还原为真实路径，图片随消息发给主 Agent
        resolved_input, images = chips.resolve(user_input)
        if images:
            console.print(f"[dim]已附带 {len(images)} 张图片："
                          + "、".join(p.name for p in images) + "[/dim]")

        try:
            with display:
                reply = agent.chat(resolved_input, images=images or None)
        except KeyboardInterrupt:  # Ctrl+C：打断进行中的请求，回到输入
            console.print("[yellow]已取消本次请求。[/yellow]")
            continue
        except Exception as e:
            logger.exception("chat 异常")
            console.print(f"[red]出错了：{e}[/red]")
            continue
        console.print(
            Panel(
                Markdown(reply),
                title="[bold green]助手[/bold green]",
                title_align="left",
                border_style="green",
                padding=(0, 1),
            )
        )


def _make_prompt_session(chips: ChipRegistry) -> PromptSession:
    return PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(_COMMANDS, sentence=True),
        style=_PT_STYLE,
        key_bindings=make_key_bindings(chips),
        input_processors=[ChipColorProcessor()],
    )


class _StreamDisplay:
    """请求进行中的实时显示区：无流式内容时是 spinner，有增量文本时滚动展示。

    主 Agent 回复与生成循环的 Mermaid 原文共用一块 Live 区域；段落切换时
    丢弃上一段（中间过程文本），transient 模式在请求结束后整段擦除，
    最终回答由调用方另出 Panel 展示。
    """

    def __init__(self, console: Console):
        self._console = console
        self._live: Live | None = None
        self._buf: list[str] = []
        self._title = ""

    def __enter__(self) -> "_StreamDisplay":
        self._buf, self._title = [], ""
        self._live = Live(
            Spinner("dots", text="[cyan]助手工作中…[/cyan]"),
            console=self._console,
            refresh_per_second=8,
            transient=True,
        )
        self._live.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def show_reply(self, delta: str) -> None:
        self._feed("[bold green]助手[/bold green]", "green", delta)

    def show_generation(self, delta: str) -> None:
        self._feed("[bold cyan]生成 Mermaid 中…[/bold cyan]", "cyan", delta)

    def reset_segment(self, _round_no: int = 0) -> None:
        """新一轮生成开始：清空上一段（上一轮的 Mermaid 原文），
        避免多轮生成文本在显示区里堆砌。"""
        self._buf = []
        if self._live is not None:
            self._live.update(Spinner("dots", text="[cyan]助手工作中…[/cyan]"))

    def _feed(self, title: str, border: str, delta: str) -> None:
        if self._live is None:
            return
        if self._buf and title != self._title:
            self._buf = []  # 段落切换：中间过程文本不保留
        self._title = title
        self._buf.append(delta)
        self._live.update(
            Panel(
                Text("".join(self._buf)),
                title=title,
                title_align="left",
                border_style=border,
                padding=(0, 1),
            )
        )


def _show_tool_call(name: str, arguments: str) -> None:
    args = arguments if len(arguments) <= 80 else arguments[:77] + "..."
    console.print(f"[dim]→ 调用 {name}({args})[/dim]")


def _print_banner(output_dir: Path, settings: Settings) -> None:
    vision = (
        "[green]已开启[/green]" if settings.text_model_vision else "[dim]未开启[/dim]"
    )
    console.print(
        Panel(
            f"[bold cyan]Flowchart AI Agent[/bold cyan]  [dim]v{__version__}[/dim]\n\n"
            "自然语言 → Mermaid 流程图  [dim]（生成 · 渲染校验 · 视觉验证循环）[/dim]\n"
            f"[dim]产物目录 {output_dir} · 主模型图像输入 {vision} · "
            "/help 查看命令 · Ctrl+D 退出[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


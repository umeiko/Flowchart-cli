"""交互式 chat REPL：与主 Agent 对话，生成并实时修改流程图。

界面基于 prompt_toolkit + rich：
- 启动横幅、Claude Code 风格的 ❯ 输入提示与底部快捷键提示栏；
- ↑/↓ 翻阅历史输入（跨会话持久化），历史命令幽灵提示与斜杠命令补全；
- Ctrl+C 取消当前输入或进行中的请求，Ctrl+D 退出；
- 拖入任意文件自动变成彩色 [文件:文件名] 芯片，Backspace 一次整块删除；
- 模型输出流式实时显示：生成 Mermaid 与最终回复都边产出边滚动，
  每轮生成只显示当前一轮的内容，服务商不支持流式时自动退回一次性显示；
- run_command 工具：红框展示 Agent 要执行的命令并请求确认（--yolo 免确认），
  执行中按 Ctrl+C 直接杀掉进程。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from itertools import zip_longest
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.styles import Style as PtStyle
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from . import __version__
from .banner_logo import LOGO_WIDTH, logo_lines
from .config import Settings
from .main_agent import MainAgent
from .session import DiagramSession
from .tui_chips import (
    ChipColorProcessor,
    ChipRegistry,
    create_loose_paste_input,
    make_key_bindings,
)

logger = logging.getLogger(__name__)
console = Console()

_HISTORY_FILE = Path.home() / ".flowchart_agent_history"
_COMMANDS = ["/code", "/engine", "/path", "/yolo", "/help", "/exit", "/quit"]

_CMD_TIMEOUT = 120  # run_command 单次执行超时（秒）
_CMD_OUTPUT_LIMIT = 4000  # 返回给模型的输出截断长度

_PT_STYLE = PtStyle.from_dict(
    {
        "prompt": "ansicyan bold",
        "bottom-toolbar": "bg:#2b2b2b #888888",
        "auto-suggest": "#555555",
        "chip": "bg:#264f78 #ffffff bold",
    }
)

_TOOLBAR = HTML(
    " <b>Enter</b> 发送 · <b>↑/↓</b> 历史 · <b>Ctrl+C</b> 取消 · <b>Ctrl+D</b> 退出 "
)

_HELP = """\
**命令**

- `/code` — 打印当前图的源码（mermaid 引擎为 Mermaid 代码，drawio 引擎为 draw.io XML）
- `/engine` — 查看或切换出图引擎：`/engine mermaid` / `/engine drawio`（drawio 需配置 DRAWIO_PATH，产物为可二次编辑的 .drawio）
- `/path` — 打印当前产物目录
- `/help` — 显示本帮助
- `/exit` — 退出（或按 Ctrl+D）

**输入**

- 直接描述需求，如：画一个登录流程，包含验证码校验失败重试
- 或给出文档路径，如：根据 test_datas/gen/1.txt 生成流程图
- 已有图之后，继续说修改意见即可，如：把登录改成验证码登录
- **切换出图引擎**：说"切换到 drawio 模式"，或直接输入 `/engine drawio`；
  drawio 模式自动按文档路由流程图/架构图两套管线，配色遵循 `styles/` 模板
- **检查文档/图片**，如：检查 test_datas/check/flowchart/2.txt 里的流程图和操作步骤是否一致（图在 2.jpg）——
  支持原理图/流程图/组网图/界面截图四类检查（产物在 `output/check/`）
- **拖入任意文件**（文档/草图/现有流程图截图等）：自动变成彩色 `[文件:文件名]` 芯片，
  提交时还原为完整路径发给模型；其中图片文件在开启 `TEXT_MODEL_VISION` 时
  还会作为图片随消息发给模型；
  光标在芯片后按一次 Backspace 即可整块删除

**快捷键**

- `↑` / `↓` — 翻阅历史输入（跨会话保留）
- `Ctrl+C` — 取消当前输入 / 取消进行中的请求 / 终止正在执行的命令
- `Ctrl+D` — 退出

**命令执行（run_command）**

- Agent 请求运行 shell 命令时会用红框展示命令，`↑`/`↓` 选择「是/否」后回车确认
- 命令在产物目录（output）下执行，产生的文件不会落到项目根目录
- `/yolo` — 切换免确认模式（命令不再询问，谨慎使用）；启动时加 `--yolo` 等效
- 命令执行中按 `Ctrl+C` 立即终止该命令
"""


def run_chat(settings: Settings, output_dir: Path, yolo: bool = False) -> int:
    # 会话级日志：整个会话的用户输入、工具调用、LLM 请求都记入 output/chat.log；
    # 每次生成/修改的详细过程另见 output/generate/v<n>/run.log
    output_dir = Path(output_dir).resolve()  # 全程使用绝对路径，展示与日志一致
    output_dir.mkdir(parents=True, exist_ok=True)
    chat_log = logging.FileHandler(output_dir / "chat.log", encoding="utf-8")
    chat_log.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger("flowchart_agent").addHandler(chat_log)
    logger.info("chat 会话开始：产物目录 %s，yolo=%s", output_dir, yolo)
    try:
        return _chat_loop(settings, output_dir, yolo=yolo)
    finally:
        logging.getLogger("flowchart_agent").removeHandler(chat_log)
        chat_log.close()


def _chat_loop(settings: Settings, output_dir: Path, yolo: bool = False) -> int:
    # 产物分目录：生成侧 output/generate/，检查侧 output/check/（由 CheckAgent 管理）
    session = DiagramSession(settings, output_dir / "generate")
    display = _StreamDisplay(console)
    display.engine = session.engine
    session.on_delta = display.show_generation  # 生成循环的源码流式显示
    session.on_round_start = display.reset_segment  # 每轮清空上一段，避免堆砌
    runner = _CommandRunner(console, display, yolo=yolo, cwd=output_dir)  # run_command 后端
    agent = MainAgent(
        settings, session,
        on_tool_call=_show_tool_call,
        on_delta=display.show_reply,  # 主 Agent 回复流
        output_root=output_dir,
        on_progress=display.set_status,  # 检查管线的路由/进度提示
        command_runner=runner,
    )
    _print_banner(output_dir, settings, yolo=yolo, session_engine=session.engine)
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
            fence = "xml" if session.engine == "drawio" else "mermaid"
            console.print(Markdown(f"```{fence}\n{code}\n```" if code else "（还没有图）"))
            continue
        if user_input == "/engine" or user_input.startswith("/engine "):
            arg = user_input[7:].strip()
            if not arg:
                console.print(
                    f"当前出图引擎：[cyan]{session.engine}[/cyan]（可选：mermaid、drawio；"
                    "切换：/engine drawio）"
                )
            else:
                console.print(session.set_output_engine(arg))
                display.engine = session.engine  # 同步流式显示的源码标签
            continue
        if user_input == "/path":
            console.print(f"产物目录：[cyan]{session.output_dir.resolve()}[/cyan]")
            continue
        if user_input == "/yolo":
            runner.yolo = not runner.yolo
            state = "[red bold]已开启（命令将不再请求确认）[/red bold]" if runner.yolo \
                else "[green]已关闭（命令执行前需确认）[/green]"
            console.print(f"yolo 模式：{state}")
            logger.info("yolo 模式切换为 %s", runner.yolo)
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
    kwargs: dict = {}
    paste_input = create_loose_paste_input()  # Windows：单行粘贴也识别为粘贴事件
    if paste_input is not None:
        kwargs["input"] = paste_input
    return PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(_COMMANDS, sentence=True),
        style=_PT_STYLE,
        key_bindings=make_key_bindings(chips),
        input_processors=[ChipColorProcessor()],
        **kwargs,
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
        self.engine = "mermaid"  # 出图引擎（/engine 切换时同步更新流式标题）

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

    def suspend(self) -> None:
        """暂停实时显示区（如 Agent 请求运行命令时让出终端）。"""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def resume(self) -> None:
        """恢复实时显示区（命令执行结束后）。"""
        if self._live is None:
            self._live = Live(
                Spinner("dots", text="[cyan]助手工作中…[/cyan]"),
                console=self._console,
                refresh_per_second=8,
                transient=True,
            )
            self._live.start()

    def show_reply(self, delta: str) -> None:
        self._feed("[bold green]助手[/bold green]", "green", delta)

    def show_generation(self, delta: str) -> None:
        label = "drawio XML" if self.engine == "drawio" else "Mermaid"
        self._feed(f"[bold cyan]生成 {label} 中…[/bold cyan]", "cyan", delta)

    def reset_segment(self, _round_no: int = 0) -> None:
        """新一轮生成开始：清空上一段（上一轮的 Mermaid 原文），
        避免多轮生成文本在显示区里堆砌。"""
        self._buf = []
        if self._live is not None:
            self._live.update(Spinner("dots", text="[cyan]助手工作中…[/cyan]"))

    def set_status(self, text: str) -> None:
        """更新进度提示文案（路由结果、检查项执行进度等），仅在还没有
        流式文本输出时替换 spinner 文案，不干扰正在滚动的内容。"""
        if self._live is not None and not self._buf:
            self._live.update(Spinner("dots", text=f"[cyan]{text}[/cyan]"))

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


class _CommandRunner:
    """run_command 工具的执行后端：红框确认 + 超时 + Ctrl+C 杀进程。

    默认每条命令执行前都以红色 Panel 展示并等待用户确认（方向键选择是/否）；
    yolo 模式下免确认。子进程放入独立进程组，执行中 Ctrl+C 的 SIGINT
    只到达父进程，由父进程捕获后 SIGKILL 整个子进程组，实现"直接杀掉"。
    """

    def __init__(self, console: Console, display: _StreamDisplay,
                 yolo: bool = False, cwd: Path | None = None):
        self._console = console
        self._display = display
        self.yolo = yolo
        # 命令的工作目录固定为产物目录：Agent 跑命令产生的文件不落在项目根目录
        self._cwd = Path(cwd).resolve() if cwd else Path.cwd()
        self._cwd.mkdir(parents=True, exist_ok=True)

    def run(self, command: str) -> str:
        command = (command or "").strip()
        if not command:
            return "错误：命令为空。"
        logger.info("run_command 请求执行：%s", command)
        self._display.suspend()
        try:
            self._console.print(
                Panel(
                    Text(f"$ {command}", style="bold white"),
                    title="[bold red]Agent 请求运行命令[/bold red]",
                    title_align="left",
                    subtitle=f"[dim]工作目录：{self._cwd}[/dim]",
                    subtitle_align="left",
                    border_style="red",
                    padding=(0, 1),
                )
            )
            if not self.yolo and not self._confirm():
                logger.info("run_command 被用户拒绝：%s", command)
                return "用户拒绝了该命令的执行，请勿重复尝试同一命令。"
            return self._exec(command)
        finally:
            self._display.resume()

    def _confirm(self) -> bool:
        """方向键选择 是/否（默认否），Enter 确认；Ctrl+C/Ctrl+D 视为拒绝。"""
        try:
            return bool(
                choice(
                    message="是否执行该命令？（↑/↓ 选择，Enter 确认）",
                    options=[(False, "否"), (True, "是")],
                    default=False,
                )
            )
        except (KeyboardInterrupt, EOFError):
            return False

    def _exec(self, command: str) -> str:
        out = ""
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # 命令输出编码不可控（Windows 工具多为 GBK，node 类工具为 UTF-8），
                # 用系统区域编码解码但容忍坏字节，避免 reader 线程崩溃丢输出
                text=True,
                errors="replace",
                cwd=str(self._cwd),
                start_new_session=(sys.platform != "win32"),
            )
        except OSError as exc:
            return f"错误：无法启动命令：{exc}"
        try:
            out, _ = proc.communicate(timeout=_CMD_TIMEOUT)
        except KeyboardInterrupt:
            self._kill_proc(proc)
            self._console.print("\n[red]命令已被 Ctrl+C 中断。[/red]")
            logger.info("run_command 被 Ctrl+C 中断：%s", command)
            return (
                "命令已被用户中断（Ctrl+C）。\n已输出内容：\n"
                + self._truncate(out or "")
            )
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
            self._console.print(f"[red]命令超时（{_CMD_TIMEOUT} 秒），已终止。[/red]")
            logger.info("run_command 超时被杀：%s", command)
            return (
                f"命令执行超过 {_CMD_TIMEOUT} 秒，已被强制终止。\n已输出内容：\n"
                + self._truncate(out or "")
            )
        out = self._truncate(out or "")
        if proc.returncode != 0:
            return f"命令退出码 {proc.returncode}：\n{out}"
        return out or "（命令执行成功，无输出）"

    @staticmethod
    def _truncate(out: str) -> str:
        if len(out) > _CMD_OUTPUT_LIMIT:
            return out[:_CMD_OUTPUT_LIMIT] + "\n…（输出过长，已截断）"
        return out

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _print_banner(
    output_dir: Path, settings: Settings, yolo: bool = False, session_engine: str = ""
) -> None:
    """启动横幅：点阵盲文 logo（banner_logo.py）+ 右侧状态栏（markup 由 rich 渲染）。

    左列 logo 全部是单宽字符（Braille Patterns），已由 logo_lines 右侧
    补齐到 LOGO_WIDTH，直接与右列拼接；中文只出现在右列，不参与左列对齐。
    """
    vision = "已开启" if settings.text_model_vision else "未开启"
    # 盲文（U+2800–U+28FF）不在 GBK 里：GBK 代码页的老终端打不出来，
    # 探测当前编码，不支持就跳过图案只留文字信息，避免启动即崩
    try:
        "⠀".encode(sys.stdout.encoding or "utf-8")
        mascot = logo_lines("bright_white")
    except (UnicodeEncodeError, LookupError):
        mascot = []
    # 右列与 logo 逐行对齐
    info = [
        "",
        f"[bold bright_white]Hi, I'm Flowchart AI Agent.[/]  [bright_black]v{__version__}[/]",
        "",
        "[bright_black]自然语言 → 流程图/架构图（生成 · 渲染校验 · 视觉验证循环）[/]",
        "",
        f"[bright_cyan]▶[/] [bright_white]出图引擎 {session_engine}（/engine 切换） · "
        f"主模型图像输入 {vision}[/]",
        "[bright_cyan]▶[/] [bright_white]/help 查看命令 · Ctrl+D 退出[/]",
        f"[bright_black]{output_dir}[/]",
    ]
    console.print()
    # 终端够宽才左右双栏；窄终端 logo 与信息上下排列，避免信息栏折行错位
    if mascot and console.width >= LOGO_WIDTH + 66:
        for left, right in zip_longest(mascot, info, fillvalue=""):
            console.print(left + ("  " + right if right else ""))
    else:
        for line in mascot:
            console.print(line)
        for line in info:
            if line:
                console.print("  " + line)
    console.print()
    if yolo:
        console.print(
            "[red bold]yolo 模式：Agent 的 shell 命令将免确认直接执行[/red bold]"
        )


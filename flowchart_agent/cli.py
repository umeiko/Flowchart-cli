"""命令行入口：

- python -m flowchart_agent run  <文档路径>  单文档批处理生成
- python -m flowchart_agent chat             交互式对话（生成 + 实时修改）
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .agent import FlowchartAgent
from .chat_cli import run_chat
from .config import load_settings
from .mermaid import render_mermaid
from .styles import get_style, load_styles


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()  # 必须在 parse_args 之前：--help 打印中文帮助时 stdout
    # 可能已被重定向（Windows 下回退 GBK/cp1252 编码），提前切 UTF-8 防止崩退出码
    parser = argparse.ArgumentParser(
        prog="flowchart-agent",
        description="根据自然语言生成 Mermaid 流程图（生成-渲染-验证循环）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单文档批处理：读文档 → 生成图 → 退出")
    p_run.add_argument("document", type=Path, help="自然语言文档路径（.txt/.md）")
    p_run.add_argument("--style", default=None, help="风格模板名（styles/ 目录中的插件，如 dark）")
    _add_common_args(p_run)

    p_chat = sub.add_parser("chat", help="交互式对话：口述需求或给文档路径，可持续修改")
    p_chat.add_argument(
        "--yolo",
        action="store_true",
        help="免确认执行 Agent 的 shell 命令（谨慎）",
    )
    _add_common_args(p_chat)

    args = parser.parse_args(argv)
    _setup_logging(args.command, args.verbose)

    try:
        settings = load_settings(args.env)
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    if args.command == "chat":
        return run_chat(settings, args.output, yolo=args.yolo)
    # run 模式属生成大类：产物落 <output>/generate，与检查侧的 <output>/check 对应
    return _run_once(settings, args.document, args.output / "generate", args.style)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--env", type=Path, default=None, help=".env 文件路径，默认 ./.env")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")


def _fix_console_encoding() -> None:
    """Best-effort 把控制台输出切到 UTF-8：Windows 老终端默认 GBK 代码页，
    会导致界面边框字符和中文乱码。失败时静默忽略（如输出被重定向）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _setup_logging(command: str, verbose: bool) -> None:
    """包日志走独立 logger：不 propagate 到 root，避免 chat 界面被步骤日志污染；
    级别固定 DEBUG，保证 output/v<n>/run.log 始终记录完整步骤。
    run 模式（或 -v）额外挂控制台 handler 显示步骤。root 只保留 WARNING 压制第三方噪音。"""
    pkg = logging.getLogger("flowchart_agent")
    pkg.setLevel(logging.DEBUG)
    pkg.propagate = False
    if command == "run" or verbose:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        pkg.addHandler(console)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING,
                        format="%(message)s")


def _run_once(settings, document: Path, output: Path, style_name: str | None = None) -> int:
    output = output.resolve()  # 展示与日志统一用绝对路径
    if not document.is_file():
        print(f"错误：文档不存在：{document}", file=sys.stderr)
        return 2
    style = None
    if style_name:
        try:
            style = get_style(style_name)
        except ValueError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2
    else:
        # 未指定时注入 default 插件：用户编辑 styles/default.md 即可定制默认风格
        style = load_styles().get("default")
    doc_text = document.read_text(encoding="utf-8")

    try:
        result = FlowchartAgent(settings).run(
            doc_text, output, style=style, verify_mode=settings.verify_mode,
            action=f"run 模式（文档 {document}）",
        )
    except Exception as e:
        print(f"运行失败：{e}", file=sys.stderr)
        return 1

    if result.success:
        final_mmd = output / "final.mmd"
        final_mmd.write_text(result.mermaid_code, encoding="utf-8")
        if result.image_path:
            shutil.copy(result.image_path, output / f"final{result.image_path.suffix}")
        # 与 agent 内相同的背景解析：风格插件 > RENDER_BACKGROUND
        bg = (style.background if style else None) or settings.render_background
        svg = render_mermaid(
            result.mermaid_code, output, stem="final", fmt="svg", background=bg,
            chrome_path=settings.chrome_path,
        )
        print(f"成功（{len(result.rounds)} 轮）：{final_mmd}")
        if svg.ok:
            print(f"SVG：{svg.image_path}")
        return 0

    print(f"失败：{len(result.rounds)} 轮后仍未通过验证。", file=sys.stderr)
    if result.final_feedback:
        print(f"最后的验证意见：\n{result.final_feedback}", file=sys.stderr)
    if result.image_path:
        print(f"最后一轮产物见：{result.image_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

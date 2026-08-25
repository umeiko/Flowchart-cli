"""draw.io 本地渲染：调用 draw.io 桌面版 CLI 把 .drawio 导出为 PNG/SVG。

draw.io 桌面版自带命令行导出（draw.io -x -f png），渲染结果与用户在
draw.io 里看到的完全一致，且纯本地、无网络依赖。路径由 .env 的
DRAWIO_PATH 配置（config.Settings.drawio_path）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class DrawioNotFoundError(RuntimeError):
    """未配置或找不到 draw.io 桌面版。"""


def check_drawio_available(drawio_path: str | None) -> str | None:
    """drawio 引擎可用性检查：返回 None 表示可用，否则返回给用户看的指引文案。"""
    if not drawio_path:
        return (
            "drawio 模式需要本机安装 draw.io 桌面版：请先到 drawio.com "
            "（或 GitHub jgraph/drawio-desktop 的 Release 页）下载安装，"
            "然后在 .env 中设置 DRAWIO_PATH 指向 draw.io 可执行文件"
            "（Windows 示例：DRAWIO_PATH=C:\\Program Files\\draw.io\\draw.io.exe）。"
        )
    if not Path(drawio_path).is_file():
        return (
            f".env 中 DRAWIO_PATH 指向的文件不存在：{drawio_path}。"
            "请修正为 draw.io 桌面版的实际安装路径。"
        )
    return None


def render_drawio(
    drawio_file: str | Path,
    output: str | Path,
    drawio_path: str | None,
    fmt: str = "png",
    scale: int = 2,
) -> Path | None:
    """用 draw.io CLI 把 .drawio 导出为图片，返回产物路径；失败返回 None。

    fmt：png / svg / pdf（drawio -f 接受的值）。scale 仅对 PNG 有效（-s）。
    """
    if not drawio_path:
        raise DrawioNotFoundError(
            "未配置 DRAWIO_PATH（draw.io 桌面版路径），请在 .env 中设置，"
            "如 DRAWIO_PATH=C:\\Program Files\\draw.io\\draw.io.exe"
        )
    exe = Path(drawio_path)
    if not exe.is_file():
        raise DrawioNotFoundError(f"draw.io 可执行文件不存在：{drawio_path}")

    drawio_file = Path(drawio_file)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(exe),
        "--export",
        "--format", fmt,
        "--output", str(output),
    ]
    if fmt == "png":
        cmd += ["--scale", str(scale)]
    cmd.append(str(drawio_file))

    logger.info("[drawio] 渲染 %s -> %s", drawio_file, output)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        logger.error("[drawio] 渲染超时（180s）：%s", drawio_file)
        return None
    if proc.returncode != 0 or not output.is_file():
        logger.error(
            "[drawio] 渲染失败（exit=%s）：%s%s",
            proc.returncode, proc.stdout.strip(), proc.stderr.strip(),
        )
        return None
    return output

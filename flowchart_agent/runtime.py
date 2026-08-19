"""运行时环境感知：PyInstaller 冻结检测与外部工具（node / mmdc / Chrome）解析。

非冻结（源码运行）：行为不变——PATH 上的 node / mmdc，项目根的 scripts/。
冻结（离线包）：资源相对 exe 所在目录（app_dir）：

    vendor/node/         内置 node 独立二进制（node / node.exe）
    vendor/mermaid-cli/  npm 安装的 @mermaid-js/mermaid-cli（含 node_modules，
                         PUPPETEER_SKIP_DOWNLOAD 装出，无自带 Chromium）
    vendor/parse/        mermaid_parse.mjs + node_modules（mermaid、jsdom）

环境变量优先：MMDC_PATH（mmdc 可执行文件或 cli.js 路径）、FLOWCHART_NODE。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """应用根目录：冻结时为 exe 所在目录，否则为项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _vendor_dir() -> Path:
    return app_dir() / "vendor"


def find_node() -> str | None:
    """node 可执行文件：FLOWCHART_NODE 覆盖 > vendor 内置 > PATH。"""
    env = os.getenv("FLOWCHART_NODE")
    if env and Path(env).is_file():
        return env
    exe = "node.exe" if sys.platform == "win32" else "node"
    bundled = _vendor_dir() / "node" / exe
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node")


def _wrap_win(cmd: list[str]) -> list[str]:
    """Windows 上 mmdc 是 .cmd 批处理，CreateProcess 无法直接执行，须经 cmd /c。"""
    if sys.platform == "win32" and not cmd[0].lower().endswith((".exe", ".js")):
        return ["cmd", "/c", *cmd]
    return cmd


def mmdc_command(args: list[str]) -> list[str] | None:
    """构造 mmdc 调用命令（args 为 -i/-o 等参数）。找不到任何 mmdc 时返回 None。

    解析顺序：MMDC_PATH 覆盖（可以是 mmdc 可执行文件或 cli.js）>
    vendor/mermaid-cli（node + cli.js）> PATH 上的 mmdc。
    """
    env = os.getenv("MMDC_PATH")
    if env:
        p = Path(env)
        if p.suffix == ".js":  # 直接给了 cli.js：用 node 调起
            node = find_node()
            return [node, str(p), *args] if node else None
        return _wrap_win([env, *args])
    vendor_cli = (
        _vendor_dir() / "mermaid-cli" / "node_modules"
        / "@mermaid-js" / "mermaid-cli" / "src" / "cli.js"
    )
    if vendor_cli.is_file():
        node = find_node()
        if node:
            return [node, str(vendor_cli), *args]
    mmdc = shutil.which("mmdc")
    if mmdc:
        return _wrap_win([mmdc, *args])
    return None


def parse_script() -> Path | None:
    """mermaid.parse 预检脚本：需在脚本或其上级目录找到 node_modules/mermaid
    （ESM 裸导入按目录向上解析：vendor/parse/node_modules 或项目根 node_modules）。
    都没有返回 None，调用方维持"跳过预检交给 mmdc 兜底"的现状。
    """
    for candidate in (
        _vendor_dir() / "parse" / "mermaid_parse.mjs",
        app_dir() / "scripts" / "mermaid_parse.mjs",
    ):
        if not candidate.is_file():
            continue
        for base in (candidate.parent, *candidate.parents):
            if (base / "node_modules" / "mermaid").is_dir():
                return candidate
    return None


# Windows 常见 Chrome / Edge 安装路径（Program Files 环境变量展开）
_WIN_BROWSER_CANDIDATES = (
    r"{PF}\Google\Chrome\Application\chrome.exe",
    r"{PF86}\Google\Chrome\Application\chrome.exe",
    r"{LOCAL}\Google\Chrome\Application\chrome.exe",
    r"{PF}\Microsoft\Edge\Application\msedge.exe",
    r"{PF86}\Microsoft\Edge\Application\msedge.exe",
)

_MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def sniff_browser() -> str | None:
    """嗅探系统浏览器（Chrome/Edge）安装路径，与是否冻结无关。

    Windows 查常见安装目录，macOS 查 /Applications，最后兜底 PATH。
    """
    if sys.platform == "win32":
        mapping = {
            "PF": os.getenv("ProgramFiles", r"C:\Program Files"),
            "PF86": os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "LOCAL": os.getenv("LOCALAPPDATA", ""),
        }
        for tpl in _WIN_BROWSER_CANDIDATES:
            p = Path(tpl.format(**mapping))
            if p.is_file():
                return str(p)
    elif sys.platform == "darwin" and _MAC_CHROME.is_file():
        return str(_MAC_CHROME)
    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_chrome() -> str | None:
    """冻结且 CHROME_PATH 未配置时的浏览器探测（离线包不含 Chromium，必须用系统浏览器）。

    非冻结模式返回 None（维持 puppeteer 自带 Chromium 的现状行为）。
    """
    if not is_frozen():
        return None
    found = sniff_browser()
    if found:
        logger.info("自动探测到浏览器：%s", found)
    return found

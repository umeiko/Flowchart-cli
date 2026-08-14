"""mermaid-cli (mmdc) 渲染封装：渲染即校验，失败时返回 stderr 供修复 loop 使用。

渲染前先用 mermaid.parse（scripts/mermaid_parse.mjs，Node + jsdom，不启动
Chromium）做快速语法预检，语法错误在 1 秒内返回，不必等 mmdc 完整渲染。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# scripts/mermaid_parse.mjs 位于项目根目录（本文件上三级）
_PARSE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mermaid_parse.mjs"

# 配置 CHROME_PATH 时在输出目录生成的 puppeteer 配置文件名
_PUPPETEER_CONFIG_NAME = ".puppeteer-config.json"


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    mmd_path: Path
    image_path: Path | None = None
    error: str = ""


class MermaidCliNotFoundError(RuntimeError):
    pass


def render_mermaid(
    code: str,
    output_dir: str | Path,
    stem: str,
    fmt: str = "png",
    background: str = "white",
    chrome_path: str | None = None,
) -> RenderResult:
    """把 Mermaid 代码写入 <output_dir>/<stem>.mmd，预检后用 mmdc 渲染为 <stem>.<fmt>。

    background 为画布背景色（white、#1e1e1e 等 mmdc -b 接受的值）。
    默认白色而非透明：透明背景在查看器/验证模型眼中表现不一致，
    会导致"用户描述背景色但图永远对不上"的验证死循环。
    chrome_path：指定 Chrome 可执行文件（公司 Windows 上 puppeteer 自带
    Chromium 常不可用），设置后自动生成 puppeteer-config.json 并传 -p。
    """
    if shutil.which("mmdc") is None:
        raise MermaidCliNotFoundError(
            "未找到 mmdc 命令，请先安装：npm install -g @mermaid-js/mermaid-cli"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mmd_path = output_dir / f"{stem}.mmd"
    image_path = output_dir / f"{stem}.{fmt}"
    mmd_path.write_text(code, encoding="utf-8")

    # 快速语法预检：失败直接返回，省掉一次 Chromium 渲染
    parse_error = _quick_parse_check(mmd_path)
    if parse_error is not None:
        return RenderResult(ok=False, mmd_path=mmd_path, error=f"[语法预检] {parse_error}")

    proc = subprocess.run(
        _mmdc_command(mmd_path, image_path, background, chrome_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0 or not image_path.exists():
        error = (proc.stderr or proc.stdout or "mmdc 渲染失败且无输出").strip()
        return RenderResult(ok=False, mmd_path=mmd_path, error=error)
    return RenderResult(ok=True, mmd_path=mmd_path, image_path=image_path)


def _write_puppeteer_config(output_dir: Path, chrome_path: str) -> Path:
    """在输出目录生成 puppeteer 配置：让 mmdc 用指定 Chrome 而非自带 Chromium。

    公司 Windows 环境常见：puppeteer 下载的 Chromium 被拦截/缺依赖，
    只有本机 chrome.exe 可用，必须 mmdc -p 指定 executablePath。
    """
    config_path = output_dir / _PUPPETEER_CONFIG_NAME
    config_path.write_text(
        json.dumps({"executablePath": chrome_path}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config_path


def _mmdc_command(
    mmd_path: Path,
    image_path: Path,
    background: str,
    chrome_path: str | None = None,
) -> list[str]:
    """构造 mmdc 调用命令。Windows 上 mmdc 是 .cmd 批处理，CreateProcess 无法
    直接执行，必须经 cmd /c 调用。"""
    args = ["mmdc", "-i", str(mmd_path), "-o", str(image_path), "-b", background]
    if chrome_path:
        config = _write_puppeteer_config(mmd_path.parent, chrome_path)
        args += ["-p", str(config)]
    if sys.platform == "win32":
        return ["cmd", "/c", *args]
    return args


def _quick_parse_check(mmd_path: Path) -> str | None:
    """mermaid.parse 预检。返回 None 表示通过（或预检不可用，交给 mmdc 兜底）。"""
    if shutil.which("node") is None or not _PARSE_SCRIPT.is_file():
        return None
    try:
        proc = subprocess.run(
            ["node", str(_PARSE_SCRIPT), str(mmd_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None  # 预检自身故障不应阻塞主流程
    if proc.returncode == 1:
        return proc.stderr.strip() or "Mermaid 语法错误"
    return None  # 0=通过；2=预检故障，交给 mmdc

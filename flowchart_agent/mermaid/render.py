"""mermaid-cli (mmdc) 渲染封装：渲染即校验，失败时返回 stderr 供修复 loop 使用。

渲染前先用 mermaid.parse（scripts/mermaid_parse.mjs，Node + jsdom，不启动
Chromium）做快速语法预检，语法错误在 1 秒内返回，不必等 mmdc 完整渲染。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import runtime

logger = logging.getLogger(__name__)

# 配置 CHROME_PATH 时（或离线包自动探测到浏览器时）在输出目录生成的 puppeteer 配置文件名
_PUPPETEER_CONFIG_NAME = ".puppeteer-config.json"

# width="auto" 时自然宽度的上限：超过则压缩到该值（防止极端宽图撑爆截图纹理上限）
_AUTO_MAX_WIDTH = 4096


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    mmd_path: Path
    image_path: Path | None = None
    error: str = ""
    # width="auto" 的 PNG 渲染会先出一份 SVG 探测自然宽度，该 SVG 直接留作产物
    svg_path: Path | None = None


class MermaidCliNotFoundError(RuntimeError):
    pass


def render_mermaid(
    code: str,
    output_dir: str | Path,
    stem: str,
    fmt: str = "png",
    background: str = "white",
    chrome_path: str | None = None,
    scale: str | None = None,
    width: str | None = None,
) -> RenderResult:
    """把 Mermaid 代码写入 <output_dir>/<stem>.mmd，预检后用 mmdc 渲染为 <stem>.<fmt>。

    background 为画布背景色（white、#1e1e1e 等 mmdc -b 接受的值）。
    默认白色而非透明：透明背景在查看器/验证模型眼中表现不一致，
    会导致"用户描述背景色但图永远对不上"的验证死循环。
    chrome_path：指定 Chrome 可执行文件（公司 Windows 上 puppeteer 自带
    Chromium 常不可用），设置后自动生成 puppeteer-config.json 并传 -p。
    scale：PNG 缩放倍数（mmdc -s），大图表分辨率低、文字看不清时提高；
    仅对 PNG 生效（SVG 是矢量图无需缩放）。
    width：PNG 视口宽度（mmdc -w），仅对 PNG 生效：
    - "auto"（推荐）：先用默认视口渲一份 SVG 探测图的自然宽度，再按
      min(自然宽度, 4096) 渲染 PNG——自适应视口的图型（甘特图等）按自然
      比例渲染不被拉宽，超宽流程图不被 mmdc 默认 800 视口压扁，探测出的
      SVG 同时作为产物保留；
    - 数字字符串：固定视口宽度（旧行为）；
    - None：mmdc 默认视口（800）。
    """
    if runtime.mmdc_command(["--version"]) is None:
        raise MermaidCliNotFoundError(
            "未找到 mmdc（Mermaid 渲染器）。源码运行请先安装："
            "npm install -g @mermaid-js/mermaid-cli；离线包请确认 exe 旁边"
            "存在 vendor/mermaid-cli 目录，或设置 MMDC_PATH 环境变量。"
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

    svg_path = None
    resolved_width = width
    if fmt == "png" and width == "auto":
        # 探测：默认视口渲 SVG，从 SVG 读出图的自然宽度
        svg_path = output_dir / f"{stem}.svg"
        probe_error = _run_mmdc(mmd_path, svg_path, background, chrome_path, fmt="svg")
        natural = _svg_natural_width(svg_path) if probe_error is None else None
        if natural is None:
            resolved_width = None  # 探测失败退回 mmdc 默认视口
            svg_path = None
            logger.warning("自然宽度探测失败，按 mmdc 默认视口渲染：%s", probe_error)
        else:
            resolved_width = str(min(natural, _AUTO_MAX_WIDTH))
            logger.info("自然宽度 %dpx，PNG 视口取 %s", natural, resolved_width)

    error = _run_mmdc(
        mmd_path, image_path, background, chrome_path,
        scale=scale, fmt=fmt, width=resolved_width,
    )
    if error is not None:
        return RenderResult(ok=False, mmd_path=mmd_path, error=error)
    return RenderResult(ok=True, mmd_path=mmd_path, image_path=image_path, svg_path=svg_path)


def _run_mmdc(
    mmd_path: Path,
    image_path: Path,
    background: str,
    chrome_path: str | None,
    scale: str | None = None,
    fmt: str = "png",
    width: str | None = None,
) -> str | None:
    """执行一次 mmdc 渲染。返回 None 表示成功，否则返回错误文本。"""
    cmd = _mmdc_command(mmd_path, image_path, background, chrome_path, scale, fmt, width)
    logger.debug("[render] 执行命令：%s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        # node/mmdc 的输出是 UTF-8；Windows 中文环境的默认 GBK 解码遇到
        # UTF-8 字节会炸 reader 线程，stderr 变 None（曾导致 AttributeError）
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0 or not image_path.exists():
        return (proc.stderr or proc.stdout or "mmdc 渲染失败且无输出").strip()
    return None


def _svg_natural_width(svg_path: Path) -> int | None:
    """从 SVG 头部解析图的自然宽度（max-width 样式或 viewBox 第三分量）。"""
    try:
        head = svg_path.read_text(encoding="utf-8", errors="ignore")[:2000]
    except OSError:
        return None
    m = re.search(r"max-width:\s*([\d.]+)px", head)
    if m:
        return max(1, round(float(m.group(1))))
    m = re.search(r'viewBox="([^"]+)"', head)
    if m:
        parts = m.group(1).split()
        if len(parts) == 4:
            return max(1, round(float(parts[2])))
    return None


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
    scale: str | None = None,
    fmt: str = "png",
    width: str | None = None,
) -> list[str]:
    """构造 mmdc 调用命令（经 runtime 解析 vendor/PATH）。scale（-s）与 width（-w）
    只对 PNG 传，SVG 是矢量图，自然宽度不受视口限制。"""
    args = ["-i", str(mmd_path), "-o", str(image_path), "-b", background]
    # 离线包（冻结）未配 CHROME_PATH 时自动探测系统 Chrome/Edge：
    # vendor 的 mermaid-cli 不带自带 Chromium，必须指定 executablePath
    chrome_path = chrome_path or runtime.detect_chrome()
    if chrome_path:
        config = _write_puppeteer_config(mmd_path.parent, chrome_path)
        args += ["-p", str(config)]
    if fmt == "png":
        if scale:
            args += ["-s", str(scale)]
        if width and width != "auto":
            args += ["-w", str(width)]
    cmd = runtime.mmdc_command(args)
    if cmd is None:  # 调用方已检查，理论不可达
        raise MermaidCliNotFoundError("未找到 mmdc（Mermaid 渲染器）。")
    return cmd


def _quick_parse_check(mmd_path: Path) -> str | None:
    """mermaid.parse 预检。返回 None 表示通过（或预检不可用，交给 mmdc 兜底）。"""
    node = runtime.find_node()
    script = runtime.parse_script()
    if node is None or script is None:
        return None
    try:
        proc = subprocess.run(
            [node, str(script), str(mmd_path)],
            capture_output=True,
            # node 输出为 UTF-8；显式指定编码避免 Windows GBK 区域设置下
            # 解码失败导致 stderr 为 None（reader 线程崩溃）
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None  # 预检自身故障不应阻塞主流程
    if proc.returncode == 1:
        return (proc.stderr or "").strip() or "Mermaid 语法错误"
    return None  # 0=通过；2=预检故障，交给 mmdc

"""launcher.exe 的配置向导（仅 Windows 离线包使用，独立于 flowchart_agent 主包）。

只用标准库，可独立冻结成极小的 launcher.exe。逻辑：
- exe 旁存在合法 .env（主模型 + 视觉模型共六项齐全）：直接启动同目录的
  flowchart-agent.exe 进入 chat；
- 否则进入引导：第 1 步主模型（base_url、api_key、模型名），第 2 步视觉模型
  （必填，默认复用主模型的地址与 Key）；Chrome/Edge 地址自动嗅探，嗅探到就
  不再询问；写盘 exe 旁的 .env 后启动 chat；
- 末尾提示：更多配置（检视强度、渲染参数等）自行编辑 .env，参考 .env.example。

构建：pyinstaller packaging/launcher.spec（CI 仅在 win-x64 任务中执行）。
源码调试：python packaging/onboard.py（此时以当前目录为工作目录）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

_ENV_TEMPLATE = """\
# Flowchart Agent 配置（由 launcher 向导生成）
# 更多可选项（检视强度 VERIFY_MODE、渲染参数 RENDER_SCALE / RENDER_WIDTH 等）
# 请参照 .env.example 自行添加。

# 主模型（文本生成）
TEXT_MODEL_NAME={model}
TEXT_MODEL_API_KEY={api_key}
TEXT_MODEL_BASE_URL={base_url}

# 视觉模型（多模态：看图生成、图像检视、OCR）
VISION_MODEL_NAME={v_model}
VISION_MODEL_API_KEY={v_api_key}
VISION_MODEL_BASE_URL={v_base_url}

# 渲染用浏览器（自动嗅探；改其它浏览器请修改此路径）
CHROME_PATH={chrome}
"""

# Windows 常见 Chrome / Edge 安装路径（与主程序 runtime.py 的嗅探规则保持一致）
_WIN_BROWSER_CANDIDATES = (
    r"{PF}\Google\Chrome\Application\chrome.exe",
    r"{PF86}\Google\Chrome\Application\chrome.exe",
    r"{LOCAL}\Google\Chrome\Application\chrome.exe",
    r"{PF}\Microsoft\Edge\Application\msedge.exe",
    r"{PF86}\Microsoft\Edge\Application\msedge.exe",
)
_MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _sniff_browser() -> str | None:
    """嗅探系统浏览器（Chrome/Edge）安装路径。"""
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


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _fix_console_encoding() -> None:
    """Windows 老终端默认 GBK/cp1252，打印中文前先切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _ask(prompt: str, default: str = "") -> str:
    """带默认值的输入提示；Ctrl+C/Ctrl+D 抛出让上层退出。"""
    suffix = f"（默认 {default}）" if default else ""
    value = input(f"{prompt}{suffix}：").strip()
    return value or default


def _config_valid(env_path: Path) -> bool:
    """主模型 + 视觉模型共六项齐全才算合法配置（不联网验证，连接性在引导流程里检测）。"""
    if not env_path.is_file():
        return False
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return all(values.get(k) for k in (
        "TEXT_MODEL_NAME", "TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL",
        "VISION_MODEL_NAME", "VISION_MODEL_API_KEY", "VISION_MODEL_BASE_URL",
    ))


def _check_connectivity(base_url: str, api_key: str) -> bool:
    """用 /models 做一次轻量连通性检测（10 秒超时），失败不阻断。"""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _run_wizard(env_path: Path) -> None:
    print("=" * 52)
    print("  Flowchart Agent 首次配置向导")
    print("  第 1 步（必选）：主模型 —— 负责理解和画图的语言模型")
    print("=" * 52)
    print()
    base_url = _ask("主模型 API 地址（BASE_URL，如 https://api.openai.com/v1）")
    api_key = _ask("主模型 API Key（输入时会显示，注意周围没人再看）")
    model = _ask("主模型名称（如 gpt-4o-mini、deepseek-v3）")

    # 第 2 步：视觉模型必填；默认复用主模型的地址与 Key，回车即可
    print()
    print("-" * 52)
    print("  第 2 步（必填）：视觉模型 —— 负责看图（图像检视/看图生成/OCR）")
    print("  与主模型同一家服务时直接回车用默认值即可")
    print("-" * 52)
    v_base_url = _ask("视觉模型 API 地址", default=base_url)
    v_api_key = _ask("视觉模型 API Key", default=api_key)
    v_model = _ask("视觉模型名称（多模态，如 qwen-vl-max、gpt-4o）")

    # Chrome/Edge 自动嗅探：嗅到就不问
    chrome = _sniff_browser()
    if chrome:
        print(f"\n已自动找到浏览器：{chrome}")
    else:
        print("\n没有自动找到 Chrome/Edge，画图渲染需要它。")
        chrome = _ask("请粘贴 chrome.exe 的完整路径（留空则稍后在 .env 里配 CHROME_PATH）")

    # 连通性检测（可选跳过）
    print("\n正在测试主模型 API 连通性（10 秒内）…")
    if _check_connectivity(base_url, api_key):
        print("连接成功！")
    else:
        print("连接失败（可能是地址或 Key 有误，也可能只是当前网络不通）。")
        if _ask("仍然保存配置？[y/N]").lower() not in ("y", "yes"):
            print("已取消，未写入任何配置。")
            sys.exit(1)

    env_path.write_text(
        _ENV_TEMPLATE.format(
            model=model, api_key=api_key, base_url=base_url,
            v_model=v_model, v_api_key=v_api_key, v_base_url=v_base_url,
            chrome=chrome,
        ),
        encoding="utf-8",
    )
    print(f"\n配置已写入：{env_path}")
    print("更多配置（检视强度、渲染参数等）请用记事本编辑该文件，")
    print("可参照同目录的 .env.example。")
    print("提示：若主模型本身支持看图，在 .env 加 TEXT_MODEL_VISION=true 即可让它直接读图。")


def main() -> int:
    _fix_console_encoding()
    exe_dir = _exe_dir()
    env_path = exe_dir / ".env"
    main_exe = exe_dir / (
        "flowchart-agent.exe" if sys.platform == "win32" else "flowchart-agent"
    )

    try:
        if not _config_valid(env_path):
            _run_wizard(env_path)
        else:
            print(f"检测到有效配置（{env_path}），直接进入对话模式。\n")

        if not main_exe.is_file():
            print(f"错误：找不到主程序 {main_exe.name}，请确认它与 launcher 在同一目录。")
            input("按回车退出…")
            return 1
        print("启动中…（退出对话可用 Ctrl+D 或输入 /exit）\n")
        proc = subprocess.run([str(main_exe), "chat"], cwd=exe_dir)
        if proc.returncode != 0:
            print(f"\n程序异常退出（退出码 {proc.returncode}）。")
            input("按回车关闭窗口…")
        return proc.returncode
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        return 130
    except Exception as e:  # 向导兜底：双击场景下让用户能看见错误
        print(f"\n出错了：{e}")
        input("按回车关闭窗口…")
        return 1


if __name__ == "__main__":
    sys.exit(main())

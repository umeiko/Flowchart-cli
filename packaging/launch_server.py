"""Windows offline-bundle launcher for the Web/API service.

This wrapper intentionally uses only the standard library so PyInstaller can
produce a small ``launch_server.exe``. Service arguments are read by the main
``flowchart-agent.exe`` from the adjacent ``.env`` file.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _open_when_ready(url: str) -> None:
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if 200 <= response.status < 300:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


def _fix_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"-h", "--help"}:
        print("launch_server.exe：按同目录 .env 配置启动 Flowchart Agent Web/API 服务。")
        print("配置项：SERVER_HOST、SERVER_PORT、SERVER_OUTPUT、SERVER_DATA_DIR、SERVER_OPEN_BROWSER")
        return 0

    exe_dir = _exe_dir()
    env_path = exe_dir / ".env"
    main_exe = exe_dir / ("flowchart-agent.exe" if sys.platform == "win32" else "flowchart-agent")
    if not env_path.is_file():
        print("错误：同目录没有 .env。请先运行 launcher.exe 完成配置，或复制 .env.example。")
        return 2
    if not main_exe.is_file():
        print(f"错误：找不到主程序 {main_exe.name}，请确认两个 exe 位于同一目录。")
        return 2

    env = _read_env(env_path)
    host = env.get("SERVER_HOST", "127.0.0.1") or "127.0.0.1"
    port = env.get("SERVER_PORT", "8765") or "8765"
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{port}"
    open_browser = env.get("SERVER_OPEN_BROWSER", "true").lower() in {"1", "true", "yes", "on"}
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()

    print(f"正在启动 Flowchart Agent Server：{url}")
    print("关闭本窗口或按 Ctrl+C 可停止服务。\n")
    try:
        return subprocess.run([str(main_exe), "server"], cwd=exe_dir).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

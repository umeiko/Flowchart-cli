"""离线发布包组装：exe + vendor(node、mermaid-cli、语法预检) + 资源目录 → zip/tar.gz。

CI（release.yml）与本地通用。需要在能访问 nodejs.org / npm registry 的机器上运行；
产出的包在目标机器上完全离线可用（除 LLM API 与系统 Chrome 外无外部依赖）。

用法：
    python packaging/make_bundle.py --platform macos-arm64 --exe dist/bin/flowchart-agent
    python packaging/make_bundle.py --platform win-x64 --exe dist/bin/flowchart-agent.exe
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _version() -> str:
    """从 pyproject.toml 读项目版本号，用于产物文件名（-vX.Y.Z 后缀）。"""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]

NODE_VERSION = "22.14.0"  # Node LTS，与 mermaid-cli engines（^18.19 || >=20）兼容
MERMAID_CLI_VERSION = "11.16.0"

# 平台 → （node 发行包后缀, 包内 node 相对路径, exe 文件名, 归档格式）
PLATFORMS = {
    "win-x64": ("win-x64.zip", "node.exe", "flowchart-agent.exe", "zip"),
    "macos-arm64": ("darwin-arm64.tar.gz", "bin/node", "flowchart-agent", "tar.gz"),
    "macos-x64": ("darwin-x64.tar.gz", "bin/node", "flowchart-agent", "tar.gz"),
    # 需要 Linux 包时在 release.yml 的 matrix 里加一行即可：
    # "linux-x64": ("linux-x64.tar.gz", "bin/node", "flowchart-agent", "tar.gz"),
}

QUICKSTART = """\
Flowchart Agent 离线包 —— 3 步上手
==================================

Windows 用户最简单的方式：双击 launcher.exe，按提示填 3 项模型配置
（浏览器地址会自动嗅探，一般不用填），完成后自动进入对话模式。
以后每次用也直接双击 launcher.exe 即可。

手动方式（macOS / Linux / 想用命令行的 Windows 用户）：

1. 复制 .env.example 为 .env，填入模型 API 配置（TEXT_MODEL_* 必填，
   VISION_MODEL_* 建议填，没有视觉模型也能用，检视会自动降级）。
2. Windows：确保装过 Chrome 或 Edge（渲染用，程序会自动探测；
   探测不到时在 .env 里设置 CHROME_PATH 指向 chrome.exe）。
   macOS：首次运行如被 Gatekeeper 拦截，先执行
   xattr -d com.apple.quarantine flowchart-agent
3. 运行：
   flowchart-agent chat            交互模式（推荐）
   flowchart-agent run 需求.txt    批处理模式

产物默认在 ./output 下。styles/ 与 skills/ 目录里的 markdown 是
风格模板与技能包，可自行增改。完整文档见项目仓库 docs/。
"""


def log(msg: str) -> None:
    print(f"[bundle] {msg}", flush=True)


def _fix_stdout_encoding() -> None:
    """Windows（cp1252/GBK 控制台或重定向）下打印中文会 UnicodeEncodeError，
    best-effort 切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def run(cmd: list[str], **kw) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def fetch_node(platform: str, dest: Path) -> None:
    """下载 node 独立二进制，只把 node 可执行文件放进 dest（vendor/node/）。"""
    suffix, node_rel, _, _ = PLATFORMS[platform]
    url = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-{suffix}"
    log(f"下载 {url}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "node.pkg"
        urllib.request.urlretrieve(url, archive)
        extract = Path(td) / "x"
        if suffix.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract)
        else:
            with tarfile.open(archive) as tf:
                tf.extractall(extract)
        node_bin = extract / f"node-v{NODE_VERSION}-{suffix.removesuffix('.zip').removesuffix('.tar.gz')}" / node_rel
        if not node_bin.is_file():
            raise RuntimeError(f"node 发行包结构异常，未找到 {node_bin}")
        out = dest / node_bin.name
        shutil.copy(node_bin, out)
        out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log(f"node -> {dest}")


def npm_install(workdir: Path, deps: dict[str, str]) -> None:
    """在 workdir 写 package.json 并 npm install（跳过 puppeteer 的 Chromium 下载）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "package.json").write_text(
        json.dumps({"private": True, "dependencies": deps}, indent=2),
        encoding="utf-8",
    )
    env = dict(os.environ)
    # 离线包运行时一律走系统 Chrome（CHROME_PATH / 自动探测），不打 Chromium
    env["PUPPETEER_SKIP_DOWNLOAD"] = "true"
    env["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = "true"
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=workdir, env=env)


def build(platform: str, exe: Path, outdir: Path) -> Path:
    suffix, _, exe_name, archive_fmt = PLATFORMS[platform]
    if not exe.is_file():
        raise SystemExit(f"找不到已构建的可执行文件：{exe}（先跑 pyinstaller）")

    bundle = outdir / f"flowchart-agent-{platform}"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    # 1) 主程序
    target_exe = bundle / exe_name
    shutil.copy(exe, target_exe)
    target_exe.chmod(target_exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 1.5) Windows 包附带 launcher.exe（首次配置向导，面向无命令行经验的用户；
    # 仅当 CI 的 Windows 任务用 launcher.spec 构建出它时才存在）
    launcher = exe.parent / "launcher.exe"
    if platform == "win-x64" and launcher.is_file():
        shutil.copy(launcher, bundle / "launcher.exe")
        log("附带 launcher.exe（首次配置向导）")

    # 2) vendor：node + mermaid-cli + 语法预检
    fetch_node(platform, bundle / "vendor" / "node")
    npm_install(bundle / "vendor" / "mermaid-cli",
                {"@mermaid-js/mermaid-cli": MERMAID_CLI_VERSION})
    parse_dir = bundle / "vendor" / "parse"
    parse_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "scripts" / "mermaid_parse.mjs", parse_dir / "mermaid_parse.mjs")
    root_deps = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["dependencies"]
    npm_install(parse_dir, root_deps)

    # 3) 资源与配置模板
    shutil.copytree(ROOT / "styles", bundle / "styles")
    shutil.copytree(ROOT / "skills", bundle / "skills",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / ".env.example", bundle / ".env.example")
    (bundle / "快速上手.txt").write_text(QUICKSTART, encoding="utf-8")

    # 4) 归档（产物名带版本号：flowchart-agent-<platform>-vX.Y.Z.zip，见 pyproject）
    if archive_fmt == "zip":
        artifact = outdir / f"{bundle.name}-v{_version()}.zip"
        with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(bundle.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(outdir))
    else:
        artifact = outdir / f"{bundle.name}-v{_version()}.tar.gz"
        with tarfile.open(artifact, "w:gz") as tf:
            tf.add(bundle, arcname=bundle.name)
    size_mb = artifact.stat().st_size / 1024 / 1024
    log(f"完成：{artifact}（{size_mb:.0f} MB）")
    return artifact


def main() -> int:
    _fix_stdout_encoding()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", choices=list(PLATFORMS), required=True)
    ap.add_argument("--exe", type=Path, required=True, help="pyinstaller 产物路径")
    ap.add_argument("--outdir", type=Path, default=Path("dist"))
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    build(args.platform, args.exe, args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

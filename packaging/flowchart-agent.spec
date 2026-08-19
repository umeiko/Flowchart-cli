# PyInstaller 构建配置：单文件控制台程序 flowchart-agent
# 用法：uv run --with pyinstaller pyinstaller packaging/flowchart-agent.spec \
#          --distpath dist/bin --workpath build/pyinstaller --noconfirm
# 注意：spec 内相对路径以 spec 所在目录（packaging/）为基准，SPECPATH 即该目录。
# 外部运行时（node/mermaid-cli/styles/skills）不在此打包，由 packaging/make_bundle.py
# 以 vendor/ 目录形式组装到 exe 旁边（见 runtime.py 的解析逻辑）。

from pathlib import Path

ROOT = Path(SPECPATH).parent  # 项目根

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="flowchart-agent",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

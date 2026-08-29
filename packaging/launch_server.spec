# launch_server.exe：Windows 离线包的 Web/API 服务启动器。
# 仅用标准库，实际服务由同目录 flowchart-agent.exe 提供。

from pathlib import Path

a = Analysis(
    [str(Path(SPECPATH) / "launch_server.py")],
    pathex=[],
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
    name="launch_server",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

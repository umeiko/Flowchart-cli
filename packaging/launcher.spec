# launcher.exe 构建配置：仅 Windows 离线包使用的首次配置向导（packaging/onboard.py）。
# 仅用标准库，不打包 flowchart_agent 主包，产物极小。
# 用法：pyinstaller packaging/launcher.spec --distpath dist/bin --noconfirm
# （CI 仅在 win-x64 任务中执行，见 .github/workflows/release.yml）

from pathlib import Path

a = Analysis(
    [str(Path(SPECPATH) / "onboard.py")],
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
    name="launcher",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

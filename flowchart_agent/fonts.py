"""应用自带字体目录：除系统字体外，启动时把 Fonts/ 下的字体注册给当前用户。

目录解析顺序：FLOWCHART_FONT_DIR 环境变量覆盖 > 冻结时 exe 旁 Fonts/ >
项目根 Fonts/。把字体文件放进该目录即可，无需手动安装。

Windows 实现为**当前用户级字体安装**：复制到
%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts 并写 HKCU 字体注册表——
不需要管理员权限。这是唯一可靠路径：draw.io 桌面版 / Chrome 走
DirectWrite 枚举字体，AddFontResource 式的会话级注册它们看不到。
安装是幂等的（注册表项存在且文件在位即跳过），装一次长期有效。
其它平台暂不支持（debug 日志跳过）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import sys
from pathlib import Path

from . import runtime

logger = logging.getLogger(__name__)

_FONT_EXTS = (".ttf", ".otf", ".ttc")


def font_dir() -> Path | None:
    """字体目录：FLOWCHART_FONT_DIR 覆盖 > 冻结时 exe 旁 Fonts/ > 项目根 Fonts/。"""
    env = os.getenv("FLOWCHART_FONT_DIR")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    p = runtime.app_dir() / "Fonts"
    return p if p.is_dir() else None


def _family_names(path: Path) -> set[str]:
    """解析 TTF/OTF name 表，返回全部族名记录（中英文别名都收，用于匹配）。"""
    names: set[str] = set()
    try:
        data = path.read_bytes()
        num = struct.unpack(">H", data[4:6])[0]
        off_name = None
        for i in range(num):
            rec = data[12 + i * 16: 12 + i * 16 + 16]
            if rec[0:4] == b"name":
                off_name = struct.unpack(">I", rec[8:12])[0]
                break
        if off_name is None:
            return names
        count, stroff = struct.unpack(">HH", data[off_name + 2:off_name + 6])
        for i in range(count):
            r = off_name + 6 + i * 12
            pid, _eid, _lid, nid, ln, off = struct.unpack(">6H", data[r:r + 12])
            if nid not in (16, 1) or pid != 3:  # typographic/legacy family，Windows 记录
                continue
            names.add(
                data[off_name + stroff + off: off_name + stroff + off + ln]
                .decode("utf-16-be", "replace")
            )
    except (OSError, struct.error, IndexError):
        pass
    return names


def _family_name(path: Path) -> str | None:
    """取单个代表族名（注册表项命名用）：优先中文名，其次任意记录。"""
    names = _family_names(path)
    if not names:
        return None
    return next(
        (n for n in names if any("一" <= ch <= "鿿" for ch in n)),
        sorted(names)[0],
    )


def register_fonts() -> list[Path]:
    """把字体目录里的字体按当前用户安装，返回本次新安装的路径列表。

    幂等：注册表项已存在且目标文件在位则跳过。非 Windows 或目录不存在
    时返回空列表。
    """
    if sys.platform != "win32":
        if font_dir() is not None:
            logger.debug("自带字体目录注册目前仅支持 Windows，跳过")
        return []
    d = font_dir()
    if d is None:
        return []

    import winreg

    user_fonts = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts"
    reg_path = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
    installed: list[Path] = []
    user_fonts.mkdir(parents=True, exist_ok=True)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
    ) as key:
        for f in sorted(d.iterdir()):
            if f.suffix.lower() not in _FONT_EXTS:
                continue
            family = _family_name(f) or f.stem
            suffix = "(OpenType)" if f.suffix.lower() == ".otf" else "(TrueType)"
            value_name = f"{family} {suffix}"
            dest = user_fonts / f.name
            try:
                existing, _ = winreg.QueryValueEx(key, value_name)
                if Path(existing).is_file():
                    continue  # 已安装且文件在位
            except OSError:
                pass  # 无此注册表项
            try:
                if not dest.is_file():
                    shutil.copy2(f, dest)
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, str(dest))
                installed.append(f)
                logger.info(
                    "[fonts] 已按当前用户安装字体：%s（%s）", f.name, family
                )
            except OSError as e:
                logger.warning("[fonts] 字体安装失败：%s（%s）", f.name, e)
    return installed


_FONT_REG_SUFFIX = re.compile(r"\s*\((TrueType|OpenType|PostScript)\)$", re.I)


def available_families() -> set[str]:
    """当前可见的字体族名集合：系统已安装（HKCU+HKLM 注册表）+ 自带 Fonts/ 目录。

    名字统一小写后比较；自带字体同时收集中英文族名别名。
    非 Windows 只返回自带目录部分（系统枚举未实现，缺字体检查会偏保守多报）。
    """
    families: set[str] = set()
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
                ) as key:
                    i = 0
                    while True:
                        try:
                            name, _, _ = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        i += 1
                        families.add(_FONT_REG_SUFFIX.sub("", name).strip().lower())
            except OSError:
                continue
    d = font_dir()
    if d is not None:
        for f in d.iterdir():
            if f.suffix.lower() in _FONT_EXTS:
                families.update(n.strip().lower() for n in _family_names(f))
    return families


def check_font_available(family: str | None) -> str | None:
    """配置的字体是否可用；不可用返回给用户看的提示文案，可用/未配置返回 None。"""
    if not family:
        return None
    if family.strip().lower() in available_families():
        return None
    return (
        f"DRAWIO_FONT_FAMILY 配置的字体「{family}」本机未安装，Fonts/ 目录里也没有，"
        "draw.io 渲染会静默回退默认字体。请把字体文件（.ttf/.otf）放入 Fonts/ 目录"
        "（重启本工具自动注册），或手动安装该字体。"
    )

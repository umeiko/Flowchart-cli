"""技能包系统：skills/ 目录下的 markdown 文件即"提示词型"技能插件。

与风格插件（styles.py）同构：frontmatter 声明 name/description，正文是
写给主 Agent 的操作手册。主 Agent 用 list_skill_packs 发现、use_skill 读取
正文后按指引执行（配合 read_document / find_files / 流程图工具完成）。
适合接入社区流传的 SKILL.md 式技能包；需要执行脚本的技能不在本系统范围内。
目录可用 FLOWCHART_SKILL_DIR 环境变量覆盖，默认 ./skills。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillPack:
    name: str
    description: str  # 一句话说明 + 触发场景，主 Agent 据此决定是否选用
    instructions: str  # markdown 正文：use_skill 时注入给主 Agent 的操作指引


def skill_packs_dir() -> Path:
    return Path(os.getenv("FLOWCHART_SKILL_DIR", "skills"))


def load_skill_packs(directory: Path | None = None) -> dict[str, SkillPack]:
    """扫描技能包目录，返回 {name: SkillPack}。每次调用重新扫描，新增文件即时生效。"""
    d = directory or skill_packs_dir()
    packs: dict[str, SkillPack] = {}
    if not d.is_dir():
        return packs
    for path in sorted(d.glob("*.md")):
        pack = _parse_pack_file(path)
        if pack is not None:
            packs[pack.name] = pack
    return packs


def get_skill_pack(name: str, directory: Path | None = None) -> SkillPack:
    """按名称取技能包，不存在时抛 ValueError（附可用技能包列表）。"""
    packs = load_skill_packs(directory)
    pack = packs.get(name.strip().lower())
    if pack is None:
        available = "、".join(packs) or "（skills 目录为空）"
        raise ValueError(f"未知技能包 {name!r}，可用技能包：{available}")
    return pack


def _parse_pack_file(path: Path) -> SkillPack | None:
    """解析 frontmatter（--- 包裹的 key: value 行）；无 frontmatter 的文件忽略。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return None
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("'\"")
    if not meta.get("name") or not meta.get("description"):
        return None
    return SkillPack(
        name=meta["name"].lower(),
        description=meta["description"],
        instructions=parts[2].strip(),
    )

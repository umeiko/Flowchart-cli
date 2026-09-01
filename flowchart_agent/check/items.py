"""从 ``kind: check`` 的 Skill 文档加载检查项。

核心只定义 Markdown 解析协议，不再内置任何领域检查项或判定标准。检查 Skill
可分散在多个文件中；二级分类器会看到这些 Skill 提供的检查项并选择相关项。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..skillpacks import SkillPack, load_skill_packs

logger = logging.getLogger(__name__)

_ITEM_HEADER = re.compile(
    r"^##\s+check:\s*([a-zA-Z0-9_-]+)\s*\|\s*(.+?)\s*$", re.MULTILINE
)
_BATCH_SECTION = re.compile(
    r"^##\s+batch\s*$\n?(.*?)(?=^##\s+check:|\Z)", re.MULTILINE | re.DOTALL
)
_APPLIES_TO = re.compile(r"^applies_to:\s*(.*?)\s*$", re.MULTILINE)
_APPLIES_TO_SEPARATOR = re.compile(r"[,，、;/；|]+")
_ALL_IMAGE_ALIASES = {
    "*", "all", "any", "所有", "全部", "任意", "不限",
    "所有图片", "全部图片", "任意图片", "任何图片",
    "所有图像", "全部图像", "任意图像", "任何图像",
    "所有类型", "全部类型", "allimages", "anyimage", "anyimages",
}


@dataclass(frozen=True)
class CheckItem:
    id: str
    name: str
    applies_to: frozenset[str]
    prompt: str
    source_skill: str
    source_description: str


def _parse_applies_to(raw: str) -> frozenset[str]:
    """容错解析人类编写的适用范围；空集合表示适用于任何图片。"""
    parts = [part.strip() for part in _APPLIES_TO_SEPARATOR.split(raw) if part.strip()]
    compact = {re.sub(r"\s+", "", part).casefold() for part in parts}
    if not parts or compact & _ALL_IMAGE_ALIASES:
        return frozenset()
    return frozenset(parts)


def parse_check_skill(pack: SkillPack) -> list[CheckItem]:
    """解析一个检查 Skill；格式不合法时返回空列表。"""
    if pack.kind != "check":
        return []
    matches = list(_ITEM_HEADER.finditer(pack.instructions))
    if not matches:
        logger.warning("[check] Skill %s 没有合法的 check 条目", pack.name)
        return []

    # 第一个检查项前的正文是所有条目共用的执行/判定协议。
    common = _BATCH_SECTION.sub("", pack.instructions[:matches[0].start()]).strip()
    items: list[CheckItem] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(pack.instructions)
        body = pack.instructions[match.end():end].strip()
        applies_match = _APPLIES_TO.search(body)
        if applies_match is None:
            logger.warning(
                "[check] Skill %s 的条目 %s 缺少 applies_to", pack.name, match.group(1)
            )
            continue
        applies_to = _parse_applies_to(applies_match.group(1).strip())
        prompt_body = (body[:applies_match.start()] + body[applies_match.end():]).strip()
        if not prompt_body:
            logger.warning(
                "[check] Skill %s 的条目 %s 没有检查说明", pack.name, match.group(1)
            )
            continue
        prompt = "\n\n".join(part for part in (prompt_body, common) if part)
        items.append(CheckItem(
            id=match.group(1).lower(),
            name=match.group(2).strip(),
            applies_to=applies_to,
            prompt=prompt,
            source_skill=pack.name,
            source_description=pack.description,
        ))
    return items


def parse_check_batch_protocol(pack: SkillPack) -> str:
    """读取检查 Skill 的可选批量规划协议，不把它注入每个单项检查 prompt。"""
    if pack.kind != "check":
        return ""
    match = _BATCH_SECTION.search(pack.instructions)
    return match.group(1).strip() if match else ""


def load_check_batch_protocol(directory: Path | None = None) -> str:
    blocks = []
    for pack in load_skill_packs(directory).values():
        protocol = parse_check_batch_protocol(pack)
        if protocol:
            blocks.append(f"Skill {pack.name}:\n{protocol}")
    return "\n\n".join(blocks)


def load_check_items(directory: Path | None = None) -> list[CheckItem]:
    """扫描当前会话 Skills，合并全部合法检查条目。重复 id 的后项被忽略。"""
    items: list[CheckItem] = []
    seen: set[str] = set()
    for pack in load_skill_packs(directory).values():
        for item in parse_check_skill(pack):
            if item.id in seen:
                logger.warning(
                    "[check] 重复检查项 id=%s（来自 Skill %s），已忽略", item.id, pack.name
                )
                continue
            seen.add(item.id)
            items.append(item)
    return items


def items_block(items: list[CheckItem]) -> str:
    """给分类模型的检查 Skill/检查项清单。"""
    lines: list[str] = []
    current_skill = None
    for item in items:
        if item.source_skill != current_skill:
            current_skill = item.source_skill
            lines.append(f"Skill {item.source_skill}：{item.source_description}")
        applies = "、".join(sorted(item.applies_to)) or "任何图片"
        lines.append(f"- {item.id}：{item.name}（适用：{applies}）")
    return "\n".join(lines)


def items_overview(items: list[CheckItem]) -> str:
    return "\n".join(
        f"- {item.id}（{item.name}；Skill: {item.source_skill}）" for item in items
    )


def resolve_items(
    selection: list[str] | str, available: list[CheckItem]
) -> list[CheckItem]:
    """按分类结果选择检查项；未知 id 不再回退到内置默认项。"""
    if selection == "all":
        return list(available)
    registry = {item.id: item for item in available}
    return [registry[item_id] for item_id in selection if item_id in registry]

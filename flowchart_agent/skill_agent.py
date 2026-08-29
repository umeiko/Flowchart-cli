from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm import LLMClient
from .skillpacks import parse_skill_pack_text, skill_packs_dir

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class SkillResult:
    ok: bool
    path: Path | None = None
    error: str = ""
    rounds: int = 0


class SkillAgent:
    """自然语言需求 → Session 级提示词型 Skill 文件。"""

    def __init__(self, settings: Settings, directory: str | Path | None = None):
        self._llm = LLMClient(settings.text_model)
        self._directory = (
            Path(directory).resolve() if directory is not None else skill_packs_dir()
        )

    def create(self, name: str, description: str) -> SkillResult:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            return SkillResult(False, error="Skill 名称只能含小写字母、数字、下划线或连字符")
        target = self._directory / f"{name}.md"
        if target.exists():
            return SkillResult(False, error=f"Skill {name!r} 已存在")
        previous = ""
        feedback = ""
        for round_no in range(1, 4):
            user = (
                f"Skill 名称必须是 {name!r}。\n用户需求：\n{description}"
                if not previous else
                f"修复下面的 Skill 文档。问题：{feedback}\n原文：\n{previous}"
            )
            raw = self._llm.chat([
                {"role": "system", "content": (
                    "你是 Agent Skill 设计师。生成提示词型 Skill 的完整 Markdown。"
                    "必须以 --- front matter 开始和结束，包含 name 与 description；"
                    "正文写清触发条件、执行步骤、限制和完成标准。只输出文件内容。"
                )},
                {"role": "user", "content": user},
            ])
            match = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", raw, re.DOTALL)
            content = (match.group(1) if match else raw).strip() + "\n"
            parsed = parse_skill_pack_text(content)
            if parsed is not None and parsed.name == name:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return SkillResult(True, target, rounds=round_no)
            previous = content
            feedback = "front matter 无效，或 name 与指定名称不一致"
        return SkillResult(False, error=feedback, rounds=3)

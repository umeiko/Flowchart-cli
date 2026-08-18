"""检查项注册表：路由到 check 之后才向模型/用户披露的技能清单。

每个检查项声明自己适用的图片类型（applies_to，按图片描述中的类型关键词匹配），
素材中没有对应类型图片时直接判"不符合该分类"，不浪费模型调用；
applies_to 为空集表示对任何图片适用（如敏感信息检查）。
新增检查项 = prompts/check/items/ 加一个 prompt 文件 + 此处注册一行。
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import prompts


@dataclass(frozen=True)
class CheckItem:
    id: str
    name: str  # 中文名（报告与进度提示用）
    applies_to: frozenset[str]  # 适用图片类型关键词；空集 = 任何图片
    prompt: str  # 检查清单模板（含 {document} 占位符）


_CHECK_ITEMS: list[CheckItem] = [
    CheckItem(
        id="schematic_consistency",
        name="原理图与原理描述一致性",
        applies_to=frozenset({"原理图"}),
        prompt=prompts.check.items.SCHEMATIC_CONSISTENCY_PROMPT,
    ),
    CheckItem(
        id="flowchart_correctness",
        name="流程图正确性",
        applies_to=frozenset({"流程图"}),
        prompt=prompts.check.items.FLOWCHART_CORRECTNESS_PROMPT,
    ),
    CheckItem(
        id="flowchart_steps",
        name="流程图与操作步骤一致性",
        applies_to=frozenset({"流程图"}),
        prompt=prompts.check.items.FLOWCHART_STEPS_PROMPT,
    ),
    CheckItem(
        id="network_correctness",
        name="组网图正确性",
        applies_to=frozenset({"组网", "拓扑"}),
        prompt=prompts.check.items.NETWORK_CORRECTNESS_PROMPT,
    ),
    CheckItem(
        id="network_consistency",
        name="组网图与组网描述一致性",
        applies_to=frozenset({"组网", "拓扑"}),
        prompt=prompts.check.items.NETWORK_CONSISTENCY_PROMPT,
    ),
    CheckItem(
        id="ui_correctness",
        name="界面截图正确性",
        applies_to=frozenset({"界面截图", "界面"}),
        prompt=prompts.check.items.UI_CORRECTNESS_PROMPT,
    ),
    CheckItem(
        id="ui_terminology",
        name="界面词与实际界面一致性",
        applies_to=frozenset({"界面截图", "界面"}),
        prompt=prompts.check.items.UI_TERMINOLOGY_PROMPT,
    ),
    CheckItem(
        id="ui_sensitive",
        name="界面截图敏感信息检查",
        applies_to=frozenset(),  # 任何图片都可能含敏感信息
        prompt=prompts.check.items.UI_SENSITIVE_PROMPT,
    ),
    CheckItem(
        id="ui_steps",
        name="界面截图与操作步骤一致性",
        applies_to=frozenset({"界面截图", "界面"}),
        prompt=prompts.check.items.UI_STEPS_PROMPT,
    ),
]

CHECK_ITEMS: dict[str, CheckItem] = {item.id: item for item in _CHECK_ITEMS}


def items_block() -> str:
    """披露给分类模型的检查项清单（注入 classify prompt）。"""
    return "\n".join(f"- {item.id}：{item.name}" for item in _CHECK_ITEMS)


def items_overview() -> str:
    """给用户看的检查项清单。"""
    return "\n".join(f"- {item.id}（{item.name}）" for item in _CHECK_ITEMS)


def resolve_items(selection: list[str] | str) -> list[CheckItem]:
    """把分类结果解析为检查项列表；"all" 或未知 id 兜底为全部。"""
    if selection == "all":
        return list(_CHECK_ITEMS)
    picked = [CHECK_ITEMS[i] for i in selection if i in CHECK_ITEMS]
    return picked or list(_CHECK_ITEMS)

"""细粒度检查项 prompt（9 项，每项一个文件，按文件名定位）。"""

from .base import ITEM_OUTPUT_FORMAT
from .schematic_consistency import SCHEMATIC_CONSISTENCY_PROMPT
from .flowchart_correctness import FLOWCHART_CORRECTNESS_PROMPT
from .flowchart_steps import FLOWCHART_STEPS_PROMPT
from .network_correctness import NETWORK_CORRECTNESS_PROMPT
from .network_consistency import NETWORK_CONSISTENCY_PROMPT
from .ui_correctness import UI_CORRECTNESS_PROMPT
from .ui_terminology import UI_TERMINOLOGY_PROMPT
from .ui_sensitive import UI_SENSITIVE_PROMPT
from .ui_steps import UI_STEPS_PROMPT

__all__ = [
    "ITEM_OUTPUT_FORMAT",
    "SCHEMATIC_CONSISTENCY_PROMPT",
    "FLOWCHART_CORRECTNESS_PROMPT",
    "FLOWCHART_STEPS_PROMPT",
    "NETWORK_CORRECTNESS_PROMPT",
    "NETWORK_CONSISTENCY_PROMPT",
    "UI_CORRECTNESS_PROMPT",
    "UI_TERMINOLOGY_PROMPT",
    "UI_SENSITIVE_PROMPT",
    "UI_STEPS_PROMPT",
]

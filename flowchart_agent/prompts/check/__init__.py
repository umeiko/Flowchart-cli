"""检查管线的通用 prompt；领域检查项由 ``kind: check`` Skill 提供。"""

from .describe import DESCRIBE_IMAGE_PROMPT
from .classify import CLASSIFY_CHECK_SYSTEM, CLASSIFY_CHECK_USER

__all__ = [
    "DESCRIBE_IMAGE_PROMPT",
    "CLASSIFY_CHECK_SYSTEM",
    "CLASSIFY_CHECK_USER",
]

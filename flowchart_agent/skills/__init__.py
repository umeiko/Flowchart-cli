"""最小 Skill 抽象：name / description / inputSchema(JSON Schema) / handler。

Schema 与 MCP 工具的 inputSchema 同构，后续要接入 MCP 时可直接映射；
OpenAI function calling 的 tools 定义也由它派生。
"""

from .base import Skill
from .builtin import build_skills

__all__ = ["Skill", "build_skills"]

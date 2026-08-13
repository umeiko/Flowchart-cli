"""Skill 基类：一个可被主 Agent 调用的能力单元。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Skill:
    """最小 Skill 定义。

    - name/description/parameters 与 MCP 工具的 (name, description, inputSchema) 一一对应；
    - handler 接收参数 dict 解包后的关键字参数，返回字符串结果（给主 Agent 阅读）。
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema，type 固定为 "object"
    handler: Callable[..., str]

    def to_openai_tool(self) -> dict:
        """转换为 OpenAI function calling 的 tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

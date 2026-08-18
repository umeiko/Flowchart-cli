"""一级路由：把用户输入分类为 generate（生成图）/ check（检查图）/ chat（闲聊）。

用文本模型做一次廉价的 JSON 分类。解析失败或类别不可信时返回 None——
调用方回退到原有主 Agent 流程，宁可不错分。
"""

from __future__ import annotations

import json
import logging
import re

from .llm import LLMClient
from . import prompts

logger = logging.getLogger(__name__)

_VALID = ("generate", "check", "chat")


def route_category(llm: LLMClient, user_input: str, has_images: bool = False) -> str | None:
    """返回 generate / check / chat；无法确定时返回 None。"""
    images_note = "（用户同时附带了图片。）" if has_images else ""
    reply = llm.chat(
        [
            {"role": "system", "content": prompts.ROUTE_SYSTEM},
            {
                "role": "user",
                "content": prompts.ROUTE_USER.format(
                    user_input=user_input, images_note=images_note
                ),
            },
        ]
    )
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        logger.warning("[route] 分类输出无法解析，回退主 Agent 流程：%s", reply[:120])
        return None
    try:
        category = json.loads(m.group(0)).get("category", "")
    except json.JSONDecodeError:
        logger.warning("[route] 分类 JSON 解析失败，回退主 Agent 流程：%s", m.group(0)[:120])
        return None
    if category not in _VALID:
        logger.warning("[route] 未知类别 %r，回退主 Agent 流程", category)
        return None
    logger.info("[route] 一级分类：%s（%s）", category,
                json.loads(m.group(0)).get("reason", ""))
    return category

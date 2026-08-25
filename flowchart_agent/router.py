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


_VALID_TYPES = ("flowchart", "architecture")

# 启发式兜底关键词（LLM 分类失败时按命中数判断，平局归 architecture——
# drawio 引擎的主战场；误判为架构图只是少箭头，误判为流程图会丢分层结构）
_FLOW_KEYWORDS = ("流程", "步骤", "判断", "是否", "开始", "结束", "分支", "流转")
_ARCH_KEYWORDS = ("架构", "分层", "层级", "模块", "子系统", "组成", "层")


def route_diagram_type(llm: LLMClient, document: str) -> str:
    """判断文档要画 flowchart 还是 architecture（drawio 引擎的二级路由）。

    LLM JSON 分类；解析失败时按关键词命中数兜底，平局归 architecture。
    """
    try:
        reply = llm.chat(
            [
                {"role": "system", "content": prompts.DIAGRAM_TYPE_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.DIAGRAM_TYPE_USER.format(
                        document=document[:4000]
                    ),
                },
            ]
        )
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            dtype = data.get("diagram_type", "")
            if dtype in _VALID_TYPES:
                logger.info("[route] 图型分类：%s（%s）", dtype, data.get("reason", ""))
                return dtype
        logger.warning("[route] 图型分类输出无法解析，回退关键词兜底：%s", reply[:120])
    except Exception as e:  # 分类失败不应阻断主流程
        logger.warning("[route] 图型分类调用失败，回退关键词兜底：%s", e)
    flow_hits = sum(1 for k in _FLOW_KEYWORDS if k in document)
    arch_hits = sum(1 for k in _ARCH_KEYWORDS if k in document)
    dtype = "flowchart" if flow_hits > arch_hits else "architecture"
    logger.info("[route] 图型关键词兜底：%s（流程词 %d 个 / 架构词 %d 个）",
                dtype, flow_hits, arch_hits)
    return dtype

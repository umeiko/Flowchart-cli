"""二级分类器：图片描述 + 检查需求 → 要执行的检查项子集与素材路径。

技术路线（与用户确认的方案一致）：
1. 图片先过视觉模型生成文字描述（describe_images）；
2. 文本模型拿"用户需求 + 图片描述 + 检查项清单"做选择——用户明确指定检查项时
   只返回对应项，否则返回 "all"；并顺带提取输入中提到的文件路径。
检查项清单由 items.items_block() 注入 prompt，实现渐进式披露。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..cancellation import CancelCheck, OperationCancelled, raise_if_cancelled
from ..llm import LLMClient
from .. import prompts
from .items import items_block

logger = logging.getLogger(__name__)


@dataclass
class Classification:
    items: list[str] | str = "all"  # 检查项 id 列表，或 "all"
    doc_paths: list[str] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    reason: str = ""
    raw: str = ""  # 模型原始输出（落盘复盘用）


class CheckClassifier:
    def __init__(
        self, text_llm: LLMClient, vision_llm: LLMClient,
        should_cancel: CancelCheck = None,
    ):
        self._text = text_llm
        self._vision = vision_llm
        self._should_cancel = should_cancel

    def describe_images(self, images: list[Path]) -> list[tuple[Path, str]]:
        """每张图片 → (路径, 文字描述)。描述失败的图片跳过并记日志。"""
        results = []
        for img in images:
            raise_if_cancelled(self._should_cancel)
            try:
                desc = self._vision.chat_with_image(
                    prompts.check.DESCRIBE_IMAGE_PROMPT,
                    img,
                    should_cancel=self._should_cancel,
                )
            except OperationCancelled:
                raise
            except Exception as e:
                logger.warning("[check] 图片描述失败 %s：%s", img, e)
                continue
            logger.info("[check] 图片描述完成：%s（%d 字符）", img.name, len(desc))
            results.append((img, desc.strip()))
        return results

    def classify(
        self, requirement: str, descriptions: list[tuple[Path, str]]
    ) -> Classification | None:
        """返回分类结果；输出无法解析时返回 None。"""
        if descriptions:
            block = "附图的内容描述：\n" + "\n\n".join(
                f"<image name=\"{p.name}\">\n{d}\n</image>" for p, d in descriptions
            )
        else:
            block = "（用户未附带图片）"
        reply = self._text.chat(
            [
                {
                    "role": "system",
                    "content": prompts.check.CLASSIFY_CHECK_SYSTEM.format(
                        items_block=items_block()
                    ),
                },
                {
                    "role": "user",
                    "content": prompts.check.CLASSIFY_CHECK_USER.format(
                        requirement=requirement, descriptions_block=block
                    ),
                },
            ],
            should_cancel=self._should_cancel,
        )
        m = re.search(r"\{.*\}", reply, re.DOTALL)
        if not m:
            logger.warning("[check] 二级分类输出无法解析：%s", reply[:120])
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            logger.warning("[check] 二级分类 JSON 解析失败：%s", m.group(0)[:120])
            return None
        items = data.get("items", "all")
        if isinstance(items, str) and items != "all":
            items = [items]
        result = Classification(
            items=items,
            doc_paths=[str(p) for p in data.get("doc_paths") or []],
            image_paths=[str(p) for p in data.get("image_paths") or []],
            reason=data.get("reason", ""),
            raw=reply,
        )
        logger.info(
            "[check] 二级分类：items=%s（%s）文档=%s 图片=%s",
            result.items, result.reason, result.doc_paths, result.image_paths,
        )
        return result

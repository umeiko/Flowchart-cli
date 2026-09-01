"""单项检查子 Agent：一个检查项 × 适用图片的逐项比对。

三值结论：通过 / 不通过 / 不符合该分类。素材中没有本项适用类型的图片时
直接判"不符合该分类"，不调用模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..cancellation import CancelCheck, raise_if_cancelled
from ..llm import LLMClient
from .items import CheckItem

logger = logging.getLogger(__name__)

VERDICT_PASS = "通过"
VERDICT_FAIL = "不通过"
VERDICT_NA = "不符合该分类"


@dataclass
class ItemResult:
    item: CheckItem
    image: Path | None  # None 表示整项不适用（无适用素材）
    verdict: str
    findings: str  # PASS 的简述 / FAIL 的问题列表 / NA 的原因
    raw: str


def _verdict_of(reply: str) -> str:
    first = reply.splitlines()[0].strip().upper() if reply.strip() else ""
    if first.startswith("PASS"):
        return VERDICT_PASS
    if first.startswith("NA"):
        return VERDICT_NA
    return VERDICT_FAIL


def _strip_verdict_line(reply: str, default: str) -> str:
    rest = "\n".join(reply.splitlines()[1:]).strip()
    return rest or default


class ItemCheckAgent:
    """执行单个检查项的子 Agent。"""

    def __init__(
        self, vision_llm: LLMClient, should_cancel: CancelCheck = None
    ):
        self._vision = vision_llm
        self._should_cancel = should_cancel

    def run(
        self,
        item: CheckItem,
        images: list[Path],
        kinds: dict[Path, str],
        document: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[ItemResult]:
        """对本项适用的每张图片执行检查，返回逐项结果。"""
        applicable = [
            img for img in images
            if not item.applies_to or not kinds.get(img)
            or any(
                expected.casefold() in kinds[img].casefold()
                or kinds[img].casefold() in expected.casefold()
                for expected in item.applies_to
            )
        ]
        if not applicable:
            kinds_seen = "、".join(sorted({kinds.get(i, "未知") for i in images}))
            logger.info("[check] %s：无适用素材（素材类型：%s），不符合该分类",
                        item.name, kinds_seen)
            return [ItemResult(
                item, None, VERDICT_NA,
                f"素材中没有{item.applies_to and ' / '.join(sorted(item.applies_to)) or '相关'}类型的图片"
                f"（素材类型：{kinds_seen}）",
                "",
            )]
        # Skill 文档可能包含 JSON/代码花括号；只替换约定占位符，避免 str.format
        # 把审查标准中的其他花括号误当模板字段。
        prompt = item.prompt.replace("{document}", document)
        results = []
        progress = on_progress or (lambda _message: None)
        for img in applicable:
            raise_if_cancelled(self._should_cancel)
            streamed_chars = 0
            reported_chars = 0

            def report_delta(text: str) -> None:
                nonlocal streamed_chars, reported_chars
                streamed_chars += len(text)
                if streamed_chars - reported_chars >= 120:
                    reported_chars = streamed_chars
                    progress(
                        f"{item.name} · {img.name}：已接收 {streamed_chars} 字符…"
                    )

            stream = getattr(self._vision, "chat_with_image_stream", None)
            if stream is not None:
                reply = stream(
                    prompt,
                    img,
                    on_delta=report_delta,
                    should_cancel=self._should_cancel,
                ).strip()
            else:
                reply = self._vision.chat_with_image(
                    prompt, img, should_cancel=self._should_cancel
                ).strip()
            verdict = _verdict_of(reply)
            findings = _strip_verdict_line(
                reply, {"通过": "通过", "不符合该分类": "不适用"}.get(verdict, "")
            )
            logger.info("[check] %s × %s：%s", item.name, img.name, verdict)
            results.append(ItemResult(item, img, verdict, findings, reply))
        return results

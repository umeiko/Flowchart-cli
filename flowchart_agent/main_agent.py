"""主 Agent：对话式调度。通过 function calling 调用 Skill 完成用户意图。

图片处理（TEXT_MODEL_VISION=true 时）：
- 用户在 TUI 贴入的图片随本轮 user 消息发给模型（历史里只保留路径文本，
  避免 base64 每轮重复发送）；
- read_image 工具读取的图片进入队列，在下一次 LLM 调用时以 user 消息注入。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .config import Settings
from .images import image_data_url, validate_image
from .llm import LLMClient
from .session import DiagramSession
from .skills import build_skills

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8

MAIN_SYSTEM = """你是一个流程图生成助手的主控 Agent，通过调用工具帮用户完成流程图工作。

意图判断：
- 用户给出文档路径：先 read_document 读取，再用 create_diagram 以文档内容为需求生成图；
- 用户直接口述新图需求：直接 create_diagram；若用户提供了参考图片路径，传给 image_path；
- 用户消息中直接附带了图片内容：结合图片理解需求，再 create_diagram 或 modify_diagram；
- 用户给出图片路径并希望你查看：read_image；
- 用户对已有图提出修改意见：modify_diagram（不要重新 create）；
- 用户提出风格相关需求（深色、浅色、商务、手绘等）：先 list_styles 发现 styles/
  目录中的风格模板，选最匹配的一个（create_diagram 的 style 参数或 set_style 切换）；
  没有匹配模板时按默认风格处理并如实告知用户；
- 用户想看当前图的代码或位置：get_current_diagram；
- 与流程图无关的闲聊：不用工具，简短回答并把话题引回流程图。

规则：
- 不要自己手写或复述大段 Mermaid 代码，图一律通过工具生成；
- 工具报错时先自我纠正再重试，至少尝试 2~3 次后再向用户求助。例如文件不存在时：
  利用错误信息里给出的候选路径重试，或用 find_files 按关键词猜测正确文件；
- 多次尝试仍失败时，如实说明原因，并给出可行的下一步建议；
- 工具成功后，用一两句话告诉用户结果与图片路径；
- 回答简洁。"""

_VISION_ON = "\n\n当前主模型图像输入：已开启，可以处理用户贴入的图片和 read_image 读取的图片。"
_VISION_OFF = (
    "\n\n当前主模型图像输入：未开启（TEXT_MODEL_VISION=false）。"
    "工具列表中没有 read_image；若用户提出图片相关需求，"
    "请提示用户在 .env 中把 TEXT_MODEL_VISION 设为 true 并使用支持图片输入的模型。"
)

_NO_VISION_REPLY = (
    "你附带了图片，但当前文本模型未开启多模态能力"
    "（TEXT_MODEL_VISION=false）。请在 .env 中把它设为 true，"
    "并确保 TEXT_MODEL_NAME 是支持图片输入的模型。"
)


class _PendingImages:
    """read_image 写入的图片队列；在下一次 LLM 调用时随消息发出（仅一次）。"""

    def __init__(self, vision_enabled: bool):
        self._vision = vision_enabled
        self._paths: list[Path] = []

    def add(self, path: str) -> str:
        if not self._vision:
            return "错误：主模型未开启图像输入（TEXT_MODEL_VISION=false），无法看图。"
        try:
            p = validate_image(path)
        except ValueError as e:
            return f"错误：{e}"
        self._paths.append(p)
        return f"已读取图片 {p}，内容将在下一步对你可见。"

    def take(self) -> list[Path]:
        paths, self._paths = self._paths, []
        return paths


class MainAgent:
    def __init__(
        self,
        settings: Settings,
        session: DiagramSession,
        on_tool_call: Callable[[str, str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ):
        self._llm = LLMClient(settings.text_model)
        self._vision = settings.text_model_vision
        self._pending = _PendingImages(self._vision)
        skills = build_skills(session, self._pending)
        if not self._vision:  # 无视觉能力时不下发 read_image，避免模型误调
            skills = [s for s in skills if s.name != "read_image"]
        self._skills = {s.name: s for s in skills}
        self._tools = [s.to_openai_tool() for s in self._skills.values()]
        system = MAIN_SYSTEM + (_VISION_ON if self._vision else _VISION_OFF)
        self._messages: list[dict] = [{"role": "system", "content": system}]
        self._on_tool_call = on_tool_call  # 界面层用来展示工具调用过程
        self._on_delta = on_delta  # 界面层用来流式显示模型输出

    def chat(self, user_input: str, images: list[Path] | None = None) -> str:
        if images and not self._vision:
            return _NO_VISION_REPLY

        # 历史中只保留文本（含图片路径），base64 只在本次调用出现一次
        history_text = user_input
        if images:
            history_text += "\n[附带图片：" + "、".join(str(p) for p in images) + "]"
        self._messages.append({"role": "user", "content": history_text})
        override = self._multimodal_message(user_input, images) if images else None

        for _ in range(MAX_TOOL_ITERATIONS):
            messages = self._messages
            if override is not None:
                messages = self._messages[:-1] + [override]
                override = None
            if self._on_delta is not None:
                msg = self._llm.chat_with_tools_stream(
                    messages, self._tools, on_delta=self._on_delta
                )
            else:
                msg = self._llm.chat_with_tools(messages, self._tools)
            if not msg.tool_calls:
                self._messages.append({"role": "assistant", "content": msg.content or ""})
                return msg.content or ""

            self._messages.append(self._assistant_message_dict(msg))
            for call in msg.tool_calls:
                if self._on_tool_call:
                    self._on_tool_call(call.function.name, call.function.arguments)
                result = self._execute(call.function.name, call.function.arguments)
                logger.info("[tool] %s(%s) -> %s", call.function.name,
                            call.function.arguments, result[:120].replace("\n", " "))
                self._messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

            # read_image 读取的图片：以一条新的 user 消息注入下一轮调用
            pending = self._pending.take()
            if pending:
                note = f"[系统] 以下是 read_image 读取的 {len(pending)} 张图片："
                self._messages.append({"role": "user", "content": note})
                override = self._multimodal_message(note, pending)
        return "（连续工具调用次数过多，本次请求已中止，请换个说法再试）"

    @staticmethod
    def _multimodal_message(text: str, images: list[Path]) -> dict:
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": image_data_url(p)}}
                for p in images
            ],
        }

    @staticmethod
    def _assistant_message_dict(msg) -> dict:
        """只保留继续对话所需的字段，剔除 reasoning_content 等扩展字段。"""
        return {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        }

    def _execute(self, name: str, arguments_json: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return f"错误：未知工具 {name}"
        try:
            args = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            return f"错误：工具参数不是合法 JSON：{arguments_json[:100]}"
        try:
            return skill.handler(**args)
        except Exception as e:  # 工具失败不应中断对话，把错误交还给模型处理
            logger.exception("skill %s 执行异常", name)
            return f"错误：工具执行失败：{e}"

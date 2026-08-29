"""主 Agent：对话式调度。通过 function calling 调用 Skill 完成用户意图。

一级路由（router.py）：每条用户输入先分类为 generate / check / chat，
check 类输入转交 CheckAgent（检查管线，产物落 output/check/），
其余走本模块的 function calling 循环（产物落 output/generate/）。

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

from .check import CheckAgent
from .config import Settings
from .images import image_data_url, validate_image
from .llm import LLMClient
from .router import route_category
from .session import DiagramSession
from .skills import build_skills

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8

MAIN_SYSTEM = """你是一个流程图生成助手的主控 Agent，通过调用工具帮用户完成流程图工作。

意图判断：
- 用户给出文档路径：先 read_document 读取，再用 create_diagram 以文档内容为需求生成图；
- 用户直接口述新图需求：直接 create_diagram；若用户提供了参考图片路径，传给 image_path；
- 用户消息中直接附带了图片内容：结合图片理解需求，再 create_diagram 或 modify_diagram；
- 用户给出图片路径并希望你查看：read_image（无视觉能力时改用 ocr_image 提取文字）；
- 用户提供多份素材（多份文档/图片）或需求复杂：先用 read_document / ocr_image
  逐份获取素材内容，write_working_doc 整合成工作文档（markdown：各素材要点 +
  初步生成方案），再基于工作文档 create_diagram；工作文档可随时 read_working_doc
  查看、write_working_doc 修改——不要把大量素材原文长期堆在对话上下文里；
- 用户对已有图提出修改意见：modify_diagram（不要重新 create）；
- 用户只想调整当前图的风格/配色/主题、明确不改内容（"换成深色"、
  "按这个风格文档调整"）：restyle_diagram（不要用 modify_diagram，
  restyle 有骨架校验保证内容零改动；风格文档路径先 read_document 读取）；
- 用户提出风格相关需求（深色、浅色、商务、手绘等）：先 list_styles 发现 styles/
  目录中的风格模板，选最匹配的一个（create_diagram 的 style 参数或 set_style 切换）；
  现有模板都不匹配、或用户明确要求定制新风格时，用 create_style 生成新风格插件
  （成功后自动切换为当前风格）；都不需要时按默认风格处理；
- 用户明确要求运行系统命令、做格式转换或批量文件处理等：run_command
  （每条命令执行前会向用户请求确认；被拒绝后不要反复重试同一命令）；
- 用户要求调整检视/验证强度（如"别核对内容了"、"不用看图验证"）：set_verification；
  视觉验证反复因文字识别错误（看不清、读错字）不通过时，也可主动降为 layout；
  用户表示没有视觉模型可用时降为 code（文本模型审查源码），并告知用户；
- 用户想看当前图的代码或位置：get_current_diagram；
- 用户要求调整中间文档（工作文档、已生成的 markdown/文本文件），或要求把内容
  输出为其它格式的文件（markdown 表格、csv、清单等）：grep_files 定位内容、
  replace_in_file 精确替换、write_file 新建/整体覆盖；改动前先 read_document
  拿到原文，替换要有依据不要凭空改写；
- 遇到超出你既有能力的专业任务（特定导出格式、行业图表规范、第三方工具对接等），
  或用户提到某个技能/技能包：先 list_skill_packs 发现 skills/ 目录中的技能包，
  有匹配的就用 use_skill 读取指引并严格遵照执行；没有就如实说明，用现有能力完成；
- 用户明确要求把领域流程、操作规范或 Agent 指引沉淀为新技能时，用 create_skill
  生成并校验技能包；普通作图请求不要创建技能；
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
    "\n\n当前主模型图像输入：未开启（TEXT_MODEL_VISION=false），你不能直接看图："
    "- 用户贴图时消息中只带图片路径，需要图片内容时用 ocr_image 提取文字；"
    "- 工具列表中没有 read_image；若用户需要看图理解版式/配色的能力，"
    "提示用户在 .env 中把 TEXT_MODEL_VISION 设为 true 并使用支持图片输入的模型。"
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
        on_tool_result: Callable[[str, str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_tick: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        output_root: Path | None = None,
        readable_root: Path | None = None,
        readable_roots: list[Path] | tuple[Path, ...] | None = None,
        on_progress: Callable[[str], None] | None = None,
        command_runner=None,
        should_cancel: Callable[[], bool] | None = None,
    ):
        self._settings = settings
        self._llm = LLMClient(settings.text_model)
        self._vision = settings.text_model_vision
        self._pending = _PendingImages(self._vision)
        # 检查管线（check 分支）：产物落 <output_root>/check/；懒加载
        self._output_root = output_root
        self._readable_root = readable_root
        self._readable_roots = readable_roots
        self._check_agent: CheckAgent | None = None
        self._on_progress = on_progress  # 界面层进度提示（路由/检查项执行）
        # 主模型无视觉能力且配置了视觉模型时，用视觉模型提供 OCR 工具作为替代
        ocr_llm = (
            None
            if self._vision or settings.vision_model is None
            else LLMClient(settings.vision_model)
        )
        skills = build_skills(
            session,
            self._pending,
            ocr_llm=ocr_llm,
            command_runner=command_runner,
            readable_root=readable_root,
            readable_roots=readable_roots,
        )
        if not self._vision:  # 无视觉能力时不下发 read_image，避免模型误调
            skills = [s for s in skills if s.name != "read_image"]
        self._skills = {s.name: s for s in skills}
        self._tools = [s.to_openai_tool() for s in self._skills.values()]
        system = MAIN_SYSTEM + (_VISION_ON if self._vision else _VISION_OFF)
        self._messages: list[dict] = [{"role": "system", "content": system}]
        self._on_tool_call = on_tool_call  # 界面层用来展示工具调用过程
        self._on_tool_result = on_tool_result
        self._on_delta = on_delta  # 界面层用来流式显示模型输出
        self._on_tick = on_tick  # 界面层用来估算 token 用量（工具参数增量）
        self._on_reasoning = on_reasoning  # 界面层用来提示思考流（reasoning_content）
        self._should_cancel = should_cancel

    def restore_history(self, messages: list[dict]) -> None:
        """恢复持久化的用户/助手文本历史，不重放旧工具调用。"""
        system = self._messages[0]
        self._messages = [system] + [
            {"role": item["role"], "content": item["content"]}
            for item in messages if item.get("role") in {"user", "assistant"}
        ]

    def chat(self, user_input: str, images: list[Path] | None = None) -> str:
        # 主模型无视觉能力时，图片路径仍随消息进入对话，由 ocr_image 提取文字
        history_text = user_input
        if images:
            history_text += "\n[附带图片：" + "、".join(str(p) for p in images) + "]"
        logger.info(
            "[chat] 用户输入（%d 字符%s）：%s",
            len(user_input),
            f"，附带 {len(images)} 张图片" if images else "",
            history_text[:200].replace("\n", " "),
        )

        # 一级路由：check 类输入转交检查管线，不进入对话上下文
        category = route_category(self._llm, user_input, has_images=bool(images))
        if category == "check":
            if self._check_agent is None and self._output_root is not None:
                self._check_agent = CheckAgent(
                    self._settings,
                    self._output_root,
                    readable_root=self._readable_root,
                    readable_roots=self._readable_roots,
                )
            if self._check_agent is not None:
                if self._on_progress:
                    self._on_progress("已路由到文档检查，正在分析素材…")
                return self._check_agent.handle(
                    user_input, images or [], on_progress=self._on_progress
                )
            logger.warning("[route] 无 output_root，check 分支回退主 Agent 流程")

        self._messages.append({"role": "user", "content": history_text})
        override = None
        if images and self._vision:
            override = self._multimodal_message(user_input, images)

        for _ in range(MAX_TOOL_ITERATIONS):
            if self._should_cancel and self._should_cancel():
                return self._cancelled_reply()
            messages = self._messages
            if override is not None:
                messages = self._messages[:-1] + [override]
                override = None
            if self._on_delta is not None:
                msg = self._llm.chat_with_tools_stream(
                    messages, self._tools, on_delta=self._on_delta,
                    on_tick=self._on_tick, on_reasoning=self._on_reasoning,
                )
            else:
                msg = self._llm.chat_with_tools(messages, self._tools)
            if self._should_cancel and self._should_cancel():
                return self._cancelled_reply()
            if not msg.tool_calls:
                self._messages.append({"role": "assistant", "content": msg.content or ""})
                logger.info("[chat] 助手回复（%d 字符）", len(msg.content or ""))
                return msg.content or ""

            self._messages.append(self._assistant_message_dict(msg))
            for call in msg.tool_calls:
                args_preview = call.function.arguments[:200].replace("\n", " ")
                logger.info("[tool] 调用 %s(%s)", call.function.name, args_preview)
                if self._on_tool_call:
                    self._on_tool_call(call.function.name, call.function.arguments)
                result = self._execute(call.function.name, call.function.arguments)
                if self._on_tool_result:
                    self._on_tool_result(call.function.name, result)
                logger.info(
                    "[tool] %s 完成（结果 %d 字符）：%s",
                    call.function.name, len(result),
                    result[:150].replace("\n", " "),
                )
                self._messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
                if self._should_cancel and self._should_cancel():
                    return self._cancelled_reply()

            # read_image 读取的图片：以一条新的 user 消息注入下一轮调用
            pending = self._pending.take()
            if pending:
                note = f"[系统] 以下是 read_image 读取的 {len(pending)} 张图片："
                self._messages.append({"role": "user", "content": note})
                override = self._multimodal_message(note, pending)
        return "（连续工具调用次数过多，本次请求已中止，请换个说法再试）"

    def _cancelled_reply(self) -> str:
        reply = "生成已停止。已保留停止前最后一轮成功渲染的候选图。"
        self._messages.append({"role": "assistant", "content": reply})
        return reply

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
        """只保留继续对话所需的字段（content + tool_calls）。

        reasoning_content 是个例外：思考模式 + tool_calls 的网关（如
        deepseek）要求历史消息原样回传思考内容，否则下一轮请求 400——
        响应里带了的就带回去，没带的（别家网关）不加这个字段。
        """
        d = {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        }
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            d["reasoning_content"] = reasoning
        return d

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

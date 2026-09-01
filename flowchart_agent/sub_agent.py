"""受限文件子 Agent：替主 Agent 承担大文件检索、阅读和局部编辑。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from .cancellation import OperationCancelled
from .config import Settings
from .images import image_data_url, validate_image
from .llm import LLMClient
from .session import DiagramSession
from .skills import build_skills

logger = logging.getLogger(__name__)

MAX_SUBAGENT_TOOL_ITERATIONS = 8
MAX_SUBAGENT_RESULT_CHARS = 6000
SUBAGENT_TOOL_NAMES = {
    "read_document",
    "find_files",
    "grep_files",
    "write_file",
    "replace_in_file",
    "read_image",
    "ocr_image",
    "run_command",
}

SUBAGENT_SYSTEM = """你是主 Agent 启动的文件处理子 Agent，只完成收到的单个任务。
你可以查找、搜索、读取和编辑文件，但不能生成流程图、修改 Agent 配置、加载 Skill/Style，
也不能创建其他子 Agent。先用 find_files/grep_files 缩小范围，再读取必要文件；大文件只提炼
与任务相关的内容，避免在最终结果中复述全文。写文件前先读取并确认依据。
最终返回给主 Agent 的报告应简洁、可执行，必须包含关键发现、涉及路径、已做修改和未解决问题，
通常控制在 2000 个中文字符以内。"""


class _SubAgentImages:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.paths: list[Path] = []

    def add(self, path: str) -> str:
        if not self.enabled:
            return "错误：子 Agent 的主模型未开启图像输入。"
        try:
            image = validate_image(path)
        except ValueError as exc:
            return f"错误：{exc}"
        self.paths.append(image)
        return f"已读取图片 {image}，内容将在下一步对你可见。"

    def take(self) -> list[Path]:
        paths, self.paths = self.paths, []
        return paths


class FileSubAgent:
    """一个 MainAgent 只持有一个实例；同一时刻只执行一个文件任务。"""

    def __init__(
        self,
        settings: Settings,
        session: DiagramSession,
        *,
        readable_root: Path | None = None,
        readable_roots: list[Path] | tuple[Path, ...] | None = None,
        command_runner=None,
        should_cancel: Callable[[], bool] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self._llm = LLMClient(settings.text_model)
        self._vision = settings.text_model_vision
        self._images = _SubAgentImages(self._vision)
        ocr_llm = (
            None
            if self._vision or settings.vision_model is None
            else LLMClient(settings.vision_model)
        )
        skills = build_skills(
            session,
            self._images,
            ocr_llm=ocr_llm,
            command_runner=command_runner,
            readable_root=readable_root,
            readable_roots=readable_roots,
            should_cancel=should_cancel,
        )
        self._skills = {
            skill.name: skill
            for skill in skills
            if skill.name in SUBAGENT_TOOL_NAMES
            and (skill.name != "read_image" or self._vision)
        }
        self._tools = [skill.to_openai_tool() for skill in self._skills.values()]
        self._should_cancel = should_cancel
        self._on_event = on_event
        self._run_lock = threading.Lock()

    @property
    def tool_names(self) -> set[str]:
        return set(self._skills)

    def run(self, task: str) -> str:
        task = (task or "").strip()
        if not task:
            return "错误：子 Agent 任务不能为空。"
        if not self._run_lock.acquire(blocking=False):
            return "错误：已有一个子 Agent 正在工作，请等待其完成。"
        self._emit("started", task=task)
        try:
            messages = [
                {"role": "system", "content": SUBAGENT_SYSTEM},
                {"role": "user", "content": task},
            ]
            override = None
            for _ in range(MAX_SUBAGENT_TOOL_ITERATIONS):
                if self._cancelled():
                    return self._finish("子 Agent 已停止。", status="cancelled")
                request_messages = messages if override is None else messages[:-1] + [override]
                override = None
                msg = self._llm.chat_with_tools_stream(
                    request_messages,
                    self._tools,
                    on_delta=lambda text: self._emit("delta", text=text),
                    on_tick=lambda text: self._emit("usage", chars=len(text)),
                    on_reasoning=lambda text: self._emit("reasoning.delta", text=text),
                    should_cancel=self._should_cancel,
                )
                if not msg.tool_calls:
                    return self._finish(msg.content or "子 Agent 已完成，但没有返回摘要。")

                messages.append(self._assistant_message(msg))
                for call in msg.tool_calls:
                    name = call.function.name
                    self._emit("tool.started", name=name, arguments=call.function.arguments)
                    result = self._execute(name, call.function.arguments)
                    self._emit("tool.completed", name=name, result=result)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result}
                    )
                    if self._cancelled():
                        return self._finish("子 Agent 已停止。", status="cancelled")

                pending = self._images.take()
                if pending:
                    note = f"[系统] 以下是 read_image 读取的 {len(pending)} 张图片："
                    messages.append({"role": "user", "content": note})
                    override = {
                        "role": "user",
                        "content": [{"type": "text", "text": note}] + [
                            {
                                "type": "image_url",
                                "image_url": {"url": image_data_url(path)},
                            }
                            for path in pending
                        ],
                    }
            return self._finish("子 Agent 连续工具调用次数过多，已停止并交回主 Agent。", status="failed")
        except OperationCancelled:
            return self._finish("子 Agent 已停止。", status="cancelled")
        except Exception as exc:
            logger.exception("文件子 Agent 执行失败")
            self._emit("failed", error=str(exc))
            return f"错误：子 Agent 执行失败：{exc}"
        finally:
            self._run_lock.release()

    def _finish(self, result: str, status: str = "completed") -> str:
        if len(result) > MAX_SUBAGENT_RESULT_CHARS:
            result = (
                result[:MAX_SUBAGENT_RESULT_CHARS]
                + "\n…（子 Agent 结果过长，已截断以保护主 Agent 上下文）"
            )
        self._emit(status, result=result)
        return result

    def _cancelled(self) -> bool:
        return bool(self._should_cancel and self._should_cancel())

    def _emit(self, event: str, **data) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, data)
        except Exception:
            logger.exception("子 Agent 界面事件回调失败：%s", event)

    @staticmethod
    def _assistant_message(msg) -> dict:
        message = {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [call.model_dump() for call in msg.tool_calls],
        }
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            message["reasoning_content"] = reasoning
        return message

    def _execute(self, name: str, arguments_json: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return f"错误：子 Agent 无权使用工具 {name}"
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            return f"错误：工具参数不是合法 JSON：{arguments_json[:100]}"
        try:
            return skill.handler(**arguments)
        except OperationCancelled:
            raise
        except Exception as exc:
            logger.exception("子 Agent 工具 %s 执行异常", name)
            return f"错误：工具执行失败：{exc}"

"""OpenAI 兼容协议的 LLM 客户端封装，文本与多模态模型各持有一个实例。"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from openai import DefaultHttpxClient, OpenAI

from ..cancellation import (
    CancelCheck,
    OperationCancelled,
    raise_if_cancelled,
    watch_cancellation,
)
from ..config import ModelConfig
from ..images import image_data_url

logger = logging.getLogger(__name__)


def _log_request(stream: bool, **kwargs) -> None:
    """请求发出前的统一日志：模型、流式与否、消息规模、工具数、是否带图。"""
    messages = kwargs.get("messages") or []
    n_images = sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    )
    tools = kwargs.get("tools") or []
    logger.info(
        "[llm] 请求 model=%s 流式=%s 消息数=%d%s%s",
        kwargs.get("model"),
        stream,
        len(messages),
        f" 工具数={len(tools)}" if tools else "",
        f" 图片数={n_images}" if n_images else "",
    )


def _log_response(resp, t0: float) -> None:
    """响应收到后的统一日志：耗时、输出规模、工具调用数。"""
    msg = resp.choices[0].message
    n_calls = len(getattr(msg, "tool_calls", None) or [])
    logger.info(
        "[llm] 响应 耗时=%.1fs 输出=%d字符%s",
        time.monotonic() - t0,
        len(msg.content or ""),
        f" tool_calls={n_calls}" if n_calls else "",
    )


def _collect_stream(
    chunks,
    on_delta=None,
    on_tick=None,
    on_reasoning=None,
    t0=None,
    should_cancel: CancelCheck = None,
) -> SimpleNamespace:
    """把流式响应收干并拼成与非流式一致的结构（choices[0].message）。

    on_delta 不为 None 时，每收到一段文本增量就回调一次（用于界面实时显示）。
    on_tick 不为 None 时，每收到一段 tool_calls 参数增量就回调一次（界面
    用来估算 token 用量；正文增量由 on_delta 覆盖，此处不重复计）。
    on_reasoning 不为 None 时，每收到一段 reasoning_content（推理模型的
    思考流）就回调一次；思考内容同时收齐挂在返回消息的
    reasoning_content 上（思考模式 + tool_calls 的网关要求历史消息回传），
    并用于界面提示与用量估算。
    t0 为请求发出的 monotonic 时刻，用于记录首 chunk 耗时（TTFT）。
    tool_calls 条目带 model_dump()，与 openai SDK 的 pydantic 对象用法兼容。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, SimpleNamespace] = {}
    first_chunk_at: float | None = None
    reasoning_chars = 0
    iterator = iter(chunks)
    while True:
        raise_if_cancelled(should_cancel)
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        raise_if_cancelled(should_cancel)
        if first_chunk_at is None:
            first_chunk_at = time.monotonic()
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None):
            reasoning_parts.append(delta.reasoning_content)
            reasoning_chars += len(delta.reasoning_content)
            if on_reasoning:
                on_reasoning(delta.reasoning_content)
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
            if on_delta:
                on_delta(delta.content)
        for tc in getattr(delta, "tool_calls", None) or []:
            slot = tool_calls.setdefault(
                tc.index,
                SimpleNamespace(
                    id=None,
                    type="function",
                    function=SimpleNamespace(name="", arguments=""),
                ),
            )
            if tc.id:
                slot.id = tc.id
            if tc.function:
                if tc.function.name:
                    slot.function.name += tc.function.name
                if tc.function.arguments:
                    slot.function.arguments += tc.function.arguments
                    if on_tick:
                        on_tick(tc.function.arguments)
    for slot in tool_calls.values():
        payload = {
            "id": slot.id,
            "type": "function",
            "function": {
                "name": slot.function.name,
                "arguments": slot.function.arguments,
            },
        }
        slot.model_dump = lambda p=payload: p
    if t0 is not None and first_chunk_at is not None:
        logger.info(
            "[llm] 首 chunk 耗时 %.1fs（TTFT），reasoning 共 %d 字符",
            first_chunk_at - t0, reasoning_chars,
        )
    message = SimpleNamespace(
        role="assistant",
        content="".join(content_parts) or None,
        tool_calls=list(tool_calls.values()) or None,
        # 思考模式 + tool_calls 的网关（如 deepseek）要求历史消息原样回传
        # reasoning_content，否则下一轮请求 400；此处收齐供调用方带回去
        reasoning_content="".join(reasoning_parts) or None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class LLMClient:
    def __init__(self, model: ModelConfig):
        self._model = model
        self._client: OpenAI | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> OpenAI:
        with self._client_lock:
            if self._client is None:
                self._client = OpenAI(
                    api_key=self._model.api_key,
                    base_url=self._model.base_url,
                    # 公司桌面通常注册了系统代理，但模型网关往往位于内网。
                    # 默认明确直连；只有 MODEL_PROXY 显式配置时才使用代理。
                    http_client=DefaultHttpxClient(
                        proxy=self._model.proxy,
                        trust_env=False,
                    ),
                )
            return self._client

    def _abort_client(self, client: OpenAI) -> None:
        """Close only the client used by the cancelled request; recreate lazily."""
        try:
            client.close()
        finally:
            with self._client_lock:
                if self._client is client:
                    self._client = None

    @property
    def model_name(self) -> str:
        return self._model.name

    def _completion(self, should_cancel: CancelCheck = None, **kwargs):
        """统一请求入口：强制非流式。

        个别网关在服务端默认流式、且无视 stream=false 时，SDK 会返回一个
        chunk 迭代器而非 ChatCompletion；此处兜底收流拼接，调用方无感。
        """
        _log_request(stream=False, **kwargs)
        t0 = time.monotonic()
        client = self._get_client()
        try:
            with watch_cancellation(
                should_cancel, lambda: self._abort_client(client)
            ):
                resp = client.chat.completions.create(stream=False, **kwargs)
        except Exception as exc:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("用户已停止模型请求") from exc
            raise
        raise_if_cancelled(should_cancel)
        if not hasattr(resp, "choices"):  # 实际返回了流式迭代器
            logger.warning("服务端无视 stream=false 返回了流式响应，已自动收流拼接")
            resp = _collect_stream(resp, should_cancel=should_cancel)
        _log_response(resp, t0)
        return resp

    def _stream_or_fallback(
        self,
        on_delta=None,
        on_tick=None,
        on_reasoning=None,
        should_cancel: CancelCheck = None,
        **kwargs,
    ):
        """流式请求入口：文本增量经 on_delta 实时回调，返回结构与 _completion 一致。

        服务商不支持流式（建连即报错）或流传输中途失败时，自动退回强制
        非流式重试；若失败前尚未吐出任何文本，把完整内容补回调一次，
        保证界面最终能看到完整结果（已吐过部分则由调用方以返回值收尾）。
        on_tick 仅用于 tool_calls 参数增量（含退回非流式后的一次性补报）。
        on_reasoning 接收推理模型的思考流增量（仅界面提示用）。
        """
        emitted = False

        def _track(text: str) -> None:
            nonlocal emitted
            emitted = True
            on_delta(text)

        _log_request(stream=True, **kwargs)
        t0 = time.monotonic()
        client = self._get_client()
        try:
            with watch_cancellation(
                should_cancel, lambda: self._abort_client(client)
            ):
                stream = client.chat.completions.create(stream=True, **kwargs)
                try:
                    resp = _collect_stream(
                        stream,
                        on_delta=_track if on_delta else None,
                        on_tick=on_tick,
                        on_reasoning=on_reasoning,
                        t0=t0,
                        should_cancel=should_cancel,
                    )
                finally:
                    close = getattr(stream, "close", None)
                    if close is not None:
                        close()
            _log_response(resp, t0)
            return resp
        except OperationCancelled:
            raise
        except Exception as e:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("用户已停止模型请求") from e
            logger.warning("流式请求失败，退回非流式：%s", e)
            resp = self._completion(should_cancel=should_cancel, **kwargs)
            msg = resp.choices[0].message
            if on_delta and not emitted:
                content = msg.content
                if content:
                    on_delta(content)
            if on_tick:
                for tc in getattr(msg, "tool_calls", None) or []:
                    args = getattr(tc.function, "arguments", None)
                    if args:
                        on_tick(args)
            return resp

    def chat(self, messages: list[dict], should_cancel: CancelCheck = None) -> str:
        """纯文本对话，返回 assistant 内容。"""
        resp = self._completion(
            model=self._model.name, messages=messages, should_cancel=should_cancel
        )
        return resp.choices[0].message.content or ""

    def chat_stream(
        self, messages: list[dict], on_delta, on_reasoning=None,
        should_cancel: CancelCheck = None,
    ) -> str:
        """流式纯文本对话：on_delta 收到文本增量，返回完整 assistant 内容。"""
        resp = self._stream_or_fallback(
            on_delta=on_delta, on_reasoning=on_reasoning,
            should_cancel=should_cancel,
            model=self._model.name, messages=messages
        )
        return resp.choices[0].message.content or ""

    def chat_with_tools(
        self, messages: list[dict], tools: list[dict], tool_choice=None,
        should_cancel: CancelCheck = None,
    ):
        """带 function calling 的对话，返回完整的 assistant message 对象。

        调用方需检查 message.tool_calls 决定是否执行工具并继续对话。
        tool_choice：强制工具调用（如 "required"），None 由服务端默认。
        """
        kwargs = dict(model=self._model.name, messages=messages, tools=tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        resp = self._completion(should_cancel=should_cancel, **kwargs)
        return resp.choices[0].message

    def chat_with_tools_stream(
        self, messages: list[dict], tools: list[dict], on_delta,
        on_tick=None, on_reasoning=None, should_cancel: CancelCheck = None,
    ):
        """chat_with_tools 的流式版本：文本增量经 on_delta 回调，返回结构一致。

        on_tick 接收 tool_calls 参数增量（界面估算 token 用量用）。
        on_reasoning 接收推理模型的思考流增量（仅界面提示用）。
        """
        resp = self._stream_or_fallback(
            on_delta=on_delta, on_tick=on_tick, on_reasoning=on_reasoning,
            should_cancel=should_cancel,
            model=self._model.name, messages=messages, tools=tools
        )
        return resp.choices[0].message

    @staticmethod
    def with_images(messages: list[dict], image_paths: list[str | Path]) -> list[dict]:
        """把图片以 base64 data URL 附加到最后一条消息，返回新列表。"""
        msgs = [dict(m) for m in messages]
        last = dict(msgs[-1])
        last["content"] = [{"type": "text", "text": last["content"]}] + [
            {"type": "image_url", "image_url": {"url": image_data_url(p)}}
            for p in image_paths
        ]
        msgs[-1] = last
        return msgs

    def chat_with_images(
        self, messages: list[dict], image_paths: list[str | Path],
        should_cancel: CancelCheck = None,
    ) -> str:
        """把图片附加到最后一条消息后对话（messages 中通常含 system + user）。"""
        return self.chat(
            self.with_images(messages, image_paths), should_cancel=should_cancel
        )

    def chat_with_images_stream(
        self,
        messages: list[dict],
        image_paths: list[str | Path],
        on_delta,
        on_reasoning=None,
        should_cancel: CancelCheck = None,
    ) -> str:
        """流式带图对话；正文增量用于长时间视觉任务的进度展示。"""
        return self.chat_stream(
            self.with_images(messages, image_paths),
            on_delta=on_delta,
            on_reasoning=on_reasoning,
            should_cancel=should_cancel,
        )

    def chat_with_image(
        self, prompt: str, image_path: str | Path,
        should_cancel: CancelCheck = None,
    ) -> str:
        """带图对话：把本地图片以 base64 data URL 随 prompt 一起发送。"""
        return self.chat_with_images(
            [{"role": "user", "content": prompt}], [image_path],
            should_cancel=should_cancel,
        )

    def chat_with_image_stream(
        self,
        prompt: str,
        image_path: str | Path,
        on_delta,
        on_reasoning=None,
        should_cancel: CancelCheck = None,
    ) -> str:
        return self.chat_with_images_stream(
            [{"role": "user", "content": prompt}],
            [image_path],
            on_delta=on_delta,
            on_reasoning=on_reasoning,
            should_cancel=should_cancel,
        )

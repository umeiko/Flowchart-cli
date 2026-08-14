"""OpenAI 兼容协议的 LLM 客户端封装，文本与多模态模型各持有一个实例。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAI

from ..config import ModelConfig
from ..images import image_data_url

logger = logging.getLogger(__name__)


def _collect_stream(chunks, on_delta=None) -> SimpleNamespace:
    """把流式响应收干并拼成与非流式一致的结构（choices[0].message）。

    on_delta 不为 None 时，每收到一段文本增量就回调一次（用于界面实时显示）。
    tool_calls 条目带 model_dump()，与 openai SDK 的 pydantic 对象用法兼容。
    """
    content_parts: list[str] = []
    tool_calls: dict[int, SimpleNamespace] = {}
    for chunk in chunks:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
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
    message = SimpleNamespace(
        role="assistant",
        content="".join(content_parts) or None,
        tool_calls=list(tool_calls.values()) or None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class LLMClient:
    def __init__(self, model: ModelConfig):
        self._model = model
        self._client = OpenAI(api_key=model.api_key, base_url=model.base_url)

    @property
    def model_name(self) -> str:
        return self._model.name

    def _completion(self, **kwargs):
        """统一请求入口：强制非流式。

        个别网关在服务端默认流式、且无视 stream=false 时，SDK 会返回一个
        chunk 迭代器而非 ChatCompletion；此处兜底收流拼接，调用方无感。
        """
        resp = self._client.chat.completions.create(stream=False, **kwargs)
        if not hasattr(resp, "choices"):  # 实际返回了流式迭代器
            return _collect_stream(resp)
        return resp

    def _stream_or_fallback(self, on_delta=None, **kwargs):
        """流式请求入口：文本增量经 on_delta 实时回调，返回结构与 _completion 一致。

        服务商不支持流式（建连即报错）或流传输中途失败时，自动退回强制
        非流式重试；若失败前尚未吐出任何文本，把完整内容补回调一次，
        保证界面最终能看到完整结果（已吐过部分则由调用方以返回值收尾）。
        """
        emitted = False

        def _track(text: str) -> None:
            nonlocal emitted
            emitted = True
            on_delta(text)

        try:
            stream = self._client.chat.completions.create(stream=True, **kwargs)
            return _collect_stream(stream, on_delta=_track if on_delta else None)
        except Exception as e:
            logger.warning("流式请求失败，退回非流式：%s", e)
            resp = self._completion(**kwargs)
            if on_delta and not emitted:
                content = resp.choices[0].message.content
                if content:
                    on_delta(content)
            return resp

    def chat(self, messages: list[dict]) -> str:
        """纯文本对话，返回 assistant 内容。"""
        resp = self._completion(model=self._model.name, messages=messages)
        return resp.choices[0].message.content or ""

    def chat_stream(self, messages: list[dict], on_delta) -> str:
        """流式纯文本对话：on_delta 收到文本增量，返回完整 assistant 内容。"""
        resp = self._stream_or_fallback(
            on_delta=on_delta, model=self._model.name, messages=messages
        )
        return resp.choices[0].message.content or ""

    def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        """带 function calling 的对话，返回完整的 assistant message 对象。

        调用方需检查 message.tool_calls 决定是否执行工具并继续对话。
        """
        resp = self._completion(
            model=self._model.name,
            messages=messages,
            tools=tools,
        )
        return resp.choices[0].message

    def chat_with_tools_stream(self, messages: list[dict], tools: list[dict], on_delta):
        """chat_with_tools 的流式版本：文本增量经 on_delta 回调，返回结构一致。"""
        resp = self._stream_or_fallback(
            on_delta=on_delta, model=self._model.name, messages=messages, tools=tools
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

    def chat_with_images(self, messages: list[dict], image_paths: list[str | Path]) -> str:
        """把图片附加到最后一条消息后对话（messages 中通常含 system + user）。"""
        return self.chat(self.with_images(messages, image_paths))

    def chat_with_image(self, prompt: str, image_path: str | Path) -> str:
        """带图对话：把本地图片以 base64 data URL 随 prompt 一起发送。"""
        return self.chat_with_images(
            [{"role": "user", "content": prompt}], [image_path]
        )

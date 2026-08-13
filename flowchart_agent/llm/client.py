"""OpenAI 兼容协议的 LLM 客户端封装，文本与多模态模型各持有一个实例。"""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from ..config import ModelConfig
from ..images import image_data_url


class LLMClient:
    def __init__(self, model: ModelConfig):
        self._model = model
        self._client = OpenAI(api_key=model.api_key, base_url=model.base_url)

    @property
    def model_name(self) -> str:
        return self._model.name

    def chat(self, messages: list[dict]) -> str:
        """纯文本对话，返回 assistant 内容。"""
        resp = self._client.chat.completions.create(
            model=self._model.name,
            messages=messages,
        )
        return resp.choices[0].message.content or ""

    def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        """带 function calling 的对话，返回完整的 assistant message 对象。

        调用方需检查 message.tool_calls 决定是否执行工具并继续对话。
        """
        resp = self._client.chat.completions.create(
            model=self._model.name,
            messages=messages,
            tools=tools,
        )
        return resp.choices[0].message

    def chat_with_images(self, messages: list[dict], image_paths: list[str | Path]) -> str:
        """把图片附加到最后一条消息后对话（messages 中通常含 system + user）。"""
        msgs = [dict(m) for m in messages]
        last = dict(msgs[-1])
        last["content"] = [{"type": "text", "text": last["content"]}] + [
            {"type": "image_url", "image_url": {"url": image_data_url(p)}}
            for p in image_paths
        ]
        msgs[-1] = last
        return self.chat(msgs)

    def chat_with_image(self, prompt: str, image_path: str | Path) -> str:
        """带图对话：把本地图片以 base64 data URL 随 prompt 一起发送。"""
        return self.chat_with_images(
            [{"role": "user", "content": prompt}], [image_path]
        )

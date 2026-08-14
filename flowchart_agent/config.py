"""配置加载：dotenv 读取文本模型与多模态模型的独立 API 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class ModelConfig:
    """单个模型的 OpenAI 兼容 API 配置。"""

    name: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class Settings:
    text_model: ModelConfig
    vision_model: ModelConfig
    text_model_vision: bool = False  # 文本（生成）模型是否具备原生多模态能力
    max_rounds: int = 5
    output_format: str = "png"
    render_background: str = "white"  # 画布背景色（mmdc -b），默认白色不用透明
    # mmdc 渲染使用的 Chrome 可执行文件路径（puppeteer executablePath）。
    # 公司 Windows 等环境 puppeteer 自带 Chromium 不可用时，指向本机 chrome.exe。
    chrome_path: str | None = None


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {key}，请复制 .env.example 为 .env 并填写配置。"
        )
    return value


def load_settings(env_path: str | Path | None = None) -> Settings:
    load_dotenv(env_path or ".env")
    return Settings(
        text_model=ModelConfig(
            name=_require("TEXT_MODEL_NAME"),
            api_key=_require("TEXT_MODEL_API_KEY"),
            base_url=_require("TEXT_MODEL_BASE_URL"),
        ),
        vision_model=ModelConfig(
            name=_require("VISION_MODEL_NAME"),
            api_key=_require("VISION_MODEL_API_KEY"),
            base_url=_require("VISION_MODEL_BASE_URL"),
        ),
        max_rounds=int(os.getenv("MAX_ROUNDS", "5")),
        output_format=os.getenv("OUTPUT_FORMAT", "png"),
        render_background=os.getenv("RENDER_BACKGROUND", "white"),
        chrome_path=os.getenv("CHROME_PATH") or None,
        text_model_vision=os.getenv("TEXT_MODEL_VISION", "").lower()
        in ("1", "true", "yes"),
    )

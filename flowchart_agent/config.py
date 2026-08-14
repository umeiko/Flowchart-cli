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
    # 视觉检视强度：full=完整检视（排版+内容语义），layout=仅基础图形检视
    # （排版/遮挡/连线，不逐字核对内容；视觉模型识字能力弱时用，防止误判死循环）
    verify_mode: str = "full"
    # PNG 渲染缩放倍数（mmdc -s），大图默认分辨率低、文字看不清时提高它
    render_scale: str = "2"
    # PNG 视口宽度（mmdc -w）。mermaid 会把图整体压缩进视口宽度（默认 800），
    # 宽图文字会被压到看不清；调大后按自然尺寸渲染，小图不受影响
    render_width: str = "4096"


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
        verify_mode=(os.getenv("VERIFY_MODE") or "full").lower(),
        render_scale=os.getenv("RENDER_SCALE") or "2",
        render_width=os.getenv("RENDER_WIDTH") or "4096",
        text_model_vision=os.getenv("TEXT_MODEL_VISION", "").lower()
        in ("1", "true", "yes"),
    )

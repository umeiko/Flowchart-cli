"""配置加载：dotenv 读取文本模型与多模态模型的独立 API 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import runtime


@dataclass(frozen=True)
class ModelConfig:
    """单个模型的 OpenAI 兼容 API 配置。"""

    name: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class Settings:
    text_model: ModelConfig
    # 主 Agent 上下文窗口，用于 UI 占用估算与压缩提示。不同兼容端点无法统一
    # 返回精确 tokenizer 结果，因此这里只作为容量参考，不参与供应商计费。
    context_window: int = 128000
    # 多模态（视觉）模型；None = 未配置——视觉检视自动降级为代码检视（VERIFY_MODE=code），
    # ocr_image 工具不可用
    vision_model: ModelConfig | None = None
    text_model_vision: bool = False  # 文本（生成）模型是否具备原生多模态能力
    max_rounds: int = 5
    # 文件子 Agent 允许的 function-calling 回合数；一回合可包含多个工具调用。
    # 图片质检在部分兼容端点上会退化为每回合只调用一个工具，因此默认高于主 Agent。
    max_subagent_tool_iterations: int = 24
    output_format: str = "png"
    render_background: str = "white"  # 画布背景色（mmdc -b），默认白色不用透明
    # mmdc 渲染使用的 Chrome 可执行文件路径（puppeteer executablePath）。
    # 公司 Windows 等环境 puppeteer 自带 Chromium 不可用时，指向本机 chrome.exe。
    chrome_path: str | None = None
    # draw.io 桌面版可执行文件路径（drawio 子 Agent 的本地渲染器，
    # 用 draw.io -x -f png 把 .drawio 导出为 PNG 供检视闭环使用）
    drawio_path: str | None = None
    # 出图引擎：mermaid（默认，Mermaid 代码 → mmdc 渲染）或
    # drawio（LLM 直接生成 draw.io 原生 XML，draw.io 桌面版渲染，
    # 产物可直接拖进 draw.io/Visio/亿图编辑）
    output_engine: str = "mermaid"
    # 视觉检视强度：full=完整检视（排版+内容语义），layout=仅基础图形检视
    # （排版/遮挡/连线，不逐字核对内容；视觉模型识字能力弱时用，防止误判死循环），
    # code=代码检视（完全不依赖视觉模型，文本模型直接审查 Mermaid 源码，最兜底）
    verify_mode: str = "full"
    # PNG 渲染缩放倍数（mmdc -s），大图默认分辨率低、文字看不清时提高它
    render_scale: str = "2"
    # PNG 视口宽度（mmdc -w）：auto=先渲 SVG 探测图的自然宽度再按
    # min(自然宽度, 4096) 渲染——甘特图等自适应视口的图型按自然比例渲染，
    # 超宽流程图不被 mmdc 默认 800 视口压扁；填数字则固定视口宽度（旧行为）
    render_width: str = "auto"
    # drawio 引擎的字体覆盖（确定性后处理注入到每个 cell 的 style，
    # 不信任 LLM 照抄骨架）：None = 不注入，保持骨架默认。
    # 字体来源：系统已安装字体 + 自带 Fonts/ 目录（cli 启动时按当前用户
    # 自动安装，见 fonts.py）；都没装时 draw.io 静默回退默认字体
    drawio_font_family: str | None = None
    drawio_font_size: str | None = None


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {key}，请复制 .env.example 为 .env 并填写配置。"
        )
    return value


def _load_vision_model() -> ModelConfig | None:
    """视觉模型可选：三项配置齐全才加载，否则返回 None（检视降级为 code 模式）。"""
    name = os.getenv("VISION_MODEL_NAME")
    api_key = os.getenv("VISION_MODEL_API_KEY")
    base_url = os.getenv("VISION_MODEL_BASE_URL")
    if name and api_key and base_url:
        return ModelConfig(name=name, api_key=api_key, base_url=base_url)
    return None


def load_settings(env_path: str | Path | None = None) -> Settings:
    if env_path is None:
        # 冻结（离线包）时优先读 exe 旁边的 .env，其次 CWD；源码运行维持 ./.env
        candidates = (
            [runtime.app_dir() / ".env", Path(".env")]
            if runtime.is_frozen()
            else [Path(".env")]
        )
        env_path = next((p for p in candidates if p.is_file()), candidates[-1])
    load_dotenv(env_path)
    return Settings(
        text_model=ModelConfig(
            name=_require("TEXT_MODEL_NAME"),
            api_key=_require("TEXT_MODEL_API_KEY"),
            base_url=_require("TEXT_MODEL_BASE_URL"),
        ),
        context_window=max(1, int(os.getenv("TEXT_MODEL_CONTEXT_WINDOW", "128000"))),
        vision_model=_load_vision_model(),
        max_rounds=int(os.getenv("MAX_ROUNDS", "5")),
        max_subagent_tool_iterations=max(
            1, int(os.getenv("MAX_SUBAGENT_TOOL_ITERATIONS", "24"))
        ),
        output_format=os.getenv("OUTPUT_FORMAT", "png"),
        render_background=os.getenv("RENDER_BACKGROUND", "white"),
        chrome_path=os.getenv("CHROME_PATH") or None,
        drawio_path=os.getenv("DRAWIO_PATH") or None,
        output_engine=(os.getenv("OUTPUT_ENGINE") or "mermaid").strip().lower(),
        verify_mode=(os.getenv("VERIFY_MODE") or "full").lower(),
        render_scale=os.getenv("RENDER_SCALE") or "2",
        render_width=os.getenv("RENDER_WIDTH") or "auto",
        text_model_vision=os.getenv("TEXT_MODEL_VISION", "").lower()
        in ("1", "true", "yes"),
        drawio_font_family=os.getenv("DRAWIO_FONT_FAMILY") or None,
        drawio_font_size=os.getenv("DRAWIO_FONT_SIZE") or None,
    )

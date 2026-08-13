"""图片处理公共模块：校验、base64 data URL 编码。"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # base64 编码后约 13MB，超出容易触发 API 限制


def validate_image(path: str | Path) -> Path:
    """校验图片文件可用性，不可用抛出带中文原因的 ValueError。"""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在：{path}")
    if p.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"不是支持的图片格式（{p.suffix}）：{path}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过 10MB：{path}")
    return p


def image_data_url(path: str | Path) -> str:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"

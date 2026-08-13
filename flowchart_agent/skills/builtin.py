"""内置 Skill：文件读取/查找、读图、流程图创建/修改/查看。"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Protocol

from ..session import DiagramSession
from .base import Skill

_MAX_DOC_BYTES = 200 * 1024
_MAX_FIND_RESULTS = 20
_MAX_FIND_WALK = 5000  # 最多遍历的文件数，防止在大目录里卡死
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


class ImageQueue(Protocol):
    """read_image 写入、主 Agent 读取的图片队列（见 main_agent）。"""

    def add(self, path: str) -> str: ...


def read_document(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        # 给出同目录下的相似文件名候选，让主 Agent 可以自行纠正路径重试
        hint = ""
        if p.parent.is_dir():
            names = [f.name for f in p.parent.iterdir() if f.is_file()]
            close = difflib.get_close_matches(p.name, names, n=3, cutoff=0.3)
            if close:
                hint = "。你是不是想找：" + "、".join(str(p.parent / c) for c in close)
        return f"错误：文件不存在：{path}{hint}"
    if p.stat().st_size > _MAX_DOC_BYTES:
        return f"错误：文件超过 200KB，请精简后再试：{path}"
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"错误：不是 UTF-8 文本文件：{path}"


def find_files(keyword: str, directory: str = ".") -> str:
    root = Path(directory)
    if not root.is_dir():
        return f"错误：目录不存在：{directory}"
    matches: list[str] = []
    walked = 0
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS or part.startswith(".") for part in p.parts):
            continue
        if p.is_file():
            walked += 1
            if keyword.lower() in p.name.lower():
                matches.append(str(p))
            if len(matches) >= _MAX_FIND_RESULTS or walked >= _MAX_FIND_WALK:
                break
    if not matches:
        return f"没有找到文件名包含 {keyword!r} 的文件（搜索范围：{root.resolve()}）"
    return "找到以下文件：\n" + "\n".join(matches)


def build_skills(session: DiagramSession, image_queue: ImageQueue) -> list[Skill]:
    return [
        Skill(
            name="read_document",
            description="读取本地需求文档（.txt/.md 等 UTF-8 文本文件），返回全文内容。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文档文件路径"},
                },
                "required": ["path"],
            },
            handler=read_document,
        ),
        Skill(
            name="find_files",
            description=(
                "按文件名关键词在目录中模糊查找文件，用于用户给出的路径有误时"
                "自行猜测正确文件。返回匹配的文件路径列表。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名关键词，如 1.txt 或 登录"},
                    "directory": {
                        "type": "string",
                        "description": "搜索的起始目录，默认当前目录",
                        "default": ".",
                    },
                },
                "required": ["keyword"],
            },
            handler=find_files,
        ),
        Skill(
            name="read_image",
            description=(
                "查看一张本地图片（手绘草图、现有流程图截图等），"
                "用于理解用户的图片需求。调用后图片内容将对你可见。"
                "仅在主模型具备多模态能力时有效。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "图片文件路径"},
                },
                "required": ["path"],
            },
            handler=image_queue.add,
        ),
        Skill(
            name="create_diagram",
            description=(
                "根据完整的需求描述创建一张新的流程图（内部会自动完成"
                "生成→渲染校验→视觉验证的循环）。仅在用户提出新图需求时调用。"
                "若用户提供了参考图片路径，通过 image_path 传入；"
                "若用户有风格倾向，先 list_styles 发现可用模板并经 style 传入；"
                "若用户明确要求画布背景颜色，通过 background 传入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "requirement": {
                        "type": "string",
                        "description": "完整的流程需求描述（可来自文档或用户口述）",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "可选：参考图片路径（草图/现有流程图截图）",
                    },
                    "style": {
                        "type": "string",
                        "description": "可选：风格模板名（来自 list_styles），如 dark",
                    },
                    "background": {
                        "type": "string",
                        "description": "可选：画布背景色，如 white、#1e1e1e；仅在用户明确要求时设置",
                    },
                },
                "required": ["requirement"],
            },
            handler=session.create,
        ),
        Skill(
            name="list_styles",
            description=(
                "列出 styles/ 目录下所有可用的作图风格模板（名称与适用场景）。"
                "用户提出风格相关需求时先调用本工具发现模板。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=session.list_styles,
        ),
        Skill(
            name="set_style",
            description=(
                "切换当前作图风格模板（作用于后续生成与修改）。"
                "风格名须来自 list_styles。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "风格模板名，如 dark"},
                },
                "required": ["name"],
            },
            handler=session.set_style,
        ),
        Skill(
            name="modify_diagram",
            description=(
                "按用户的修改意见调整当前流程图（内部同样会渲染校验并视觉验证）。"
                "仅在已有图且用户提出修改时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "用户的修改意见，如：把登录改为验证码登录",
                    },
                },
                "required": ["instruction"],
            },
            handler=session.modify,
        ),
        Skill(
            name="get_current_diagram",
            description="查看当前流程图的 Mermaid 代码与产物路径。",
            parameters={"type": "object", "properties": {}},
            handler=lambda: (
                f"当前 Mermaid 代码：\n```mermaid\n{session.current_code}\n```\n"
                f"图片：{session.current_image}"
                if session.has_diagram
                else "当前还没有流程图。"
            ),
        ),
    ]

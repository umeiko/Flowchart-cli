"""内置 Skill：文件读取/查找/写入/替换/搜索、读图/OCR、工作文档、流程图创建/修改/查看。"""

from __future__ import annotations

import difflib
import re
from functools import partial
from pathlib import Path
from typing import Iterable, Protocol

from ..images import validate_image
from ..llm import LLMClient
from ..prompts import OCR_PROMPT
from ..session import DiagramSession
from .base import Skill

_MAX_DOC_BYTES = 200 * 1024
_MAX_FIND_RESULTS = 20
_MAX_GREP_RESULTS = 50
_MAX_FIND_WALK = 5000  # 最多遍历的文件数，防止在大目录里卡死
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def resolve_readable_path(
    path: str | Path,
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
    *,
    allow_root: bool = False,
) -> Path:
    """Resolve a read path and enforce an optional sandbox boundary.

    CLI callers keep the historical unrestricted behavior by omitting ``root``.
    Server callers pass the current Session root plus its user-visible subdirs.
    """
    raw = Path(path)
    if root is None:
        return raw.resolve()
    boundary = Path(root).resolve()
    candidate = (raw if raw.is_absolute() else boundary / raw).resolve()
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise ValueError("只允许读取当前 Session 内的文件") from exc
    allowed = tuple(Path(item).resolve() for item in (readable_roots or (boundary,)))
    if candidate == boundary and allow_root:
        return candidate
    if not any(candidate == item or item in candidate.parents for item in allowed):
        raise ValueError("只允许读取当前 Session 的工作区、附件和产物目录")
    return candidate


def _search_roots(
    directory: str,
    root: Path | None,
    readable_roots: Iterable[Path] | None,
) -> list[Path]:
    search_root = resolve_readable_path(
        directory, root, readable_roots, allow_root=True
    )
    if root is not None and search_root == Path(root).resolve() and readable_roots:
        return [Path(item).resolve() for item in readable_roots if Path(item).is_dir()]
    return [search_root]


def _valid_file_glob(file_glob: str) -> bool:
    normalized = file_glob.replace("\\", "/")
    return (
        not Path(file_glob).is_absolute()
        and not normalized.startswith("/")
        and ".." not in normalized.split("/")
    )


def _writable_path(path: str, root: Path) -> Path:
    """写操作安全边界：只允许写产物目录内的文件；相对路径按产物目录解析。"""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"只允许写入产物目录（{root}）内的文件：{path}")
    return p


def write_file(path: str, content: str, root: Path) -> str:
    try:
        p = _writable_path(path, root)
    except ValueError as e:
        return f"错误：{e}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"文件已写入（{len(content)} 字符）：{p}"


def replace_in_file(path: str, old_text: str, new_text: str, root: Path,
                    replace_all: bool = False) -> str:
    try:
        p = _writable_path(path, root)
    except ValueError as e:
        return f"错误：{e}"
    if not p.is_file():
        return f"错误：文件不存在：{path}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        return f"错误：在 {path} 中未找到要替换的文本（注意需与文件内容完全一致）"
    if count > 1 and not replace_all:
        return (
            f"错误：要替换的文本在 {path} 中出现 {count} 处，请提供更长的上下文"
            "保证唯一匹配，或确认要全部替换（replace_all=true）。"
        )
    text = text.replace(old_text, new_text) if replace_all \
        else text.replace(old_text, new_text, 1)
    p.write_text(text, encoding="utf-8")
    return f"已在 {p} 中完成 {count if replace_all else 1} 处替换。"


def grep_files(
    pattern: str,
    directory: str = ".",
    file_glob: str = "",
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"错误：正则表达式不合法：{e}"
    if file_glob and not _valid_file_glob(file_glob):
        return "错误：文件名过滤条件不能越出当前 Session"
    try:
        roots = _search_roots(directory, root, readable_roots)
    except ValueError as e:
        return f"错误：{e}"
    if not all(search_root.is_dir() for search_root in roots):
        return f"错误：目录不存在：{directory}"
    results: list[str] = []
    walked = 0
    for search_root in roots:
        for p in search_root.rglob(file_glob or "*"):
            relative_parts = p.relative_to(search_root).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in relative_parts):
                continue
            if not p.is_file():
                continue
            walked += 1
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # 跳过二进制/非 UTF-8 文件
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    results.append(f"{p}:{i}: {line.strip()[:150]}")
                    if len(results) >= _MAX_GREP_RESULTS:
                        break
            if len(results) >= _MAX_GREP_RESULTS or walked >= _MAX_FIND_WALK:
                break
        if len(results) >= _MAX_GREP_RESULTS or walked >= _MAX_FIND_WALK:
            break
    if not results:
        scope = "、".join(str(item) for item in roots)
        return f"没有匹配 {pattern!r} 的内容（搜索范围：{scope}）"
    suffix = f"（已达 {_MAX_GREP_RESULTS} 条上限）" if len(results) >= _MAX_GREP_RESULTS else ""
    return f"找到 {len(results)} 处匹配{suffix}：\n" + "\n".join(results)


class ImageQueue(Protocol):
    """read_image 写入、主 Agent 读取的图片队列（见 main_agent）。"""

    def add(self, path: str) -> str: ...


class CommandRunner(Protocol):
    """run_command 工具的执行后端（由界面层注入，见 chat_cli）。

    负责：红框展示命令、用户确认（或 yolo 直通）、执行并捕获输出、
    Ctrl+C 杀进程。返回给模型的文本（输出/错误/被拒说明）。
    """

    def run(self, command: str) -> str: ...


def read_document(
    path: str,
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
) -> str:
    try:
        p = resolve_readable_path(path, root, readable_roots)
    except ValueError as e:
        return f"错误：{e}"
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


def find_files(
    keyword: str,
    directory: str = ".",
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
) -> str:
    try:
        roots = _search_roots(directory, root, readable_roots)
    except ValueError as e:
        return f"错误：{e}"
    if not all(search_root.is_dir() for search_root in roots):
        return f"错误：目录不存在：{directory}"
    matches: list[str] = []
    walked = 0
    for search_root in roots:
        for p in search_root.rglob("*"):
            relative_parts = p.relative_to(search_root).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in relative_parts):
                continue
            if p.is_file():
                walked += 1
                if keyword.lower() in p.name.lower():
                    matches.append(str(p))
                if len(matches) >= _MAX_FIND_RESULTS or walked >= _MAX_FIND_WALK:
                    break
        if len(matches) >= _MAX_FIND_RESULTS or walked >= _MAX_FIND_WALK:
            break
    if not matches:
        scope = "、".join(str(item) for item in roots)
        return f"没有找到文件名包含 {keyword!r} 的文件（搜索范围：{scope}）"
    return "找到以下文件：\n" + "\n".join(matches)


def _make_ocr_handler(
    llm: LLMClient,
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
):
    """ocr_image 工具的 handler：用多模态验证模型从图片提取文字。"""

    def ocr_image(path: str) -> str:
        try:
            p = validate_image(resolve_readable_path(path, root, readable_roots))
        except ValueError as e:
            return f"错误：{e}"
        return llm.chat_with_image(OCR_PROMPT, p)

    return ocr_image


def _make_read_image_handler(
    image_queue: ImageQueue,
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
):
    def read_image(path: str) -> str:
        try:
            safe_path = resolve_readable_path(path, root, readable_roots)
        except ValueError as e:
            return f"错误：{e}"
        return image_queue.add(str(safe_path))

    return read_image


def _make_create_handler(
    session: DiagramSession,
    root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
):
    def create_diagram(**kwargs) -> str:
        image_path = kwargs.get("image_path")
        if image_path:
            try:
                kwargs["image_path"] = str(
                    resolve_readable_path(image_path, root, readable_roots)
                )
            except ValueError as e:
                return f"错误：参考图片不可用：{e}"
        return session.create(**kwargs)

    return create_diagram


def build_skills(
    session: DiagramSession,
    image_queue: ImageQueue,
    ocr_llm: LLMClient | None = None,
    command_runner: CommandRunner | None = None,
    readable_root: Path | None = None,
    readable_roots: Iterable[Path] | None = None,
) -> list[Skill]:
    """构建主 Agent 的工具表。ocr_llm 不为 None 时注册 ocr_image
    （主模型无视觉能力时，用多模态验证模型做图片文字提取）；
    command_runner 不为 None 时注册 run_command（界面层提供确认与进程管理）。"""
    writable_root = session.output_dir  # write_file/replace_in_file 的写入边界
    skills = [
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
            handler=partial(
                read_document, root=readable_root, readable_roots=readable_roots
            ),
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
            handler=partial(
                find_files, root=readable_root, readable_roots=readable_roots
            ),
        ),
        Skill(
            name="write_file",
            description=(
                "新建或整体覆盖一个文本文件（自动创建父目录）。"
                "用于生成中间文档、或按用户要求输出其它格式的文件"
                "（markdown、csv、纯文本等）。仅限产物目录内，相对路径按产物目录解析。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
            handler=partial(write_file, root=writable_root),
        ),
        Skill(
            name="replace_in_file",
            description=(
                "在文本文件中做精确字符串替换（先 read_document 拿到原文再改）。"
                "old_text 必须与文件内容完全一致；多处匹配时需提供更长上下文或 "
                "replace_all=true。仅限产物目录内。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文（精确匹配）"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换全部匹配处，默认 false（仅一处，多处则报错）",
                        "default": False,
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
            handler=partial(replace_in_file, root=writable_root),
        ),
        Skill(
            name="grep_files",
            description=(
                "按正则表达式搜索文件内容，返回 文件:行号: 匹配行。"
                "用于在中间文档/代码中定位内容（配合 replace_in_file 修改）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式，如 保存设置"},
                    "directory": {
                        "type": "string",
                        "description": "搜索的起始目录，默认当前目录",
                        "default": ".",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "文件名过滤，如 *.md；默认所有文件",
                        "default": "",
                    },
                },
                "required": ["pattern"],
            },
            handler=partial(
                grep_files, root=readable_root, readable_roots=readable_roots
            ),
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
            handler=_make_read_image_handler(
                image_queue, readable_root, readable_roots
            ),
        ),
        Skill(
            name="create_diagram",
            description=(
                "根据完整的需求描述创建一张新的流程图（内部会自动完成"
                "生成→渲染校验→视觉验证的循环）。仅在用户提出新图需求时调用。"
                "若用户提供了参考图片路径，通过 image_path 传入；"
                "若用户有风格倾向，先 list_styles 发现可用模板并经 style 传入；"
                "若用户明确要求画布背景颜色，通过 background 传入；"
                "若技能包或用户指定了框的尺寸/间距，通过 node_width/"
                "node_height/gap_x/gap_y 传入（验证失败重画时也可微调它们）。"
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
                    "node_width": {
                        "type": "integer",
                        "description": "可选：节点框宽 px（默认 220）；技能包指定框尺寸时设置",
                    },
                    "node_height": {
                        "type": "integer",
                        "description": "可选：节点框最小高 px（默认 70）；文字换行更多时全图会自动加高，不会溢出",
                    },
                    "gap_x": {
                        "type": "integer",
                        "description": "可选：同层节点水平间距 px（默认 60）",
                    },
                    "gap_y": {
                        "type": "integer",
                        "description": "可选：层间垂直间距 px（默认 60）",
                    },
                },
                "required": ["requirement"],
            },
            handler=_make_create_handler(session, readable_root, readable_roots),
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
            name="set_output_engine",
            description=(
                "切换出图引擎（作用于后续生成与修改）。"
                "mermaid=默认：产出 Mermaid 代码（.mmd），mmdc 渲染；"
                "drawio=直接产出 draw.io 原生可编辑文件（.drawio，组件等大、"
                "网格对齐，可导入 draw.io/Visio/亿图二次编辑），由本机 draw.io "
                "桌面版渲染，需要 .env 配置 DRAWIO_PATH。"
                "用户要求可编辑产物、方方正正的架构图、或明确要求 drawio 模式时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "engine": {
                        "type": "string",
                        "enum": ["mermaid", "drawio"],
                        "description": "出图引擎：mermaid 或 drawio",
                    },
                },
                "required": ["engine"],
            },
            handler=session.set_output_engine,
        ),
        Skill(
            name="set_verification",            description=(
                "调整检视强度（作用于后续生成与修改）。"
                "full=完整检视：排版结构 + 内容与逻辑核对；"
                "layout=仅基础图形检视：只查文字排版错误、连线混乱、方框遮挡，"
                "不逐字核对内容，视觉模型识字能力弱时用；"
                "code=代码检视：不看渲染图，文本模型直接审查 Mermaid 源码，"
                "完全没有视觉模型时的兜底（查不了排版问题）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["full", "layout", "code"],
                        "description": "检视强度：full、layout 或 code",
                    },
                },
                "required": ["mode"],
            },
            handler=session.set_verify_mode,
        ),
        Skill(
            name="create_style",
            description=(
                "根据用户的自然语言风格描述，生成一个新的作图风格插件"
                "（写入 styles/ 目录的 .md 文件，内部会自动校验格式并试渲染，"
                "成功后自动切换为当前风格）。当现有风格模板（list_styles）"
                "都不能满足用户的风格需求时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "风格标识（英文小写，可含数字/下划线/连字符），如 handdrawn",
                    },
                    "description": {
                        "type": "string",
                        "description": "完整的风格要求描述，如：手绘风格，暖色调，适合产品评审",
                    },
                },
                "required": ["name", "description"],
            },
            handler=session.create_style,
        ),
        Skill(
            name="create_skill",
            description=(
                "根据用户描述生成提示词型 Skill，校验通过后写入当前会话的 skills 目录。"
                "用户要求沉淀新的领域流程、操作规范或 Agent 指引时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "英文小写 Skill 标识"},
                    "description": {"type": "string", "description": "完整需求、触发条件和期望行为"},
                },
                "required": ["name", "description"],
            },
            handler=session.create_skill,
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
            name="restyle_diagram",
            description=(
                "只调整当前流程图的风格（配色、主题、背景等），内容与结构严格保持原样"
                "（内部有骨架校验，任何内容改动都会被拒绝）。风格来源二选一："
                "style_name 指定现有风格模板（来自 list_styles），或 style_document "
                "传入风格要求文本（用户给了风格文档路径时先 read_document 读取再传入）。"
                "当用户只想换风格、不想改内容时调用；要改内容用 modify_diagram。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "style_name": {
                        "type": "string",
                        "description": "可选：现有风格模板名，如 dark",
                    },
                    "style_document": {
                        "type": "string",
                        "description": "可选：风格要求的完整文本（或风格文档的内容）",
                    },
                },
            },
            handler=session.restyle,
        ),
        Skill(
            name="list_skill_packs",
            description=(
                "列出 skills/ 目录下所有可用的技能包（名称与适用场景）。"
                "遇到自己不熟悉领域的任务（如特定导出格式、行业图表规范、"
                "第三方工具对接）时，先调用本工具看看有没有现成指引。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=session.list_skill_packs,
        ),
        Skill(
            name="use_skill",
            description=(
                "读取指定技能包的完整操作指引并遵照执行。"
                "技能包名须来自 list_skill_packs。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能包名，如 drawio-export"},
                },
                "required": ["name"],
            },
            handler=session.use_skill,
        ),
        Skill(
            name="get_current_diagram",
            description="查看当前流程图的源码（Mermaid 或 drawio XML）与产物路径。",
            parameters={"type": "object", "properties": {}},
            handler=lambda: (
                f"当前{'drawio XML' if session.engine == 'drawio' else 'Mermaid 代码'}：\n"
                f"```{session.engine if session.engine == 'drawio' else 'mermaid'}\n"
                f"{session.current_code}\n```\n"
                f"图片：{session.current_image}"
                if session.has_diagram
                else "当前还没有流程图。"
            ),
        ),
        Skill(
            name="read_working_doc",
            description=(
                "读取工作文档：整合多份素材（文档/图片）信息与初步生成方案的"
                "中间产物（markdown）。处理多素材需求前先读它，避免遗漏。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=session.read_working_doc,
        ),
        Skill(
            name="write_working_doc",
            description=(
                "写入或修改工作文档（整体覆盖，先 read_working_doc 再改即可局部修订）。"
                "用于把多份素材的内容与初步生成方案整合成一份 markdown，"
                "而不是全部堆在自己的对话上下文里。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "工作文档的完整 markdown 内容",
                    },
                },
                "required": ["content"],
            },
            handler=session.write_working_doc,
        ),
    ]
    if command_runner is not None:
        skills.append(
            Skill(
                name="run_command",
                description=(
                    "运行一条单行 shell 命令并返回输出（执行前会向用户请求确认，"
                    "用户可能拒绝）。用于用户明确要求的系统操作、格式转换、"
                    "批量文件处理等；命令尽量只读，破坏性命令务必先向用户说明风险。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "要执行的单行 shell 命令",
                        },
                    },
                    "required": ["command"],
                },
                handler=command_runner.run,
            )
        )
    if ocr_llm is not None:
        skills.append(
            Skill(
                name="ocr_image",
                description=(
                    "从图片中提取文字内容（OCR，由多模态验证模型完成）。"
                    "素材是图片（扫描件、截图、手绘草图、已有图表照片）时使用；"
                    "是图表时还会附带连接关系的文字描述。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "图片文件路径"},
                    },
                    "required": ["path"],
                },
                handler=_make_ocr_handler(
                    ocr_llm, readable_root, readable_roots
                ),
            )
        )
    return skills

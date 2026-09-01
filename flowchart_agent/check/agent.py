"""检查编排 Agent：两级分类路由的 check 分支执行者。

逐项检查（用户明确指定则只查指定项，否则全部逐项检查），每项由
ItemCheckAgent 子 Agent 执行，结论为三值：通过 / 不通过 / 不符合该分类。
产物落盘 <output>/check/v<n>/：
report.csv（报告，UTF-8-BOM 兼容 Excel）、source/（本次检查的原始素材副本：
图片与文档，方便溯源）、classify_raw.txt、describe_<i>.txt、
check_<检查项>_<图>_raw.txt、run.log。
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
from pathlib import Path
from typing import Callable, Iterable

from ..cancellation import CancelCheck, OperationCancelled, raise_if_cancelled
from ..config import Settings
from ..images import validate_image
from ..llm import LLMClient
from ..skills.builtin import find_files, read_document, resolve_readable_path
from .classifier import CheckClassifier, Classification
from .item_agent import ItemCheckAgent, ItemResult, VERDICT_FAIL, VERDICT_NA, VERDICT_PASS
from .items import load_check_items, resolve_items

logger = logging.getLogger(__name__)

# 图片类型关键词：从图片描述首行推断图片类别
_KIND_KEYWORDS = ("原理图", "流程图", "组网", "拓扑", "界面截图", "界面")
_BATCH_TERMS = ("批量", "目录", "全部图片", "所有图片", "一批")
_BATCH_PATH = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|(?:workspace|attachments|generate|check)[\\/])"
    r"[^\s，。；;！？!?\"'<>]+)"
)
def _infer_kind(description: str) -> str:
    """从图片描述（首行含类型判断）推断图片类别关键词。"""
    head = description[:150]
    for kw in _KIND_KEYWORDS:
        if kw in head:
            return kw
    return ""


class CheckAgent:
    """文档/图片检视编排：素材收集 → 二级分类 → 逐项派发给 ItemCheckAgent → CSV 报告。"""

    def __init__(
        self,
        settings: Settings,
        output_root: str | Path,
        readable_root: Path | None = None,
        readable_roots: Iterable[Path] | None = None,
        skill_dir: Path | None = None,
        should_cancel: CancelCheck = None,
    ):
        self._settings = settings
        self._check_root = Path(output_root) / "check"
        self._readable_root = readable_root
        self._readable_roots = tuple(readable_roots or ())
        self._skill_dir = skill_dir
        self._should_cancel = should_cancel
        self._text_llm = LLMClient(settings.text_model)
        self._vision_llm = (
            LLMClient(settings.vision_model) if settings.vision_model else None
        )

    def handle(
        self,
        user_input: str,
        images: list[Path] | None = None,
        document_paths: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """检查管线入口，返回给用户的回复文本。on_progress 为界面层进度回调。"""
        progress = on_progress or (lambda _msg: None)
        raise_if_cancelled(self._should_cancel)
        available_items = load_check_items(self._skill_dir)
        if not available_items:
            return (
                "无法执行检查：当前 Session 的 Skills 中没有合法的检查 Skill。"
                "系统不会使用内置或臆造的审查标准。请提供审查标准文档，或在 Skills "
                "中导入带 `kind: check` 和检查条目的 Skill 后重试。"
            )
        if self._vision_llm is None:
            return (
                "检查功能需要视觉模型支持，但当前未配置 VISION_MODEL_*。"
                "请在 .env 中配置视觉模型后再试。"
            )
        classifier = CheckClassifier(
            self._text_llm, self._vision_llm,
            should_cancel=self._should_cancel,
        )
        images = list(images or [])

        # 1. 图片描述（技术路线第一步）+ 二级分类（技术路线第二步）
        if images:
            progress(f"正在描述 {len(images)} 张图片的内容…")
        descriptions = classifier.describe_images(images, progress)
        progress("正在匹配检查项…")
        cls = classifier.classify(user_input, descriptions, available_items)
        if cls is None:
            return "没能确定要执行哪些检查，请换个说法再试（如：只检查敏感信息）。"
        if not cls.supported:
            return (
                "无法执行这项检查：当前 Session 中没有与需求匹配的检查 Skill，"
                "系统不会猜测审查标准。请提供一份审查标准文档（至少说明检查对象、"
                "检查项、适用范围和通过/不通过判定），再导入为检查 Skill。"
            )

        # 2. 收集素材：输入文本中提到的路径 + 用户直接贴入的图片
        requested_docs = list(dict.fromkeys([*cls.doc_paths, *(document_paths or [])]))
        docs, doc_paths, doc_errors = self._load_documents(requested_docs)
        extra_images, img_errors = self._load_images(cls.image_paths)
        known = {p.resolve() for p in images}
        new_images = [p for p in extra_images if p.resolve() not in known]
        if new_images:
            progress(f"正在描述 {len(new_images)} 张图片的内容…")
            descriptions.extend(classifier.describe_images(new_images, progress))
            images.extend(new_images)
        errors = doc_errors + img_errors

        if not images:
            return (
                "检查需要至少一张图片（原理图/流程图/组网图/界面截图）。"
                "请把图片拖入输入框，或在消息中给出图片路径。"
                + ("\n\n素材加载问题：\n" + "\n".join(errors) if errors else "")
            )

        selected = resolve_items(cls.items, available_items)
        if not selected:
            return (
                "无法执行这项检查：检查路由没有从当前 Session 的检查 Skill 中匹配到"
                "有效检查项。请提供审查标准文档，或补充对应检查 Skill 后重试。"
            )
        progress(
            "已匹配检查项：" + "、".join(i.name for i in selected)
            if cls.items != "all" else "未指定检查项，逐项全部检查…"
        )

        # 3. 逐项执行检查并落盘
        run_dir = self._next_run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        handler = _attach_run_log(run_dir / "run.log")
        try:
            logger.info(
                "检查任务开始：检查项=%s 文档=%d 份 图片=%d 张",
                [i.id for i in selected], len(docs), len(images),
            )
            (run_dir / "classify_raw.txt").write_text(cls.raw, encoding="utf-8")
            for i, (p, desc) in enumerate(descriptions, 1):
                (run_dir / f"describe_{i}_{p.stem}.txt").write_text(desc, encoding="utf-8")
            copied = _copy_sources(run_dir, images, doc_paths)
            logger.info("原始素材已复制 %d 份 -> %s", copied, run_dir / "source")

            document = "\n\n".join(docs) if docs else "（未提供文档文本）"
            kinds = {p: _infer_kind(desc) for p, desc in descriptions}
            item_agent = ItemCheckAgent(
                self._vision_llm, should_cancel=self._should_cancel
            )
            results: list[ItemResult] = []
            for item in selected:
                raise_if_cancelled(self._should_cancel)
                progress(f"正在执行：{item.name}…")
                item_results = item_agent.run(
                    item, images, kinds, document, on_progress=progress
                )
                for r in item_results:
                    if r.raw:
                        img_stem = r.image.stem if r.image else "na"
                        (run_dir / f"check_{item.id}_{img_stem}_raw.txt").write_text(
                            r.raw, encoding="utf-8"
                        )
                results.extend(item_results)

            csv_path = run_dir / "report.csv"
            _write_report_csv(csv_path, results)
            logger.info("检查报告 -> %s", csv_path)
        finally:
            _detach_run_log(handler)

        # 4. 回复摘要
        passed = [r for r in results if r.verdict == VERDICT_PASS]
        failed = [r for r in results if r.verdict == VERDICT_FAIL]
        na = [r for r in results if r.verdict == VERDICT_NA]
        lines = [
            f"检查完成（{len(selected)} 项）："
            f"通过 {len(passed)} 项，不通过 {len(failed)} 项，不符合该分类 {len(na)} 项。"
        ]
        for r in failed:
            img = r.image.name if r.image else "-"
            lines.append(f"\n**{r.item.name}**（{img}）：")
            lines.append(r.findings[:500])
        if errors:
            lines.append("\n注意，部分素材加载失败：\n" + "\n".join(errors))
        lines.append(f"\n完整报告（CSV）：{csv_path}")
        return "\n".join(lines)

    # ---------- 素材收集 ----------

    def find_batch_directory(
        self, user_input: str, explicit: str | Path | None = None
    ) -> Path | None:
        """从批量请求中解析一个受 Session 边界保护的目录。"""
        if explicit is None and not any(term in user_input for term in _BATCH_TERMS):
            return None
        candidates = [str(explicit)] if explicit is not None else [
            match.group("path") for match in _BATCH_PATH.finditer(user_input)
        ]
        for raw in candidates:
            try:
                path = resolve_readable_path(
                    raw, self._readable_root, self._readable_roots, allow_root=True
                )
            except ValueError:
                continue
            if path.is_dir():
                return path
        return None

    def relative_path(self, path: Path) -> str:
        """返回供 Agent 与 WebUI 使用的 Session 相对路径，避免泄露服务端路径。"""
        if self._readable_root is not None:
            try:
                return path.resolve().relative_to(Path(self._readable_root).resolve()).as_posix()
            except ValueError:
                pass
        return path.name

    def _load_documents(self, paths: list[str]) -> tuple[list[str], list[Path], list[str]]:
        """读文档；路径不存在时按文件名关键词 find_files 自我纠正一次。
        返回 (文档文本, 实际读取的文件路径, 错误列表)。"""
        docs, doc_paths, errors = [], [], []
        for raw in paths:
            path = raw
            try:
                candidate = resolve_readable_path(
                    path, self._readable_root, self._readable_roots
                )
            except ValueError:
                candidate = Path(path)
            if not candidate.is_file():
                found = find_files(
                    Path(path).name or path,
                    root=self._readable_root,
                    readable_roots=self._readable_roots,
                )
                candidates = [
                    l.split(" | size=", 1)[0]
                    for l in found.splitlines()[1:] if l.strip()
                ] if found.startswith("找到") else []
                if len(candidates) == 1:
                    logger.info("[check] 路径纠正：%s -> %s", path, candidates[0])
                    path = candidates[0]
            content = read_document(
                path,
                root=self._readable_root,
                readable_roots=self._readable_roots,
            )
            if content.startswith("错误："):
                errors.append(content)
            else:
                docs.append(f"# 文档 {path}\n\n{content}")
                doc_paths.append(Path(path))
        return docs, doc_paths, errors

    def _load_images(self, paths: list[str]) -> tuple[list[Path], list[str]]:
        images, errors = [], []
        for raw in paths:
            try:
                safe_path = resolve_readable_path(
                    raw, self._readable_root, self._readable_roots
                )
                images.append(validate_image(safe_path))
            except ValueError as e:
                errors.append(f"错误：{e}")
        return images, errors

    # ---------- 产物 ----------

    def _next_run_dir(self) -> Path:
        self._check_root.mkdir(parents=True, exist_ok=True)
        existing = [
            int(p.name[1:]) for p in self._check_root.iterdir()
            if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit()
        ]
        return self._check_root / f"v{(max(existing) + 1) if existing else 1}"


def _copy_sources(run_dir: Path, images: list[Path], doc_paths: list[Path]) -> int:
    """把本次检查的原始素材（图片 + 文档）复制到 run_dir/source/，方便溯源。
    同名文件自动加序号后缀。返回成功复制的份数。"""
    src_dir = run_dir / "source"
    src_dir.mkdir(exist_ok=True)
    copied = 0
    for p in [*images, *doc_paths]:
        p = Path(p)
        target = src_dir / p.name
        n = 2
        while target.exists():
            target = src_dir / f"{p.stem}_{n}{p.suffix}"
            n += 1
        try:
            shutil.copy(p, target)
            copied += 1
        except OSError as e:
            logger.warning("源文件复制失败 %s：%s", p, e)
    return copied


def _write_report_csv(
    csv_path: Path, results: list[ItemResult], result_cases: dict[int, str] | None = None
) -> None:
    """逐项检查报告：检查项 / 图片 / 结果（通过/不通过/不符合该分类）/ 问题说明。
    UTF-8 with BOM，Excel 直接打开不乱码。"""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["案例", "检查项", "检查项ID", "图片", "结果", "问题/说明"])
        for r in results:
            writer.writerow([
                (result_cases or {}).get(id(r), "-"),
                r.item.name,
                r.item.id,
                r.image.name if r.image else "-",
                r.verdict,
                r.findings,
            ])


def _attach_run_log(log_path: Path) -> logging.FileHandler:
    """把检查过程同时写入 <run_dir>/run.log（与生成侧同一约定）。"""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger("flowchart_agent").addHandler(handler)
    return handler


def _detach_run_log(handler: logging.FileHandler) -> None:
    logging.getLogger("flowchart_agent").removeHandler(handler)
    handler.close()

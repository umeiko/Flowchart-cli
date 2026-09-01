from __future__ import annotations

import hashlib
import mimetypes
import shutil
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..cancellation import OperationCancelled
from ..config import Settings
from ..main_agent import MainAgent
from ..runtime import app_dir
from ..session import DiagramSession
from ..skillpacks import parse_skill_pack_text
from ..styles import parse_style_text
from .storage import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_kind(path: Path) -> str:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    if path.suffix.lower() == ".svg":
        return "svg"
    if path.suffix.lower() in {".mmd", ".drawio"}:
        return "source"
    if path.suffix.lower() in {".log", ".txt", ".md", ".csv"}:
        return "text"
    return "file"


@dataclass
class RunState:
    id: str
    session_id: str
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    completed_at: str | None = None
    reply: str | None = None
    error: str | None = None
    events: list[dict] = field(default_factory=list)
    _event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def emit(self, event_type: str, **data) -> dict:
        with self._event_lock:
            event = {
                "id": len(self.events) + 1,
                "run_id": self.id,
                "session_id": self.session_id,
                "type": event_type,
                "timestamp": _now(),
                "data": data,
            }
            self.events.append(event)
            return event

    def events_after(self, event_id: int) -> list[dict]:
        with self._event_lock:
            return [event for event in self.events if event["id"] > event_id]

    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def request_cancel(self) -> None:
        if self.status in {"completed", "failed", "cancelled"}:
            return
        self._cancel_event.set()
        if self.status != "cancelling":
            self.status = "cancelling"
            self.emit("run.cancelling")


@dataclass
class SessionState:
    id: str
    created_at: str
    root: Path
    diagram: DiagramSession
    agent: MainAgent
    user_id: str = ""
    title: str = "未命名会话"
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    files: dict[str, Path] = field(default_factory=dict)
    active_run_holder: dict = field(default_factory=lambda: {"run": None}, repr=False)
    mounted_resources: dict[str, set[str]] = field(
        default_factory=lambda: {"skills": set(), "styles": set()}, repr=False
    )
    builtin_resources: dict[str, set[str]] = field(
        default_factory=lambda: {"skills": set(), "styles": set()}, repr=False
    )


class AgentService:
    """Single-process MVP service. Sessions and chat history live in memory."""

    def __init__(
        self,
        settings: Settings,
        data_root: str | Path,
        workspace_root: str | Path = "output",
        store: Store | None = None,
    ):
        self.settings = settings
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.store = store or Store(self.data_root / "flowchart.db")
        self.sessions: dict[str, SessionState] = {}
        self.runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="flowchart-run")

    def session_workspace(self, session_id: str) -> Path:
        root = self.get_session(session_id).root / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        return root

    _workspace_roots = {"workspace", "attachments", "generate", "check"}

    def _workspace_path(
        self, session_id: str, relative_path: str, *, must_exist: bool = True,
        allow_root: bool = True,
    ) -> Path:
        session = self.get_session(session_id)
        if not relative_path or "\x00" in relative_path:
            raise ValueError("文件路径不能为空")
        candidate = (session.root / relative_path).resolve()
        try:
            relative = candidate.relative_to(session.root)
        except ValueError as exc:
            raise ValueError("文件路径超出当前 Session 工作区") from exc
        if not relative.parts or relative.parts[0] not in self._workspace_roots:
            raise ValueError("该 Session 内部目录不允许通过 Workspace 访问")
        if not allow_root and len(relative.parts) == 1:
            raise ValueError("不能修改 Workspace 顶级目录")
        if must_exist and not candidate.exists():
            raise FileNotFoundError(relative_path)
        return candidate

    def workspace_tree(self, session_id: str) -> list[dict]:
        session = self.get_session(session_id)
        def walk(directory: Path) -> list[dict]:
            nodes = []
            try:
                entries = sorted(
                    directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())
                )
            except OSError:
                return nodes
            for path in entries:
                relative = path.relative_to(session.root).as_posix()
                if path.is_dir():
                    nodes.append(
                        {
                            "name": path.name,
                            "path": relative,
                            "type": "directory",
                            "children": walk(path),
                        }
                    )
                elif path.is_file():
                    nodes.append(
                        {
                            "name": path.name,
                            "path": relative,
                            "type": "file",
                            "size": path.stat().st_size,
                            "children": [],
                        }
                    )
            return nodes

        nodes = []
        for name in ("workspace", "attachments", "generate", "check"):
            directory = session.root / name
            if directory.is_dir():
                nodes.append({
                    "name": name, "path": name, "type": "directory",
                    "children": walk(directory),
                })
        return nodes

    def workspace_file(self, session_id: str, relative_path: str) -> Path:
        candidate = self._workspace_path(session_id, relative_path)
        if not candidate.is_file():
            raise FileNotFoundError(relative_path)
        return candidate

    def workspace_download(self, session_id: str, relative_path: str) -> tuple[Path, bool]:
        """返回下载目标；目录会打包为临时 ZIP，并由响应层负责删除。"""
        target = self._workspace_path(session_id, relative_path)
        if target.is_file():
            return target, False
        if not target.is_dir():
            raise FileNotFoundError(relative_path)

        session_root = self.get_session(session_id).root.resolve()
        target_root = target.resolve()
        handle = tempfile.NamedTemporaryFile(
            prefix="flowchart-directory-", suffix=".zip", delete=False
        )
        archive = Path(handle.name)
        handle.close()
        try:
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as output:
                root_name = target.name
                output.writestr(f"{root_name}/", b"")
                for path in sorted(target.rglob("*"), key=lambda item: item.as_posix()):
                    if path.is_symlink():
                        continue
                    resolved = path.resolve(strict=True)
                    try:
                        resolved.relative_to(session_root)
                        resolved.relative_to(target_root)
                    except ValueError as exc:
                        raise ValueError("目录中包含超出当前 Session 的路径") from exc
                    member = Path(root_name, *path.relative_to(target).parts).as_posix()
                    if path.is_dir():
                        output.writestr(member.rstrip("/") + "/", b"")
                    elif path.is_file():
                        output.write(path, member)
            return archive, True
        except Exception:
            archive.unlink(missing_ok=True)
            raise

    def web_text(self, session_id: str, content: str | None) -> str | None:
        """Hide server paths while preserving clickable Session file references for Web clients."""
        if not content:
            return content
        session = self.get_session(session_id)
        session_root = str(session.root.resolve())
        root_variants = {session_root, session_root.replace("\\", "/")}
        if not any(source in content for source in root_variants):
            return content
        replacements: list[tuple[str, str]] = []
        for root_name in self._workspace_roots:
            root = session.root / root_name
            if not root.exists():
                continue
            for path in (root, *root.rglob("*")):
                try:
                    relative = path.resolve().relative_to(session.root).as_posix()
                except (OSError, ValueError):
                    continue
                if path.is_file():
                    replacement = f"[{relative}](workspace-file:{quote(relative, safe='')})"
                elif path.is_dir():
                    replacement = f"`{relative}`"
                else:
                    continue
                resolved = str(path.resolve())
                replacements.extend(
                    (
                        (resolved, replacement),
                        (resolved.replace("\\", "/"), replacement),
                    )
                )

        rendered = content
        for source, replacement in sorted(set(replacements), key=lambda item: len(item[0]), reverse=True):
            rendered = rendered.replace(source, replacement)

        # Historical messages may reference files that have since been deleted.
        # Remove the Session root even when no current tree entry can be linked.
        for source in root_variants:
            rendered = rendered.replace(source, "当前 Session")
        return rendered

    def create_workspace_entry(self, session_id: str, relative_path: str, entry_type: str) -> Path:
        target = self._workspace_path(session_id, relative_path, must_exist=False, allow_root=False)
        if target.exists():
            raise ValueError(f"已存在：{relative_path}")
        if not target.parent.is_dir():
            raise ValueError("父目录不存在")
        if entry_type == "directory":
            target.mkdir()
        elif entry_type == "file":
            target.touch()
        else:
            raise ValueError("不支持的文件类型")
        return target

    def transfer_workspace_entry(
        self, session_id: str, source_path: str, target_path: str, operation: str
    ) -> Path:
        session = self.get_session(session_id)
        source = self._workspace_path(session_id, source_path, allow_root=False)
        target = self._workspace_path(session_id, target_path, must_exist=False, allow_root=False)
        if target.exists():
            raise ValueError(f"目标已存在：{target_path}")
        if not target.parent.is_dir():
            raise ValueError("目标父目录不存在")
        if source.is_dir() and (target == source or source in target.parents):
            raise ValueError("不能把目录移动或复制到自身内部")
        if operation == "copy":
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        elif operation == "move":
            shutil.move(str(source), str(target))
            for file_id, registered in list(session.files.items()):
                try:
                    tail = registered.relative_to(source)
                except ValueError:
                    continue
                session.files[file_id] = target / tail
        else:
            raise ValueError("不支持的文件操作")
        return target

    def delete_workspace_entry(self, session_id: str, relative_path: str) -> None:
        session = self.get_session(session_id)
        target = self._workspace_path(session_id, relative_path, allow_root=False)
        for file_id, registered in list(session.files.items()):
            if registered == target or target in registered.parents:
                session.files.pop(file_id, None)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    def save_workspace_file(self, session_id: str, filename: str, content: bytes) -> Path:
        clean_name = Path(filename).name.strip()
        if not clean_name:
            raise ValueError("文件名不能为空")
        stem = Path(clean_name).stem
        suffix = Path(clean_name).suffix
        workspace = self.session_workspace(session_id)
        path = workspace / clean_name
        counter = 1
        while path.exists():
            path = workspace / f"{stem}_{counter}{suffix}"
            counter += 1
        path.write_bytes(content)
        return path

    def create_session(self, *, user_id="", title="未命名会话", engine=None, verification_mode=None, style=None, _restore_id=None) -> SessionState:
        session_id = _restore_id or f"sess_{uuid.uuid4().hex}"
        root = self.data_root / "users" / user_id / "sessions" / session_id
        root.mkdir(parents=True, exist_ok=bool(_restore_id))
        (root / "workspace").mkdir(exist_ok=True)
        (root / "attachments").mkdir(exist_ok=True)
        builtin_resources = {"skills": set(), "styles": set()}
        for kind in ("skills", "styles"):
            source = app_dir() / kind
            target = root / "client" / kind
            target.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                for item in source.glob("*.md"):
                    if not (target / item.name).exists():
                        shutil.copy2(item, target / item.name)
                    if self._valid_resource(kind, item.read_text(encoding="utf-8")):
                        builtin_resources[kind].add(item.name)
        diagram = DiagramSession(
            self.settings,
            root / "generate",
            style_dir=root / "client" / "styles",
            skill_dir=root / "client" / "skills",
        )
        if engine:
            result = diagram.set_output_engine(engine)
            if result.startswith("错误") or result.startswith("暂时无法"):
                raise ValueError(result)
            diagram.restore_from_disk()
        if verification_mode:
            diagram.set_verify_mode(verification_mode)
        if style:
            result = diagram.set_style(style)
            if result.startswith("错误"):
                raise ValueError(result)
        holder: dict[str, RunState | None] = {"run": None}

        def emit(event_type: str, **data) -> None:
            run = holder["run"]
            if run is not None:
                run.emit(event_type, **data)

        def workspace_changed(reason: str, *, refresh_diagram: bool = False) -> None:
            """Notify streaming clients after a generation milestone may have written files."""
            emit(
                "workspace.changed",
                reason=reason,
                refresh_diagram=refresh_diagram,
            )

        def generation_stage(stage: str, message: str) -> None:
            emit("generation.stage", stage=stage, message=message)
            # Stage callbacks are emitted immediately after the preceding stage has
            # persisted its outputs.  In particular, ``verifying`` means the
            # rendered candidate and current.* are ready for clients to inspect.
            workspace_changed(stage, refresh_diagram=stage == "verifying")

        def tool_completed(name: str, result: str) -> None:
            emit("tool.completed", name=name, result=result)
            workspace_changed(f"tool:{name}")

        def progress_updated(message: str) -> None:
            emit("progress.updated", message=message)
            workspace_changed("progress")

        def subagent_event(event: str, data: dict) -> None:
            emit(f"subagent.{event}", **data)
            if event == "usage":
                emit("usage.delta", chars=data.get("chars", 0), kind="subagent")
            if event == "tool.completed":
                workspace_changed(f"subagent:{data.get('name', 'tool')}")

        diagram.on_delta = lambda text: (
            emit("generation.delta", text=text),
            emit("usage.delta", chars=len(text), kind="generation"),
        )
        diagram.on_reasoning = lambda text: (
            emit("reasoning.status", status="thinking"),
            emit("reasoning.delta", text=text),
            emit("usage.delta", chars=len(text), kind="reasoning"),
        )
        diagram.on_round_start = lambda round_no: (
            emit("generation.round_started", round=round_no),
            workspace_changed(f"round:{round_no}"),
        )
        diagram.on_stage = generation_stage
        diagram.on_verify_delta = lambda text: (
            emit("verification.delta", text=text, channel="content"),
            emit("usage.delta", chars=len(text), kind="verification"),
        )
        diagram.on_verify_tick = lambda text: (
            emit("verification.delta", text="", chars=len(text), channel="tool"),
            emit("usage.delta", chars=len(text), kind="verification"),
        )
        diagram.should_cancel = lambda: bool(
            holder["run"] and holder["run"].cancel_requested()
        )
        agent = MainAgent(
            self.settings,
            diagram,
            on_tool_call=lambda name, arguments: emit(
                "tool.started", name=name, arguments=arguments
            ),
            on_tool_result=tool_completed,
            on_delta=lambda text: emit("assistant.delta", text=text),
            on_tick=lambda text: emit("usage.delta", chars=len(text), kind="tool"),
            on_reasoning=lambda text: (
                emit("reasoning.status", status="thinking"),
                emit("reasoning.delta", text=text),
                emit("usage.delta", chars=len(text), kind="reasoning"),
            ),
            output_root=root,
            readable_root=root,
            readable_roots=tuple(
                root / name for name in ("workspace", "attachments", "generate", "check")
            ),
            on_progress=progress_updated,
            command_runner=None,
            should_cancel=diagram.should_cancel,
            on_subagent_event=subagent_event,
        )
        state = SessionState(
            session_id, _now(), root, diagram, agent, user_id=user_id, title=title,
            active_run_holder=holder, builtin_resources=builtin_resources,
        )
        with self._lock:
            self.sessions[session_id] = state
        if _restore_id:
            state.mounted_resources = self.store.resource_mounts(session_id)
            self._activate_mounted_resources(state)
            summary, messages = self.store.context_messages(session_id)
            agent.restore_history(messages, summary)
        else:
            self.store.save_session(
                session_id, user_id, title, diagram.engine, diagram.verify_mode,
                diagram.style.name if diagram.style else None,
            )
        return state

    def get_session(self, session_id: str) -> SessionState:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            record = self.store.session_by_id(session_id)
            if record is None:
                raise KeyError(f"未知会话：{session_id}") from exc
            return self.create_session(
                user_id=record["user_id"], title=record["title"],
                engine=record["engine"], verification_mode=record["verification_mode"],
                style=record["style"], _restore_id=session_id,
            )

    def create_run(self, session_id: str, user_input: str, attachments: list[str]) -> RunState:
        session = self.get_session(session_id)
        active = session.active_run_holder["run"]
        if active is not None and active.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("当前 Session 已有任务正在运行")
        if session.title == "未命名会话":
            title = " ".join(user_input.strip().split())[:28] or "未命名会话"
            session.title = title
            self.store.rename_session(session_id, session.user_id, title)
        paths: list[Path] = []
        for file_id in attachments:
            if file_id not in session.files:
                raise ValueError(f"未知附件：{file_id}")
            paths.append(session.files[file_id])
        run = RunState(f"run_{uuid.uuid4().hex}", session_id)
        self.store.add_message(
            session_id, "user", user_input,
            __import__("json").dumps([path.name for path in paths], ensure_ascii=False),
        )
        run.emit("run.queued")
        with self._lock:
            self.runs[run.id] = run
            session.active_run_holder["run"] = run
        self._executor.submit(self._execute_run, session, run, user_input, paths)
        return run

    def _execute_run(
        self, session: SessionState, run: RunState, user_input: str, paths: list[Path]
    ) -> None:
        with session.lock:
            if run.cancel_requested():
                self._finish_cancelled(session, run)
                return
            run.status = "running"
            run.emit("run.started")
            try:
                image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                images = [path for path in paths if path.suffix.lower() in image_suffixes]
                documents = [path for path in paths if path not in images]
                prompt = user_input
                session.mounted_resources = self.store.resource_mounts(session.id)
                activated = self._activate_mounted_resources(session)
                for item in activated:
                    run.emit("resource.activated", **item)
                mounted = self.mounted_prompt(session)
                if mounted:
                    prompt += mounted
                if documents:
                    prompt += (
                        "\n\n[系统提供的本轮用户附件]\n"
                        + "\n".join(f"- {path.name}: {path}" for path in documents)
                        + "\n在回答本轮问题前，必须先用 read_document 读取上述附件；"
                        "不要只根据文件名或用户描述猜测内容。"
                    )
                reply = session.agent.chat(prompt, images=images or None)
                run.reply = reply
                self.store.add_message(session.id, "assistant", reply)
                if run.cancel_requested():
                    run.status = "cancelled"
                    run.completed_at = _now()
                    run.emit("run.cancelled", reply=reply)
                else:
                    run.status = "completed"
                    run.completed_at = _now()
                    run.emit("run.completed", reply=reply)
            except OperationCancelled:
                self._finish_cancelled(session, run)
            except Exception as exc:
                run.error = str(exc)
                run.status = "failed"
                run.completed_at = _now()
                run.emit("run.failed", error=str(exc))
            finally:
                if session.active_run_holder["run"] is run:
                    session.active_run_holder["run"] = None

    def _finish_cancelled(self, session: SessionState, run: RunState) -> None:
        reply = "生成已停止。"
        run.reply = reply
        self.store.add_message(session.id, "assistant", reply)
        run.status = "cancelled"
        run.completed_at = _now()
        run.emit("run.cancelled", reply=reply)
        if session.active_run_holder["run"] is run:
            session.active_run_holder["run"] = None

    def active_run(self, session_id: str) -> RunState | None:
        run = self.get_session(session_id).active_run_holder["run"]
        if run is None or run.status in {"completed", "failed", "cancelled"}:
            return None
        return run

    def context_stats(self, session_id: str) -> dict:
        return self.get_session(session_id).agent.context_stats()

    def compact_context(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        active = session.active_run_holder["run"]
        if active is not None and active.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("任务运行中，暂时不能压缩上下文")
        with session.lock:
            result = session.agent.compact_context()
            if not result.get("compressed"):
                return result
            response = dict(result)
            summary = response.pop("summary")
            retained = response.pop("retained_plain_messages")
            self.store.save_context_summary(session_id, summary, retained)
            return response

    def cancel_run(self, run_id: str) -> RunState:
        run = self.get_run(run_id)
        run.request_cancel()
        return run

    @staticmethod
    def _resource_kind(kind: str) -> str:
        if kind not in {"skills", "styles"}:
            raise ValueError("资源类型只能是 skills 或 styles")
        return kind

    def client_resources(self, session_id: str, kind: str) -> list[dict]:
        session = self.get_session(session_id)
        kind = self._resource_kind(kind)
        session.mounted_resources = self.store.resource_mounts(session_id)
        directory = session.root / "client" / kind
        resources = []
        for path in sorted(directory.glob("*.md"), key=lambda p: p.name.casefold()):
            if not self._valid_resource(kind, path.read_text(encoding="utf-8")):
                continue
            resources.append({
                "kind": kind,
                "name": path.name,
                "mounted": path.name in session.mounted_resources[kind],
                "builtin": path.name in session.builtin_resources[kind],
            })
        return resources

    @staticmethod
    def _valid_resource(kind: str, content: str) -> bool:
        parser = parse_skill_pack_text if kind == "skills" else parse_style_text
        return parser(content) is not None

    @classmethod
    def _validate_resource(cls, kind: str, content: str) -> None:
        if not cls._valid_resource(kind, content):
            label = "Skill" if kind == "skills" else "Style"
            raise ValueError(
                f"{label} 格式无效：文件必须以 --- 开始和结束 front matter，"
                "并包含非空的 name 与 description"
            )

    def client_resource(self, session_id: str, kind: str, name: str) -> tuple[Path, bool]:
        session = self.get_session(session_id)
        kind = self._resource_kind(kind)
        clean_name = Path(name).name
        if clean_name != name or not clean_name.lower().endswith(".md"):
            raise ValueError("资源名必须是单个 .md 文件名")
        path = session.root / "client" / kind / clean_name
        if not path.is_file():
            raise FileNotFoundError(name)
        content = path.read_text(encoding="utf-8")
        self._validate_resource(kind, content)
        return path, clean_name in session.mounted_resources[kind]

    def create_client_resource(
        self, session_id: str, kind: str, name: str, content: str
    ) -> dict:
        session = self.get_session(session_id)
        kind = self._resource_kind(kind)
        clean_name = Path(name).name.strip()
        if clean_name != name or not clean_name.lower().endswith(".md"):
            raise ValueError("资源名必须是单个 .md 文件名")
        self._validate_resource(kind, content)
        directory = session.root / "client" / kind
        path = directory / clean_name
        if path.exists():
            raise ValueError(f"资源已存在：{clean_name}")
        path.write_text(content, encoding="utf-8")
        return {
            "kind": kind, "name": path.name, "mounted": False,
            "builtin": False, "content": content,
        }

    def generate_client_resource(
        self, session_id: str, kind: str, name: str, description: str
    ) -> dict:
        session = self.get_session(session_id)
        kind = self._resource_kind(kind)
        result = (
            session.diagram.create_skill(name, description)
            if kind == "skills" else session.diagram.create_style(name, description)
        )
        if "生成失败" in result or result.startswith("错误"):
            raise ValueError(result)
        path, mounted = self.client_resource(session_id, kind, f"{name.strip().lower()}.md")
        return {
            "kind": kind, "name": path.name, "mounted": mounted,
            "builtin": False, "content": path.read_text(encoding="utf-8"),
        }

    def delete_client_resource(self, session_id: str, kind: str, name: str) -> None:
        session = self.get_session(session_id)
        path, _ = self.client_resource(session_id, kind, name)
        if path.name in session.builtin_resources[kind]:
            raise ValueError("系统默认资源不能删除")
        session.mounted_resources[kind].discard(path.name)
        self.store.set_resource_mount(session_id, kind, path.name, False)
        self._deactivate_resource(session, kind, path)
        path.unlink()

    def update_client_resource(
        self,
        session_id: str,
        kind: str,
        name: str,
        *,
        content: str | None = None,
        mounted: bool | None = None,
    ) -> dict:
        session = self.get_session(session_id)
        path, current_mounted = self.client_resource(session_id, kind, name)
        if content is not None:
            self._validate_resource(kind, content)
            if current_mounted:
                self._deactivate_resource(session, kind, path)
            path.write_text(content, encoding="utf-8")
        if mounted is not None:
            if mounted:
                if kind == "styles":
                    session.mounted_resources[kind].clear()
                session.mounted_resources[kind].add(path.name)
            else:
                session.mounted_resources[kind].discard(path.name)
            self.store.set_resource_mount(session_id, kind, path.name, mounted)
            current_mounted = mounted
        if current_mounted:
            self._activate_resource(session, kind, path)
        elif mounted is False:
            self._deactivate_resource(session, kind, path)
        return {
            "kind": kind,
            "name": path.name,
            "mounted": current_mounted,
            "builtin": path.name in session.builtin_resources[kind],
            "content": path.read_text(encoding="utf-8"),
        }

    def mounted_prompt(self, session: SessionState) -> str:
        sections = []
        for kind, label in (("skills", "Skill"), ("styles", "Style")):
            for name in sorted(session.mounted_resources[kind]):
                path = session.root / "client" / kind / name
                if path.is_file():
                    content = path.read_text(encoding="utf-8")
                    parsed = (
                        parse_skill_pack_text(content)
                        if kind == "skills"
                        else parse_style_text(content)
                    )
                    # 检查 Skill 由 check 路由按任务动态发现，不进入普通对话/作图
                    # Prompt，避免审查标准污染生成上下文。
                    if kind == "skills" and getattr(parsed, "kind", "") == "check":
                        continue
                    resource_name = parsed.name if parsed is not None else path.stem
                    mandatory = (
                        f"在调用 create_diagram/modify_diagram 之前，必须先调用 "
                        f"use_skill(name={resource_name!r})；不得只在回复中声称已使用。"
                        if kind == "skills"
                        else f"调用作图工具前必须先调用 set_style(name={resource_name!r})，"
                        f"或把 style={resource_name!r} 明确传给 create_diagram。"
                    )
                    sections.append(
                        f"\n### 用户明确要求加载 {label}：{resource_name}\n"
                        f"{mandatory}\n\n{content}"
                    )
        if not sections:
            return ""
        return (
            "\n\n[系统提供的当前客户端挂载资源]"
            "\n以下资源已由用户明确勾选挂载。本轮必须视为用户明确要求使用，"
            "调用作图工具时必须遵循。作图前先判断每个挂载的 Skill 与本轮作图目标"
            "是否相关；只要有明显无关的 Skill，必须拒绝作图、点名该 Skill，并要求"
            "用户先取消挂载或更换相关 Skill，不得静默忽略；"
            "若与用户本轮明确要求冲突，以用户本轮要求为准。\n"
            + "\n".join(sections)
        )

    def _activate_resource(self, session: SessionState, kind: str, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        if kind == "skills":
            pack = parse_skill_pack_text(content)
            if pack is not None:
                session.diagram.use_skill(pack.name)
        else:
            style = parse_style_text(content)
            if style is not None:
                result = session.diagram.set_style(style.name)
                if result.startswith("错误"):
                    raise ValueError(result)

    def _deactivate_resource(self, session: SessionState, kind: str, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return
        if kind == "skills":
            pack = parse_skill_pack_text(content)
            if pack is not None:
                session.diagram.unuse_skill(pack.name)
        else:
            style = parse_style_text(content)
            if (
                style is not None
                and session.diagram.style is not None
                and session.diagram.style.name == style.name
            ):
                session.diagram.style = None

    def _activate_mounted_resources(self, session: SessionState) -> list[dict]:
        activated = []
        for kind in ("skills", "styles"):
            missing = []
            for name in sorted(session.mounted_resources[kind]):
                path = session.root / "client" / kind / name
                if not path.is_file():
                    missing.append(name)
                    continue
                self._activate_resource(session, kind, path)
                content = path.read_text(encoding="utf-8")
                parsed = (
                    parse_skill_pack_text(content)
                    if kind == "skills"
                    else parse_style_text(content)
                )
                activated.append({
                    "kind": kind,
                    "name": parsed.name if parsed is not None else path.stem,
                    "filename": path.name,
                })
            for name in missing:
                session.mounted_resources[kind].discard(name)
                self.store.set_resource_mount(session.id, kind, name, False)
        return activated

    def get_run(self, run_id: str) -> RunState:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError(f"未知 Run：{run_id}") from exc

    def save_file(self, session_id: str, filename: str, content: bytes) -> tuple[str, Path]:
        session = self.get_session(session_id)
        file_id = f"file_{uuid.uuid4().hex}"
        clean_name = Path(filename).name.strip()
        if not clean_name:
            raise ValueError("文件名不能为空")
        directory = session.root / "attachments"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / clean_name
        stem, suffix, counter = path.stem, path.suffix, 1
        while path.exists():
            path = directory / f"{stem}_{counter}{suffix}"
            counter += 1
        path.write_bytes(content)
        session.files[file_id] = path
        return file_id, path

    def attach_workspace_file(self, session_id: str, relative_path: str) -> tuple[str, Path]:
        session = self.get_session(session_id)
        path = self.workspace_file(session_id, relative_path)
        file_id = f"file_{uuid.uuid4().hex}"
        session.files[file_id] = path
        return file_id, path

    def artifacts(self, session_id: str) -> list[dict]:
        session = self.get_session(session_id)
        result = []
        for path in sorted(session.root.rglob("*")):
            if not path.is_file() or "uploads" in path.parts:
                continue
            relative = path.relative_to(session.root).as_posix()
            artifact_id = hashlib.sha256(relative.encode()).hexdigest()[:20]
            result.append(
                {
                    "id": artifact_id,
                    "name": relative,
                    "kind": _artifact_kind(path),
                    "size": path.stat().st_size,
                    "download_url": f"/v1/sessions/{session_id}/artifacts/{artifact_id}/content",
                    "_path": path,
                }
            )
        return result

    def artifact(self, session_id: str, artifact_id: str) -> tuple[Path, str]:
        for item in self.artifacts(session_id):
            if item["id"] == artifact_id:
                path = item["_path"]
                return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        raise KeyError(f"未知产物：{artifact_id}")

    def current_diagram(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        artifacts = self.artifacts(session_id)

        def current_id(kind: str) -> str | None:
            matches = [a for a in artifacts if a["kind"] == kind and Path(a["name"]).name.startswith("current")]
            return matches[0]["id"] if matches else None

        return {
            "version": session.diagram.version,
            "engine": session.diagram.engine,
            "source": session.diagram.current_code,
            "source_artifact_id": current_id("source"),
            "image_artifact_id": current_id("image"),
            "svg_artifact_id": current_id("svg"),
        }

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from .models import (
    ArtifactView,
    ClientResourceUpdate,
    ClientResourceGenerate,
    ClientResourceView,
    DiagramView,
    FileView,
    RunCreate,
    RunView,
    SessionCreate,
    SessionPatch,
    SessionView,
    WorkspaceFileView,
    WorkspaceAttachmentCreate,
    WorkspaceEntryCreate,
    WorkspaceTransfer,
    WorkspaceNode,
    AuthInput,
    UserView,
    SessionTitlePatch,
    MessageView,
)
from .service import AgentService, RunState, SessionState
from .storage import Store


STATIC_DIR = Path(__file__).with_name("static")
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _avatar_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _session_view(session: SessionState) -> SessionView:
    return SessionView(
        id=session.id,
        engine=session.diagram.engine,
        verification_mode=session.diagram.verify_mode,
        style=session.diagram.style.name if session.diagram.style else None,
        version=session.diagram.version,
        has_diagram=session.diagram.has_diagram,
        created_at=session.created_at,
        title=session.title,
    )


def _run_view(run: RunState) -> RunView:
    return RunView(
        id=run.id,
        session_id=run.session_id,
        status=run.status,
        created_at=run.created_at,
        completed_at=run.completed_at,
        reply=run.reply,
        error=run.error,
    )


def create_app(
    settings: Settings,
    data_root: str | Path = "server_data",
    workspace_root: str | Path = "output",
) -> FastAPI:
    app = FastAPI(
        title="Flowchart Agent API",
        version=__version__,
        description=(
            "Flowchart Agent 的服务化 API。用户、Session 与对话历史保存在 SQLite；"
            "附件、生成日志和图表产物保存在用户与 Session 隔离的服务端目录。"
        ),
    )
    store = Store(Path(data_root) / "flowchart.db")
    service = AgentService(settings, data_root, workspace_root=workspace_root, store=store)
    app.state.agent_service = service
    app.state.store = store
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        path = request.url.path
        public = path in {"/", "/health", "/openapi.json", "/v1/auth/register", "/v1/auth/login"} or path.startswith("/static/")
        user = store.user_for_token(request.cookies.get("flowchart_auth"))
        request.state.user = user
        if path.startswith("/v1/") and not public and user is None:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        match = re.match(r"/v1/sessions/([^/]+)", path)
        if match and user and store.session(match.group(1), user["id"]) is None:
            return JSONResponse({"detail": "Session 不存在或无权访问"}, status_code=404)
        return await call_next(request)

    def get_session(session_id: str) -> SessionState:
        try:
            return service.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    def get_run(run_id: str) -> RunState:
        try:
            return service.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def web_app() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/auth/register", response_model=UserView, status_code=201, tags=["auth"])
    def register(payload: AuthInput, response: Response) -> UserView:
        try:
            user = store.create_user(payload.username, payload.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        response.set_cookie("flowchart_auth", store.issue_token(user["id"]), httponly=True,
                            samesite="lax", secure=False, max_age=14 * 86400)
        return UserView(**user)

    @app.post("/v1/auth/login", response_model=UserView, tags=["auth"])
    def login(payload: AuthInput, response: Response) -> UserView:
        user = store.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(401, "用户名或密码错误")
        response.set_cookie("flowchart_auth", store.issue_token(user["id"]), httponly=True,
                            samesite="lax", secure=False, max_age=14 * 86400)
        return UserView(**user)

    @app.get("/v1/auth/me", response_model=UserView, tags=["auth"])
    def me(request: Request) -> UserView:
        return UserView(**request.state.user)

    @app.get("/v1/auth/avatar", tags=["auth"])
    def avatar(request: Request) -> Response:
        stored = store.user_avatar(request.state.user["id"])
        if stored is None:
            raise HTTPException(404, "尚未设置头像")
        content, media_type = stored
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.put("/v1/auth/avatar", response_model=UserView, tags=["auth"])
    async def update_avatar(request: Request) -> UserView:
        content = await request.body()
        if not content:
            raise HTTPException(400, "头像文件不能为空")
        if len(content) > MAX_AVATAR_BYTES:
            raise HTTPException(413, "头像不能超过 2 MB")
        media_type = _avatar_media_type(content)
        if media_type is None:
            raise HTTPException(400, "头像仅支持 PNG、JPEG、WebP 或 GIF")
        store.set_user_avatar(request.state.user["id"], content, media_type)
        return UserView(
            id=request.state.user["id"],
            username=request.state.user["username"],
            avatar_url="/v1/auth/avatar",
        )

    @app.delete("/v1/auth/avatar", response_model=UserView, tags=["auth"])
    def delete_avatar(request: Request) -> UserView:
        store.set_user_avatar(request.state.user["id"], None)
        return UserView(
            id=request.state.user["id"],
            username=request.state.user["username"],
            avatar_url=None,
        )

    @app.post("/v1/auth/logout", status_code=204, tags=["auth"])
    def logout(request: Request, response: Response) -> None:
        store.revoke_token(request.cookies.get("flowchart_auth"))
        response.delete_cookie("flowchart_auth")

    @app.get(
        "/v1/sessions/{session_id}/workspace/tree",
        response_model=list[WorkspaceNode],
        tags=["workspace"],
    )
    def workspace_tree(session_id: str) -> list[WorkspaceNode]:
        get_session(session_id)
        return [WorkspaceNode(**node) for node in service.workspace_tree(session_id)]

    @app.get(
        "/v1/sessions/{session_id}/client/{kind}",
        response_model=list[ClientResourceView],
        tags=["client resources"],
    )
    def list_client_resources(session_id: str, kind: str) -> list[ClientResourceView]:
        get_session(session_id)
        try:
            return [ClientResourceView(**item) for item in service.client_resources(session_id, kind)]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get(
        "/v1/sessions/{session_id}/client/{kind}/{name}",
        response_model=ClientResourceView,
        tags=["client resources"],
    )
    def read_client_resource(session_id: str, kind: str, name: str) -> ClientResourceView:
        get_session(session_id)
        try:
            path, mounted = service.client_resource(session_id, kind, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"资源不存在：{name}") from exc
        return ClientResourceView(
            kind=kind, name=path.name, mounted=mounted,
            builtin=path.name in get_session(session_id).builtin_resources[kind],
            content=path.read_text(encoding="utf-8"),
        )

    @app.post(
        "/v1/sessions/{session_id}/client/{kind}",
        response_model=ClientResourceView,
        status_code=201,
        tags=["client resources"],
    )
    async def create_client_resource(
        session_id: str, kind: str, request: Request, filename: str = Query(min_length=1)
    ) -> ClientResourceView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "会话正在执行 Run，暂时不能修改挂载资源")
        try:
            content = (await request.body()).decode("utf-8")
            return ClientResourceView(**service.create_client_resource(
                session_id, kind, filename, content
            ))
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "资源文件必须使用 UTF-8 编码") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post(
        "/v1/sessions/{session_id}/client/{kind}/generate",
        response_model=ClientResourceView,
        status_code=201,
        tags=["client resources"],
    )
    def generate_client_resource(
        session_id: str, kind: str, payload: ClientResourceGenerate
    ) -> ClientResourceView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "会话正在执行 Run，暂时不能生成资源")
        try:
            return ClientResourceView(**service.generate_client_resource(
                session_id, kind, payload.name, payload.description
            ))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch(
        "/v1/sessions/{session_id}/client/{kind}/{name}",
        response_model=ClientResourceView,
        tags=["client resources"],
    )
    def update_client_resource(
        session_id: str, kind: str, name: str, payload: ClientResourceUpdate
    ) -> ClientResourceView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "会话正在执行 Run，暂时不能修改挂载资源")
        try:
            return ClientResourceView(**service.update_client_resource(
                session_id, kind, name, content=payload.content, mounted=payload.mounted
            ))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"资源不存在：{name}") from exc

    @app.delete(
        "/v1/sessions/{session_id}/client/{kind}/{name}",
        status_code=204,
        tags=["client resources"],
    )
    def delete_client_resource(session_id: str, kind: str, name: str) -> None:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "会话正在执行 Run，暂时不能修改挂载资源")
        try:
            service.delete_client_resource(session_id, kind, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"资源不存在：{name}") from exc

    @app.get("/v1/sessions/{session_id}/workspace/files/content", tags=["workspace"])
    def workspace_file(session_id: str, path: str = Query(min_length=1)):
        try:
            file_path = service.workspace_file(session_id, path)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"文件不存在：{path}") from exc
        return FileResponse(file_path, media_type=None)

    @app.get("/v1/sessions/{session_id}/workspace/files/download", tags=["workspace"])
    def download_workspace_file(session_id: str, path: str = Query(min_length=1)):
        try:
            file_path = service.workspace_file(session_id, path)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"文件不存在：{path}") from exc
        return FileResponse(file_path, filename=file_path.name, media_type="application/octet-stream")

    @app.post(
        "/v1/sessions/{session_id}/workspace/entries",
        response_model=WorkspaceFileView,
        status_code=201,
        tags=["workspace"],
    )
    def create_workspace_entry(session_id: str, payload: WorkspaceEntryCreate) -> WorkspaceFileView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "Session 正在运行，暂时不能修改文件")
        try:
            path = service.create_workspace_entry(session_id, payload.path, payload.type)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return WorkspaceFileView(path=path.relative_to(session.root).as_posix(), filename=path.name,
                                 size=path.stat().st_size if path.is_file() else 0)

    @app.post(
        "/v1/sessions/{session_id}/workspace/transfer",
        response_model=WorkspaceFileView,
        tags=["workspace"],
    )
    def transfer_workspace_entry(session_id: str, payload: WorkspaceTransfer) -> WorkspaceFileView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "Session 正在运行，暂时不能修改文件")
        try:
            path = service.transfer_workspace_entry(
                session_id, payload.source, payload.target, payload.operation
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "源文件或目录不存在") from exc
        return WorkspaceFileView(path=path.relative_to(session.root).as_posix(), filename=path.name,
                                 size=path.stat().st_size if path.is_file() else 0)

    @app.delete("/v1/sessions/{session_id}/workspace/entries", status_code=204, tags=["workspace"])
    def delete_workspace_entry(session_id: str, path: str = Query(min_length=1)) -> None:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "Session 正在运行，暂时不能修改文件")
        try:
            service.delete_workspace_entry(session_id, path)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"文件或目录不存在：{path}") from exc

    @app.post(
        "/v1/sessions/{session_id}/workspace/files",
        response_model=WorkspaceFileView,
        status_code=201,
        tags=["workspace"],
    )
    async def upload_workspace_file(
        session_id: str, request: Request, filename: str = Query(min_length=1)
    ) -> WorkspaceFileView:
        content = await request.body()
        if not content:
            raise HTTPException(400, "文件内容不能为空")
        try:
            path = service.save_workspace_file(session_id, filename, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return WorkspaceFileView(
            path=path.relative_to(get_session(session_id).root).as_posix(),
            filename=path.name,
            size=path.stat().st_size,
        )

    @app.get("/v1/sessions", tags=["sessions"])
    def list_sessions(request: Request) -> list[dict]:
        return store.sessions(request.state.user["id"])

    @app.post("/v1/sessions", response_model=SessionView, status_code=201, tags=["sessions"])
    def create_session(payload: SessionCreate, request: Request) -> SessionView:
        try:
            session = service.create_session(
                user_id=request.state.user["id"],
                engine=payload.engine,
                verification_mode=payload.verification_mode,
                style=payload.style,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _session_view(session)

    @app.patch("/v1/sessions/{session_id}/title", status_code=204, tags=["sessions"])
    def rename_session(session_id: str, payload: SessionTitlePatch, request: Request) -> None:
        store.rename_session(session_id, request.state.user["id"], payload.title)
        if session_id in service.sessions:
            service.sessions[session_id].title = payload.title.strip()

    @app.delete("/v1/sessions/{session_id}", status_code=204, tags=["sessions"])
    def delete_session(session_id: str, request: Request) -> None:
        session = service.sessions.get(session_id)
        if session and session.lock.locked():
            raise HTTPException(409, "Session 正在运行")
        store.delete_session(session_id, request.state.user["id"])
        service.sessions.pop(session_id, None)
        if session and session.root.is_dir():
            import shutil
            shutil.rmtree(session.root)

    @app.get("/v1/sessions/{session_id}/messages", response_model=list[MessageView], tags=["sessions"])
    def session_messages(session_id: str) -> list[MessageView]:
        rows = store.messages(session_id)
        return [MessageView(role=row["role"], content=row["content"],
                            attachments=json.loads(row["attachments"]), created_at=row["created_at"])
                for row in rows]

    @app.get("/v1/sessions/{session_id}", response_model=SessionView, tags=["sessions"])
    def read_session(session_id: str) -> SessionView:
        return _session_view(get_session(session_id))

    @app.patch("/v1/sessions/{session_id}", response_model=SessionView, tags=["sessions"])
    def update_session(session_id: str, payload: SessionPatch) -> SessionView:
        session = get_session(session_id)
        if session.lock.locked():
            raise HTTPException(409, "会话正在执行 Run，暂时不能修改配置")
        if payload.engine:
            result = session.diagram.set_output_engine(payload.engine)
            if result.startswith("错误") or result.startswith("暂时无法"):
                raise HTTPException(400, result)
        if payload.verification_mode:
            session.diagram.set_verify_mode(payload.verification_mode)
        if payload.style:
            result = session.diagram.set_style(payload.style)
            if result.startswith("错误"):
                raise HTTPException(400, result)
        return _session_view(session)

    @app.post(
        "/v1/sessions/{session_id}/runs",
        response_model=RunView,
        status_code=202,
        tags=["runs"],
    )
    def create_run(session_id: str, payload: RunCreate) -> RunView:
        get_session(session_id)
        try:
            run = service.create_run(session_id, payload.input, payload.attachments)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _run_view(run)

    @app.get("/v1/runs/{run_id}", response_model=RunView, tags=["runs"])
    def read_run(run_id: str, request: Request) -> RunView:
        run = get_run(run_id)
        if store.session(run.session_id, request.state.user["id"]) is None:
            raise HTTPException(404, "Run 不存在或无权访问")
        return _run_view(run)

    @app.get(
        "/v1/sessions/{session_id}/active-run",
        response_model=RunView | None,
        tags=["runs"],
    )
    def active_run(session_id: str) -> RunView | None:
        run = service.active_run(session_id)
        return _run_view(run) if run else None

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunView, tags=["runs"])
    def cancel_run(run_id: str, request: Request) -> RunView:
        run = get_run(run_id)
        if store.session(run.session_id, request.state.user["id"]) is None:
            raise HTTPException(404, "Run 不存在或无权访问")
        return _run_view(service.cancel_run(run_id))

    @app.get("/v1/runs/{run_id}/events", tags=["runs"])
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        run = get_run(run_id)
        if store.session(run.session_id, request.state.user["id"]) is None:
            raise HTTPException(404, "Run 不存在或无权访问")
        cursor = last_event_id if last_event_id is not None else after

        async def generate():
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                events = run.events_after(cursor)
                for event in events:
                    cursor = event["id"]
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
                if run.status in {"completed", "failed", "cancelled"} and not run.events_after(cursor):
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/sessions/{session_id}/files",
        response_model=FileView,
        status_code=201,
        tags=["files"],
    )
    async def upload_file(
        session_id: str,
        request: Request,
        filename: str = Query(min_length=1),
    ) -> FileView:
        get_session(session_id)
        content = await request.body()
        if not content:
            raise HTTPException(400, "文件内容不能为空")
        try:
            file_id, path = service.save_file(session_id, filename, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileView(id=file_id, filename=Path(filename).name, size=path.stat().st_size)

    @app.post(
        "/v1/sessions/{session_id}/files/from-workspace",
        response_model=FileView,
        status_code=201,
        tags=["files"],
    )
    def attach_workspace_file(
        session_id: str, payload: WorkspaceAttachmentCreate
    ) -> FileView:
        get_session(session_id)
        try:
            file_id, path = service.attach_workspace_file(session_id, payload.path)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, f"文件不存在：{payload.path}") from exc
        return FileView(id=file_id, filename=path.name, size=path.stat().st_size)

    @app.get(
        "/v1/sessions/{session_id}/artifacts",
        response_model=list[ArtifactView],
        tags=["artifacts"],
    )
    def list_artifacts(session_id: str) -> list[ArtifactView]:
        get_session(session_id)
        return [ArtifactView(**{k: v for k, v in item.items() if not k.startswith("_")})
                for item in service.artifacts(session_id)]

    @app.get(
        "/v1/sessions/{session_id}/artifacts/{artifact_id}/content",
        tags=["artifacts"],
    )
    def download_artifact(session_id: str, artifact_id: str):
        get_session(session_id)
        try:
            path, media_type = service.artifact(session_id, artifact_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get(
        "/v1/sessions/{session_id}/diagram",
        response_model=DiagramView,
        tags=["artifacts"],
    )
    def current_diagram(session_id: str) -> DiagramView:
        get_session(session_id)
        return DiagramView(**service.current_diagram(session_id))

    return app

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    engine: Literal["mermaid", "drawio"] | None = None
    verification_mode: Literal["full", "layout", "code"] | None = None
    style: str | None = None


class SessionPatch(SessionCreate):
    pass


class SessionView(BaseModel):
    id: str
    engine: str
    verification_mode: str
    style: str | None
    version: int
    has_diagram: bool
    created_at: str
    title: str = "未命名会话"


class AuthInput(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=256)


class UserView(BaseModel):
    id: str
    username: str
    avatar_url: str | None = None


class ContextView(BaseModel):
    used_tokens: int
    message_tokens: int
    tool_tokens: int
    limit_tokens: int
    percent: float
    message_count: int
    compressed: bool = False
    before_tokens: int | None = None
    reason: str | None = None
    cleared: bool = False


class SessionTitlePatch(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class MessageView(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    attachments: list[str] = Field(default_factory=list)
    created_at: str


class RunCreate(BaseModel):
    input: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)


class RunView(BaseModel):
    id: str
    session_id: str
    status: Literal["queued", "running", "cancelling", "completed", "failed", "cancelled"]
    created_at: str
    completed_at: str | None = None
    reply: str | None = None
    error: str | None = None


class EventView(BaseModel):
    id: int
    run_id: str
    session_id: str
    type: str
    timestamp: str
    data: dict[str, Any]


class FileView(BaseModel):
    id: str
    filename: str
    size: int


class WorkspaceFileView(BaseModel):
    path: str
    filename: str
    size: int


class WorkspaceAttachmentCreate(BaseModel):
    path: str = Field(min_length=1)


class WorkspaceEntryCreate(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    type: Literal["file", "directory"]


class WorkspaceTransfer(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    operation: Literal["copy", "move"]


class ArtifactView(BaseModel):
    id: str
    name: str
    kind: str
    size: int
    download_url: str


class DiagramView(BaseModel):
    version: int
    engine: str
    source: str
    source_artifact_id: str | None
    image_artifact_id: str | None
    svg_artifact_id: str | None


class WorkspaceNode(BaseModel):
    name: str
    path: str
    type: Literal["directory", "file"]
    size: int | None = None
    children: list["WorkspaceNode"] = Field(default_factory=list)


class ClientResourceView(BaseModel):
    kind: Literal["skills", "styles"]
    name: str
    mounted: bool
    builtin: bool
    content: str | None = None


class ClientResourceUpdate(BaseModel):
    content: str | None = None
    mounted: bool | None = None


class ClientResourceGenerate(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)

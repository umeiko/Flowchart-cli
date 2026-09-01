from __future__ import annotations

import base64
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flowchart_agent.config import ModelConfig, Settings
from flowchart_agent.agent import FlowchartAgent
from flowchart_agent import agent as agent_module
from flowchart_agent import cli as cli_module
from flowchart_agent.server import create_app
from flowchart_agent.server.storage import Store
from flowchart_agent.session import DiagramSession
from flowchart_agent.skillpacks import parse_skill_pack_text
from flowchart_agent.skills.builtin import build_skills, find_files, read_document
from flowchart_agent.styles import parse_style_text


def _settings() -> Settings:
    return Settings(
        text_model=ModelConfig(name="test", api_key="test", base_url="http://localhost/v1"),
        output_engine="mermaid",
        verify_mode="code",
    )


def _register(client: TestClient, username: str = "tester") -> None:
    response = client.post(
        "/v1/auth/register", json={"username": username, "password": "test-password"}
    )
    assert response.status_code == 201


def test_store_migrates_existing_users_for_avatars(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
        )

    store = Store(database)
    with store.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}

    assert {"avatar", "avatar_mime"} <= columns


def test_server_cli_reads_startup_parameters_from_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            "TEXT_MODEL_NAME=test",
            "TEXT_MODEL_API_KEY=test",
            "TEXT_MODEL_BASE_URL=http://localhost/v1",
            "SERVER_HOST=0.0.0.0",
            "SERVER_PORT=9876",
            "SERVER_OUTPUT=web-output",
            "SERVER_DATA_DIR=web-data",
        ]),
        encoding="utf-8",
    )
    for key, value in {
        "TEXT_MODEL_NAME": "test",
        "TEXT_MODEL_API_KEY": "test",
        "TEXT_MODEL_BASE_URL": "http://localhost/v1",
        "SERVER_HOST": "0.0.0.0",
        "SERVER_PORT": "9876",
        "SERVER_OUTPUT": "web-output",
        "SERVER_DATA_DIR": "web-data",
    }.items():
        monkeypatch.setenv(key, value)
    captured = {}
    monkeypatch.setattr(cli_module, "register_fonts", lambda: None)
    monkeypatch.setattr(cli_module, "check_font_available", lambda _font: None)
    monkeypatch.setattr(
        cli_module,
        "_run_server",
        lambda settings, host, port, output, data: captured.update(
            host=host, port=port, output=output, data=data
        ) or 0,
    )

    assert cli_module.main(["server", "--env", str(env_path)]) == 0
    assert captured == {
        "host": "0.0.0.0",
        "port": 9876,
        "output": Path("web-output"),
        "data": Path("web-data"),
    }


def test_server_mvp_flow(tmp_path):
    workspace = tmp_path / "output"
    workspace.mkdir()
    app = create_app(_settings(), data_root=tmp_path / "data", workspace_root=workspace)
    client = TestClient(app)
    _register(client)

    assert client.get("/v1/auth/me").json()["avatar_url"] is None
    assert client.put(
        "/v1/auth/avatar", content=b"not-an-image", headers={"content-type": "image/png"}
    ).status_code == 400
    avatar_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    avatar = client.put(
        "/v1/auth/avatar", content=avatar_bytes, headers={"content-type": "image/png"}
    )
    assert avatar.status_code == 200
    assert avatar.json()["avatar_url"] == "/v1/auth/avatar"
    downloaded_avatar = client.get("/v1/auth/avatar")
    assert downloaded_avatar.content == avatar_bytes
    assert downloaded_avatar.headers["content-type"] == "image/png"
    assert client.get("/v1/auth/me").json()["avatar_url"] == "/v1/auth/avatar"
    assert client.delete("/v1/auth/avatar").json()["avatar_url"] is None
    assert client.get("/v1/auth/avatar").status_code == 404

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "Flowchart Agent API"
    session = client.post("/v1/sessions", json={}).json()
    session_id = session["id"]
    base = f"/v1/sessions/{session_id}/workspace"
    notes = client.post(f"{base}/files?filename=notes.md", content=b"# Notes")
    assert notes.status_code == 201
    tree = client.get(f"{base}/tree").json()
    assert tree[0]["path"] == "workspace"
    assert tree[0]["children"][0]["path"] == "workspace/notes.md"
    assert client.get(f"{base}/files/content?path=workspace/notes.md").text == "# Notes"
    assert client.get(f"{base}/files/content?path=../outside.txt").status_code == 403
    dropped = client.post(
        f"{base}/files?filename=diagram.xml", content=b"<diagram />"
    )
    assert dropped.status_code == 201
    assert dropped.json()["path"] == "workspace/diagram.xml"
    duplicate = client.post(
        f"{base}/files?filename=diagram.xml", content=b"<diagram id='2' />"
    )
    assert duplicate.json()["path"] == "workspace/diagram_1.xml"

    created_dir = client.post(f"{base}/entries", json={"path": "workspace/docs", "type": "directory"})
    assert created_dir.status_code == 201
    empty_file = client.post(f"{base}/entries", json={"path": "workspace/docs/empty.txt", "type": "file"})
    assert empty_file.status_code == 201
    copied = client.post(f"{base}/transfer", json={
        "source": "workspace/docs/empty.txt", "target": "workspace/empty-copy.txt", "operation": "copy"
    })
    assert copied.status_code == 200
    moved = client.post(f"{base}/transfer", json={
        "source": "workspace/empty-copy.txt", "target": "workspace/docs/renamed.txt", "operation": "move"
    })
    assert moved.status_code == 200
    assert client.get(f"{base}/files/download?path=workspace/docs/renamed.txt").status_code == 200
    assert client.delete(f"{base}/entries?path=workspace/docs").status_code == 204
    assert client.post(f"{base}/transfer", json={
        "source": "workspace", "target": "workspace-renamed", "operation": "move"
    }).status_code == 400
    assert client.post(f"{base}/entries", json={"path": "../escape.txt", "type": "file"}).status_code == 400

    assert session["engine"] == "mermaid"
    attached_existing = client.post(
        f"/v1/sessions/{session_id}/files/from-workspace", json={"path": "workspace/notes.md"}
    )
    assert attached_existing.status_code == 201
    assert attached_existing.json()["filename"] == "notes.md"
    skills = client.get(f"/v1/sessions/{session_id}/client/skills").json()
    assert skills
    assert all(item["name"].lower() != "readme.md" for item in skills)
    assert all(item["builtin"] is True for item in skills)
    skill_name = skills[0]["name"]
    resource = client.get(
        f"/v1/sessions/{session_id}/client/skills/{skill_name}"
    ).json()
    edited_content = (resource["content"] or "") + "\nTEST_MOUNTED_SKILL"
    updated = client.patch(
        f"/v1/sessions/{session_id}/client/skills/{skill_name}",
        json={"content": edited_content, "mounted": True},
    )
    assert updated.status_code == 200
    assert updated.json()["mounted"] is True
    state = app.state.agent_service.get_session(session_id)
    assert "flowchart_format" in state.diagram.list_skill_packs()
    assert "default" in state.diagram.list_styles()

    invalid = client.post(
        f"/v1/sessions/{session_id}/client/skills?filename=invalid.md",
        content=b"# no front matter",
    )
    assert invalid.status_code == 400
    created_resource = client.post(
        f"/v1/sessions/{session_id}/client/skills?filename=custom.md",
        content=b"---\nname: custom\ndescription: custom skill\n---\n\nDo it.",
    )
    assert created_resource.status_code == 201
    assert created_resource.json()["builtin"] is False
    assert "custom" in state.diagram.list_skill_packs()
    assert client.delete(
        f"/v1/sessions/{session_id}/client/skills/custom.md"
    ).status_code == 204
    assert client.delete(
        f"/v1/sessions/{session_id}/client/skills/{skill_name}"
    ).status_code == 400

    uploaded = client.post(
        f"/v1/sessions/{session_id}/files?filename=brief.md",
        content=b"login flow",
        headers={"content-type": "text/markdown"},
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]
    assert (state.root / "attachments" / "brief.md").read_bytes() == b"login flow"
    assert not (workspace / "brief.md").exists()

    captured = {}

    def fake_chat(prompt, images=None):
        captured["prompt"] = prompt
        intermediate = state.root / "generate" / "intermediate.txt"
        intermediate.parent.mkdir(parents=True, exist_ok=True)
        intermediate.write_text("working", encoding="utf-8")
        state.diagram.on_stage("rendering", "正在渲染")
        return "mock reply"

    state.agent.chat = fake_chat
    created = client.post(
        f"/v1/sessions/{session_id}/runs",
        json={"input": "create", "attachments": [file_id]},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]

    for _ in range(100):
        run = client.get(f"/v1/runs/{run_id}").json()
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert run["status"] == "completed"
    assert run["reply"] == "mock reply"
    sessions = client.get("/v1/sessions").json()
    assert sessions[0]["title"] == "create"
    assert "brief.md" in captured["prompt"]
    assert "必须先用 read_document" in captured["prompt"]
    assert "TEST_MOUNTED_SKILL" in captured["prompt"]
    assert "必须先调用 use_skill" in captured["prompt"]
    events = app.state.agent_service.get_run(run_id).events
    assert [event["type"] for event in events] == [
        "run.queued",
        "run.started",
        "resource.activated",
        "generation.stage",
        "workspace.changed",
        "run.completed",
    ]
    workspace_event = next(event for event in events if event["type"] == "workspace.changed")
    assert workspace_event["data"] == {
        "reason": "rendering",
        "refresh_diagram": False,
    }


def test_unknown_attachment_is_rejected(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client)
    session_id = client.post("/v1/sessions", json={}).json()["id"]

    response = client.post(
        f"/v1/sessions/{session_id}/runs",
        json={"input": "create", "attachments": ["file_missing"]},
    )

    assert response.status_code == 400


def test_tool_call_details_keep_request_and_full_result(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client, "tool-detail-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)
    full_result = "RESULT-" + ("x" * 2500)

    def fake_chat(_prompt, images=None):
        state.agent._on_tool_call("read_document", '{"path":"workspace/brief.md"}')
        state.agent._on_tool_result("read_document", full_result)
        return "done"

    state.agent.chat = fake_chat
    created = client.post(
        f"/v1/sessions/{session_id}/runs", json={"input": "inspect tool"}
    )
    run_id = created.json()["id"]
    for _ in range(100):
        run = client.get(f"/v1/runs/{run_id}").json()
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    events = app.state.agent_service.get_run(run_id).events
    started = next(event for event in events if event["type"] == "tool.started")
    completed = next(event for event in events if event["type"] == "tool.completed")
    assert started["data"]["arguments"] == '{"path":"workspace/brief.md"}'
    assert completed["data"]["result"] == full_result

    html = client.get("/").text
    script = client.get("/static/app.js").text
    assert 'id="tool-detail-dialog"' in html
    assert "openToolDetail(action)" in script
    assert "toolDetailRequest.textContent" in script
    assert "toolDetailResult.textContent" in script
    assert "ui.messages.insertBefore(node, beforeNode)" in script
    assert 'payload.data.arguments ?? "", assistant' in script


def test_create_diagram_can_skip_visual_verification(tmp_path, monkeypatch):
    session = DiagramSession(_settings(), tmp_path / "generate")
    captured = {}

    def fake_run(*args, **kwargs):
        captured["verify_mode"] = kwargs["verify_mode"]
        return SimpleNamespace(
            success=True,
            cancelled=False,
            mermaid_code="flowchart TD\nA-->B",
            image_path=None,
            rounds=[SimpleNamespace(image_path=None)],
        )

    monkeypatch.setattr(session._agent, "run", fake_run)
    monkeypatch.setattr(session, "_publish", lambda code, image, run_dir, note: note)

    reply = session.create("简单画一下，尽快给我", visual_verification=False)

    assert captured["verify_mode"] == "none"
    assert "已按要求跳过视觉验证" in reply
    images = SimpleNamespace(add=lambda path: path)
    tools = {item.name: item for item in build_skills(session, images)}
    parameter = tools["create_diagram"].parameters["properties"]["visual_verification"]
    assert parameter["type"] == "boolean"
    assert parameter["default"] is True


def test_modify_diagram_can_skip_visual_verification(tmp_path, monkeypatch):
    session = DiagramSession(_settings(), tmp_path / "generate")
    session.current_code = "flowchart TD\nA-->B"
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return "modified"

    monkeypatch.setattr(session, "_run", fake_run)
    reply = session.modify("快速改成绿色", visual_verification=False)

    assert reply == "modified"
    assert captured["visual_verification"] is False
    images = SimpleNamespace(add=lambda path: path)
    tools = {item.name: item for item in build_skills(session, images)}
    parameter = tools["modify_diagram"].parameters["properties"]["visual_verification"]
    assert parameter["type"] == "boolean"
    assert parameter["default"] is True


def test_agent_skips_review_after_successful_render(tmp_path, monkeypatch):
    agent = FlowchartAgent(_settings())
    stages = []

    monkeypatch.setattr(
        agent,
        "_generate",
        lambda *args, **kwargs: ("flowchart TD\nA-->B", "raw model output"),
    )

    def fail_if_verified(*args, **kwargs):
        raise AssertionError("verification should not run")

    monkeypatch.setattr(agent, "_verify", fail_if_verified)

    def fake_render(code, output_dir, stem, fmt, **kwargs):
        path = Path(output_dir) / f"{stem}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return SimpleNamespace(ok=True, image_path=path, svg_path=None, error="")

    monkeypatch.setattr(agent_module, "render_mermaid", fake_render)
    result = agent.run(
        "简单流程",
        tmp_path / "run",
        verify_mode="none",
        on_stage=lambda stage, message: stages.append((stage, message)),
    )

    assert result.success is True
    assert len(result.rounds) == 1
    assert result.rounds[0].feedback == "已按要求跳过视觉验证"
    assert any(stage == "completed" for stage, _ in stages)
    assert not (tmp_path / "run" / "round_1_verify_raw.txt").exists()


def test_server_agent_file_tools_cannot_escape_current_session(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client, "sandbox-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    other_session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)
    other_state = app.state.agent_service.get_session(other_session_id)

    allowed = state.root / "workspace" / "inside.md"
    allowed.write_text("SESSION_ONLY_MARKER", encoding="utf-8")
    server_secret = tmp_path / "server-secret.txt"
    server_secret.write_text("SERVER_SECRET_MARKER", encoding="utf-8")
    other_secret = other_state.root / "workspace" / "other-user.md"
    other_secret.write_text("OTHER_SESSION_MARKER", encoding="utf-8")

    tools = state.agent._skills
    assert tools["read_document"].handler(path="workspace/inside.md") == "SESSION_ONLY_MARKER"
    find_result = tools["find_files"].handler(keyword="inside")
    grep_result = tools["grep_files"].handler(pattern="SESSION_ONLY")
    assert "inside.md" in find_result and "size=" in find_result
    assert "SESSION_ONLY_MARKER" in grep_result and "[19 B]" in grep_result
    assert "delegate_task" in tools
    assert "32KB" in tools["read_document"].description
    assert state.agent._subagent.tool_names == {
        "read_document", "find_files", "grep_files", "write_file", "replace_in_file"
    }
    assert state.agent._subagent._run_lock.acquire(blocking=False)
    try:
        assert "已有一个子 Agent" in state.agent._subagent.run("读取 inside.md")
    finally:
        state.agent._subagent._run_lock.release()

    for forbidden in (server_secret, other_secret):
        result = tools["read_document"].handler(path=str(forbidden))
        assert result.startswith("错误：只允许读取当前 Session")
    assert tools["find_files"].handler(
        keyword="server-secret", directory=str(tmp_path)
    ).startswith("错误：只允许读取当前 Session")
    assert tools["grep_files"].handler(
        pattern="SERVER_SECRET", directory=str(tmp_path)
    ).startswith("错误：只允许读取当前 Session")
    assert tools["create_diagram"].handler(
        requirement="test", image_path=str(server_secret)
    ).startswith("错误：参考图片不可用：只允许读取当前 Session")

    # Local TUI callers omit the sandbox arguments and retain explicit local-file access.
    assert read_document(str(server_secret)) == "SERVER_SECRET_MARKER"
    assert "server-secret.txt" in find_files("server-secret", str(tmp_path))


def test_tui_default_resource_paths_remain_compatible(tmp_path, monkeypatch):
    """TUI 不传目录覆盖时，仍使用项目级默认资源目录。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    (tmp_path / "styles").mkdir()
    (tmp_path / "skills" / "tui-skill.md").write_text(
        "---\nname: tui-skill\ndescription: TUI skill\n---\n\nInstructions",
        encoding="utf-8",
    )
    (tmp_path / "styles" / "default.md").write_text(
        "---\nname: default\ndescription: TUI style\n---\n",
        encoding="utf-8",
    )

    session = DiagramSession(_settings(), tmp_path / "output" / "generate")

    assert "tui-skill" in session.list_skill_packs()
    assert "default" in session.list_styles()
    # 非法名称在模型调用前返回，也覆盖默认 SkillAgent 目录不会 Path(None)。
    assert "名称只能" in session.create_skill("INVALID NAME", "test")


def test_file_subagent_runs_restricted_tool_loop_and_emits_events(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client, "subagent-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)
    subagent = state.agent._subagent
    target = state.root / "workspace" / "large.md"
    target.write_text("IMPORTANT_FINDING", encoding="utf-8")
    events = []
    subagent._on_event = lambda event, data: events.append((event, data))
    calls = 0

    def fake_chat(
        messages, tools, on_delta, on_tick=None, on_reasoning=None,
        should_cancel=None,
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            arguments = '{"keyword":"large"}'
            on_reasoning("先查找目标文件")
            on_tick(arguments)
            tool_call = SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="find_files", arguments=arguments),
                model_dump=lambda: {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "find_files", "arguments": arguments},
                },
            )
            return SimpleNamespace(content="", tool_calls=[tool_call])
        assert "large.md" in messages[-1]["content"]
        on_delta("已找到目标文件。")
        return SimpleNamespace(content="已找到目标文件。", tool_calls=[])

    subagent._llm.chat_with_tools_stream = fake_chat
    assert subagent.run("查找 large.md") == "已找到目标文件。"
    assert [event for event, _ in events] == [
        "started", "reasoning.delta", "usage", "tool.started", "tool.completed",
        "delta", "completed",
    ]
    tool_result = next(data["result"] for event, data in events if event == "tool.completed")
    assert "large.md" in tool_result and "size=" in tool_result


def test_sessions_are_isolated_between_users(tmp_path):
    app = create_app(_settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output")
    owner = TestClient(app)
    _register(owner, "owner")
    session_id = owner.post("/v1/sessions", json={}).json()["id"]
    assert owner.post(
        f"/v1/sessions/{session_id}/workspace/files?filename=private.txt",
        content=b"secret",
    ).status_code == 201

    other = TestClient(app)
    _register(other, "other")
    assert other.get(f"/v1/sessions/{session_id}/workspace/tree").status_code == 404
    assert other.get(f"/v1/sessions/{session_id}/workspace/files/content?path=private.txt").status_code == 404
    assert other.get("/v1/workspace/tree").status_code == 404


def test_session_restores_after_service_restart(tmp_path):
    data = tmp_path / "data"
    workspace = tmp_path / "output"
    first = TestClient(create_app(_settings(), data_root=data, workspace_root=workspace))
    _register(first, "restore-user")
    session_id = first.post("/v1/sessions", json={}).json()["id"]
    first.post(
        f"/v1/sessions/{session_id}/workspace/files?filename=remember.txt",
        content=b"remember",
    )
    first_state = first.app.state.agent_service.get_session(session_id)
    (first_state.root / "generate" / "v7").mkdir(parents=True)
    (first_state.root / "generate" / "current.mmd").write_text(
        "flowchart TD\nRESTORED-->OK", encoding="utf-8"
    )

    second = TestClient(create_app(_settings(), data_root=data, workspace_root=workspace))
    login = second.post(
        "/v1/auth/login", json={"username": "restore-user", "password": "test-password"}
    )
    assert login.status_code == 200
    tree = second.get(f"/v1/sessions/{session_id}/workspace/tree")
    assert tree.status_code == 200
    assert tree.json()[0]["children"][0]["name"] == "remember.txt"
    restored = second.get(f"/v1/sessions/{session_id}").json()
    assert restored["version"] == 7
    assert restored["has_diagram"] is True


def test_diagram_versions_and_current_state_restore_from_disk(tmp_path):
    generate = tmp_path / "generate"
    (generate / "v1").mkdir(parents=True)
    (generate / "v1" / "history.txt").write_text("keep-v1", encoding="utf-8")
    (generate / "v3").mkdir()
    (generate / "current.mmd").write_text("flowchart TD\nA-->B", encoding="utf-8")
    (generate / "current.png").write_bytes(b"existing-image")

    session = DiagramSession(_settings(), generate)

    assert session.version == 3
    assert session.current_code == "flowchart TD\nA-->B"
    assert session.current_image == generate / "current.png"
    assert session.has_diagram
    assert session._next_run_dir() == generate / "v4"
    assert (generate / "v1" / "history.txt").read_text(encoding="utf-8") == "keep-v1"


def test_rendered_candidate_is_published_before_final_verification(tmp_path):
    generate = tmp_path / "generate"
    run_dir = generate / "v1"
    run_dir.mkdir(parents=True)
    image = run_dir / "round_2.png"
    svg = run_dir / "round_2.svg"
    image.write_bytes(b"candidate-image")
    svg.write_text("<svg/>", encoding="utf-8")
    session = DiagramSession(_settings(), generate)
    session.version = 1

    session._publish_candidate(
        "flowchart TD\nNEW-->CANDIDATE", image, svg, run_dir, 2
    )

    assert (generate / "current.mmd").read_text(encoding="utf-8") == "flowchart TD\nNEW-->CANDIDATE"
    assert (generate / "current.png").read_bytes() == b"candidate-image"
    assert (generate / "current.svg").read_text(encoding="utf-8") == "<svg/>"
    assert session.current_image == generate / "current.png"


def test_active_run_can_be_recovered_and_cancelled(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client, "cancel-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)

    def cancellable_chat(prompt, images=None):
        for _ in range(500):
            if state.diagram.should_cancel and state.diagram.should_cancel():
                return "生成已停止。已保留候选图。"
            time.sleep(0.002)
        return "unexpected completion"

    state.agent.chat = cancellable_chat
    created = client.post(
        f"/v1/sessions/{session_id}/runs", json={"input": "long drawing"}
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    active = client.get(f"/v1/sessions/{session_id}/active-run")
    assert active.status_code == 200
    assert active.json()["id"] == run_id

    cancelling = client.post(f"/v1/runs/{run_id}/cancel")
    assert cancelling.status_code == 200
    assert cancelling.json()["status"] in {"cancelling", "cancelled"}
    for _ in range(200):
        run = client.get(f"/v1/runs/{run_id}").json()
        if run["status"] == "cancelled":
            break
        time.sleep(0.005)

    assert run["status"] == "cancelled"
    assert client.get(f"/v1/sessions/{session_id}/active-run").json() is None
    event_types = [event["type"] for event in app.state.agent_service.get_run(run_id).events]
    assert "run.cancelling" in event_types
    assert event_types[-1] == "run.cancelled"


def test_verification_uses_streaming_tool_call_and_reports_deltas():
    class FakeLLM:
        _no_tools = False

        def chat_with_tools_stream(
            self, messages, tools, on_delta, on_tick=None, on_reasoning=None,
            should_cancel=None,
        ):
            on_delta('{"reason":"版面清晰",')
            if on_tick:
                on_tick('{"passed":true,"issues":""}')
            call = SimpleNamespace(
                function=SimpleNamespace(
                    arguments='{"reason":"版面清晰","passed":true,"issues":""}'
                )
            )
            return SimpleNamespace(content=None, tool_calls=[call])

    deltas, ticks = [], []
    agent = FlowchartAgent.__new__(FlowchartAgent)
    passed, issues, raw = agent._judge(
        FakeLLM(),
        [{"role": "user", "content": "verify"}],
        on_delta=deltas.append,
        on_tick=ticks.append,
    )

    assert passed is True
    assert issues == ""
    assert deltas == ['{"reason":"版面清晰",']
    assert ticks == ['{"passed":true,"issues":""}']
    assert '"passed":true' in raw


def test_resource_mounts_persist_and_activate_core(tmp_path):
    data = tmp_path / "data"
    output = tmp_path / "output"
    first_app = create_app(_settings(), data_root=data, workspace_root=output)
    first = TestClient(first_app)
    _register(first, "mount-user")
    session_id = first.post("/v1/sessions", json={}).json()["id"]
    skills = first.get(f"/v1/sessions/{session_id}/client/skills").json()
    styles = first.get(f"/v1/sessions/{session_id}/client/styles").json()
    skill_file = skills[0]["name"]
    style_file = styles[0]["name"]

    assert first.patch(
        f"/v1/sessions/{session_id}/client/skills/{skill_file}",
        json={"mounted": True},
    ).json()["mounted"] is True
    assert first.patch(
        f"/v1/sessions/{session_id}/client/styles/{style_file}",
        json={"mounted": True},
    ).json()["mounted"] is True
    first_state = first_app.state.agent_service.get_session(session_id)
    skill = parse_skill_pack_text(
        (first_state.root / "client" / "skills" / skill_file).read_text(encoding="utf-8")
    )
    style = parse_style_text(
        (first_state.root / "client" / "styles" / style_file).read_text(encoding="utf-8")
    )
    assert skill is not None and skill.name in first_state.diagram._active_skill_names
    assert style is not None and first_state.diagram.style.name == style.name
    mounted_prompt = first_app.state.agent_service.mounted_prompt(first_state)
    assert "用户明确要求加载 Skill" in mounted_prompt
    assert "必须先调用 use_skill" in mounted_prompt

    # SQLite is authoritative: a stale in-memory/UI refresh must not erase mounts.
    first_state.mounted_resources = {"skills": set(), "styles": set()}
    refreshed_skills = first.get(f"/v1/sessions/{session_id}/client/skills").json()
    assert next(item for item in refreshed_skills if item["name"] == skill_file)["mounted"] is True
    assert skill_file in first_state.mounted_resources["skills"]

    second_app = create_app(_settings(), data_root=data, workspace_root=output)
    second = TestClient(second_app)
    assert second.post(
        "/v1/auth/login",
        json={"username": "mount-user", "password": "test-password"},
    ).status_code == 200
    restored_skills = second.get(f"/v1/sessions/{session_id}/client/skills").json()
    restored_styles = second.get(f"/v1/sessions/{session_id}/client/styles").json()
    assert next(item for item in restored_skills if item["name"] == skill_file)["mounted"] is True
    assert next(item for item in restored_styles if item["name"] == style_file)["mounted"] is True
    restored = second_app.state.agent_service.get_session(session_id)
    assert skill.name in restored.diagram._active_skill_names
    assert restored.diagram.style.name == style.name


def test_reasoning_delta_is_emitted_for_web_run(tmp_path):
    app = create_app(_settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output")
    client = TestClient(app)
    _register(client, "reason-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)

    def fake_chat(prompt, images=None):
        state.diagram.on_reasoning("正在分析流程结构")
        return "done"

    state.agent.chat = fake_chat
    run_id = client.post(
        f"/v1/sessions/{session_id}/runs", json={"input": "draw"}
    ).json()["id"]
    for _ in range(100):
        run = app.state.agent_service.get_run(run_id)
        if run.status == "completed":
            break
        time.sleep(0.005)
    reasoning = [event for event in run.events if event["type"] == "reasoning.delta"]
    assert reasoning[0]["data"]["text"] == "正在分析流程结构"


def test_context_stats_compaction_and_restore(tmp_path):
    data = tmp_path / "data"
    output = tmp_path / "output"
    app = create_app(_settings(), data_root=data, workspace_root=output)
    client = TestClient(app)
    _register(client, "context-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    store = app.state.store
    state = app.state.agent_service.get_session(session_id)

    for turn in range(3):
        store.add_message(session_id, "user", f"第 {turn} 轮需求：" + "流程步骤" * 180)
        store.add_message(session_id, "assistant", f"第 {turn} 轮结果：" + "已完成" * 180)
    state.agent.restore_history(store.messages(session_id))
    state.agent._llm.chat = lambda _messages: "已确认目标、关键文件和前两轮执行结果。"

    before = client.get(f"/v1/sessions/{session_id}/context")
    assert before.status_code == 200
    assert before.json()["used_tokens"] > 800

    compacted = client.post(f"/v1/sessions/{session_id}/context/compact")
    assert compacted.status_code == 200
    assert compacted.json()["compressed"] is True
    assert compacted.json()["used_tokens"] < compacted.json()["before_tokens"]
    assert len(client.get(f"/v1/sessions/{session_id}/messages").json()) == 6

    app.state.agent_service.sessions.pop(session_id)
    restored = app.state.agent_service.get_session(session_id)
    assert restored.agent._messages[1]["role"] == "system"
    assert "已确认目标" in restored.agent._messages[1]["content"]
    assert [item["role"] for item in restored.agent._messages[-2:]] == ["user", "assistant"]

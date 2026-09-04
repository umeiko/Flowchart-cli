"""上下文成本控制：大文件/超大搜索截断、force_read 门禁、压缩兜底与强制清空。"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flowchart_agent.config import ModelConfig, Settings
from flowchart_agent.main_agent import MainAgent
from flowchart_agent.server import create_app
from flowchart_agent.session import DiagramSession
from flowchart_agent.skills.builtin import build_skills


def _settings() -> Settings:
    return Settings(
        text_model=ModelConfig(name="test", api_key="test", base_url="http://localhost/v1"),
        output_engine="mermaid",
        verify_mode="code",
    )


def _tools(session, allow_force_read=False):
    images = SimpleNamespace(add=lambda path: path)
    skills = build_skills(session, images, allow_force_read=allow_force_read)
    return {item.name: item for item in skills}


def _register(client: TestClient, username: str = "tester") -> None:
    response = client.post(
        "/v1/auth/register", json={"username": username, "password": "test-password"}
    )
    assert response.status_code == 201


def test_read_document_truncates_large_files_and_force_read_is_subagent_only(tmp_path):
    session = DiagramSession(_settings(), tmp_path / "generate")
    main_tools = _tools(session)
    sub_tools = _tools(session, allow_force_read=True)
    content = "需求文档开头重要标记\n" + "正文内容行\n" * 3000  # > 20KB
    big = tmp_path / "big.md"
    big.write_text(content, encoding="utf-8")

    preview = main_tools["read_document"].handler(path=str(big))
    assert preview.startswith("警告：文件")
    assert "需求文档开头重要标记" in preview  # 只保留开头约 100 token
    assert "delegate_task" in preview and "force_read=true" in preview
    assert len(preview) < 2000

    denied = main_tools["read_document"].handler(path=str(big), force_read=True)
    assert denied.startswith("错误：force_read 仅限子 Agent")

    assert sub_tools["read_document"].handler(path=str(big), force_read=True) == content
    assert "force_read" in main_tools["read_document"].parameters["properties"]

    small = tmp_path / "small.md"
    small.write_text("小文件全文", encoding="utf-8")
    assert main_tools["read_document"].handler(path=str(small)) == "小文件全文"


def test_grep_files_truncates_oversized_results_and_force_read_returns_all(tmp_path):
    session = DiagramSession(_settings(), tmp_path / "generate")
    main_tools = _tools(session)
    sub_tools = _tools(session, allow_force_read=True)
    (tmp_path / "hits.txt").write_text(
        "\n".join(f"命中行 {i} " + "字" * 100 for i in range(40)),
        encoding="utf-8",
    )

    result = main_tools["grep_files"].handler(pattern="命中行", directory=str(tmp_path))
    assert "结果过大" in result
    assert "delegate_task" in result
    assert len(result) < 2000

    full = sub_tools["grep_files"].handler(
        pattern="命中行", directory=str(tmp_path), force_read=True
    )
    assert "找到 40 处匹配" in full

    denied = main_tools["grep_files"].handler(
        pattern="命中行", directory=str(tmp_path), force_read=True
    )
    assert denied.startswith("错误：force_read 仅限子 Agent")
    assert "force_read" in main_tools["grep_files"].parameters["properties"]


def test_grep_files_supports_single_file_path(tmp_path):
    session = DiagramSession(_settings(), tmp_path / "generate")
    main_tools = _tools(session)
    target = tmp_path / "one.txt"
    target.write_text("alpha\nbeta 命中 gamma\n", encoding="utf-8")

    result = main_tools["grep_files"].handler(pattern="命中", path=str(target))
    assert "one.txt:2" in result and "找到 1 处匹配" in result

    none = main_tools["grep_files"].handler(pattern="不存在词", path=str(target))
    assert "没有匹配" in none and "one.txt" in none

    missing = main_tools["grep_files"].handler(pattern="x", path=str(tmp_path / "nope.txt"))
    assert missing.startswith("错误：文件不存在")

    assert "path" in main_tools["grep_files"].parameters["properties"]


def test_compact_context_survives_provider_rejection(tmp_path):
    session = DiagramSession(_settings(), tmp_path / "generate")
    agent = MainAgent(_settings(), session)
    agent._messages += [
        {"role": "user", "content": "历史" * 500},
        {"role": "assistant", "content": "回复" * 500},
        {"role": "user", "content": "最新问题"},
    ]

    def rejecting_chat(_messages):
        raise RuntimeError("context length exceeded")

    agent._llm.chat = rejecting_chat
    result = agent.compact_context()

    assert result["compressed"] is False
    assert "强制清空上下文" in result["reason"]
    assert len(agent._messages) == 4  # 历史未被破坏


def test_compact_context_skips_oversized_history_without_calling_model(tmp_path):
    settings = replace(_settings(), context_window=100)
    session = DiagramSession(settings, tmp_path / "generate")
    agent = MainAgent(settings, session)
    agent._messages += [
        {"role": "user", "content": "历史" * 500},
        {"role": "assistant", "content": "回复" * 500},
        {"role": "user", "content": "最新问题"},
    ]
    called = False

    def spy_chat(_messages):
        nonlocal called
        called = True
        return "摘要"

    agent._llm.chat = spy_chat
    result = agent.compact_context()

    assert result["compressed"] is False
    assert called is False
    assert "强制清空上下文" in result["reason"]


def test_clear_context_endpoint_resets_history_but_keeps_chat_log(tmp_path):
    app = create_app(
        _settings(), data_root=tmp_path / "data", workspace_root=tmp_path / "output"
    )
    client = TestClient(app)
    _register(client, "clear-user")
    session_id = client.post("/v1/sessions", json={}).json()["id"]
    state = app.state.agent_service.get_session(session_id)
    state.agent._messages.append({"role": "user", "content": "hello"})
    app.state.store.add_message(session_id, "user", "hello")

    assert client.get(f"/v1/sessions/{session_id}/context").json()["message_count"] == 1

    response = client.post(f"/v1/sessions/{session_id}/context/clear")

    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["message_count"] == 0
    assert len(state.agent._messages) == 1  # 仅剩 system
    # 恢复点已截断：重启后不再恢复历史，但聊天记录保留用于界面查看
    summary, rows = app.state.store.context_messages(session_id)
    assert summary is None and rows == []
    assert len(app.state.store.messages(session_id)) == 1

    # 前端：压缩按钮变为二级菜单，包含压缩与强制清空两项
    html = client.get("/").text
    script = client.get("/static/app.js").text
    assert 'id="context-menu"' in html
    assert 'id="context-menu-compact"' in html
    assert 'id="context-menu-clear"' in html
    assert "/context/clear" in script

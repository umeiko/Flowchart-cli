import json
import re
from pathlib import Path
from types import SimpleNamespace

from flowchart_agent.check.agent import CheckAgent
from flowchart_agent.check.classifier import CheckClassifier
from flowchart_agent.check.item_agent import ItemCheckAgent, VERDICT_PASS
from flowchart_agent.check.items import (
    CheckItem,
    load_check_batch_protocol,
    load_check_items,
    parse_check_skill,
)
from flowchart_agent.config import ModelConfig, Settings
from flowchart_agent.main_agent import MainAgent
from flowchart_agent import main_agent as main_agent_module
from flowchart_agent.router import route_generation_skill_relevance
from flowchart_agent.session import DiagramSession
from flowchart_agent.skillpacks import parse_skill_pack_text


def _settings() -> Settings:
    return Settings(
        text_model=ModelConfig(
            name="test", api_key="test", base_url="http://localhost/v1"
        )
    )


def test_builtin_check_skill_provides_all_review_items():
    items = load_check_items(Path("skills"))

    assert {item.id for item in items} == {
        "schematic_consistency",
        "flowchart_correctness",
        "flowchart_steps",
        "network_correctness",
        "network_consistency",
        "ui_correctness",
        "ui_terminology",
        "ui_sensitive",
        "ui_steps",
    }
    assert all(item.source_skill == "check" for item in items)
    assert all("第一行输出 `PASS`" in item.prompt for item in items)
    assert all("batch_plan.json" not in item.prompt for item in items)
    assert "用 `write_file` 写出 `batch_plan.json`" in load_check_batch_protocol(
        Path("skills")
    )


def test_batch_protocol_keeps_planning_separate_from_check_execution():
    protocol = load_check_batch_protocol(Path("skills"))

    assert "list_dir" in protocol
    assert "read_document" in protocol
    assert "write_file" in protocol
    assert "规划子 Agent 立即结束" in protocol
    assert "逐 case 发起彼此独立的图片质检" in protocol


def test_only_kind_check_skills_are_loaded():
    ordinary = parse_skill_pack_text(
        "---\nname: ordinary\ndescription: normal\n---\n"
        "## check: fake | 不应加载\napplies_to: *\n检查内容"
    )
    malformed = parse_skill_pack_text(
        "---\nname: malformed\ndescription: bad\nkind: check\n---\n没有检查条目"
    )

    assert ordinary is not None and parse_check_skill(ordinary) == []
    assert malformed is not None and parse_check_skill(malformed) == []


def test_check_skill_applies_to_accepts_human_wildcard_aliases():
    for alias in ("*", "所有图片", "全部图像", "任意图片", "all", "any"):
        pack = parse_skill_pack_text(
            "---\nname: custom\ndescription: custom check\nkind: check\n---\n"
            f"## check: custom_item | 自定义检查\napplies_to: {alias}\n检查内容"
        )
        assert pack is not None
        item = parse_check_skill(pack)[0]
        assert item.applies_to == frozenset()


def test_check_skill_applies_to_accepts_human_separators():
    pack = parse_skill_pack_text(
        "---\nname: custom\ndescription: custom check\nkind: check\n---\n"
        "## check: custom_item | 自定义检查\n"
        "applies_to: 原理图、流程图；组网图 / 界面截图\n检查内容"
    )

    assert pack is not None
    assert parse_check_skill(pack)[0].applies_to == frozenset(
        {"原理图", "流程图", "组网图", "界面截图"}
    )


def test_check_agent_refuses_when_session_has_no_check_skill(tmp_path):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    agent = CheckAgent(_settings(), tmp_path / "output", skill_dir=skill_dir)

    reply = agent.handle("检查这个文件")

    assert "没有合法的检查 Skill" in reply
    assert "不会使用内置或臆造的审查标准" in reply
    assert "审查标准文档" in reply


def test_classifier_can_reject_request_not_covered_by_check_skills():
    class TextModel:
        def __init__(self):
            self.messages = None

        def chat(self, messages, should_cancel=None):
            self.messages = messages
            return (
                '{"supported":false,"items":[],"doc_paths":[],"image_paths":[],'
                '"reason":"现有 Skill 不包含代码许可证审查"}'
            )

    text_model = TextModel()
    items = load_check_items(Path("skills"))
    result = CheckClassifier(text_model, object()).classify(
        "检查代码许可证合规性", [], items
    )

    assert result is not None and result.supported is False
    assert "Skill check" in text_model.messages[0]["content"]
    assert "禁止臆造" in text_model.messages[0]["content"]


def test_classifier_reports_each_image_description_progress(tmp_path):
    images = [tmp_path / "one.png", tmp_path / "two.png"]
    progress = []

    class VisionModel:
        def chat_with_image(self, _prompt, image, should_cancel=None):
            return f"流程图：{image.name}"

    classifier = CheckClassifier(object(), VisionModel())
    descriptions = classifier.describe_images(images, progress.append)

    assert [path.name for path, _ in descriptions] == ["one.png", "two.png"]
    assert progress == [
        "正在描述图片 1/2：one.png…",
        "图片描述完成 1/2：one.png",
        "正在描述图片 2/2：two.png…",
        "图片描述完成 2/2：two.png",
    ]


def test_item_check_stream_reports_received_characters(tmp_path):
    image = tmp_path / "one.png"
    item = CheckItem(
        id="custom",
        name="自定义检查",
        applies_to=frozenset(),
        prompt="检查图片\n{document}",
        source_skill="check",
        source_description="test",
    )
    progress = []

    class VisionModel:
        def chat_with_image_stream(
            self, _prompt, _image, on_delta, on_reasoning=None,
            should_cancel=None,
        ):
            on_delta("x" * 120)
            return "PASS\n通过"

    result = ItemCheckAgent(VisionModel()).run(
        item, [image], {image: "流程图"}, "文档", on_progress=progress.append
    )

    assert result[0].verdict == VERDICT_PASS
    assert progress == ["自定义检查 · one.png：已接收 120 字符…"]


def test_generate_refuses_mounted_check_skill_before_model_call(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "Check.md").write_text(
        Path("skills/Check.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    session = DiagramSession(
        _settings(), tmp_path / "generate", skill_dir=skill_dir
    )
    session.use_skill("check")
    monkeypatch.setattr(main_agent_module, "route_category", lambda *_args, **_kwargs: "generate")
    monkeypatch.setattr(
        main_agent_module,
        "route_generation_skill_relevance",
        lambda *_args, **_kwargs: ["check"],
    )
    agent = MainAgent(_settings(), session, output_root=tmp_path)

    reply = agent.chat("画美蛙鱼火锅店的客户接待流程")

    assert "暂时不能为你作图" in reply
    assert "Skill：check" in reply
    assert "漏取消选择" in reply
    assert not (tmp_path / "generate" / "v1").exists()
    dispatch_log = (tmp_path / "generate" / "run.log").read_text(encoding="utf-8")
    assert "Skill 相关性检查开始：已挂载=check" in dispatch_log
    assert "Skill 相关性检查结果：发现明显无关 Skill=check" in dispatch_log


def test_generation_skill_relevance_is_decided_from_skill_meaning():
    class RelevanceModel:
        def __init__(self):
            self.messages = None

        def chat(self, messages, should_cancel=None):
            self.messages = messages
            return '{"unrelated":["check"],"reason":"这是审查规范，不指导餐饮接待作图"}'

    model = RelevanceModel()
    skills = [
        pack for pack in [parse_skill_pack_text(Path("skills/Check.md").read_text(encoding="utf-8"))]
        if pack is not None
    ]

    unrelated = route_generation_skill_relevance(
        model, "画美蛙鱼火锅店的客户接待流程", skills
    )

    assert unrelated == ["check"]
    prompt = model.messages[1]["content"]
    assert "美蛙鱼火锅店" in prompt
    assert "通用技术文档与图片审查标准" in prompt


def test_generation_preflight_is_carried_into_version_log(tmp_path):
    session = DiagramSession(_settings(), tmp_path / "generate")
    messages = [
        "Skill 相关性检查开始：已挂载=flowchart_format",
        "Skill 相关性检查结果：全部相关，可以继续作图",
    ]
    session.queue_generation_preflight(messages)
    run_dir = tmp_path / "generate" / "v1"

    session._flush_generation_preflight(run_dir)

    content = (run_dir / "run.log").read_text(encoding="utf-8")
    assert all(message in content for message in messages)
    assert session._pending_generation_log == []


def test_check_skill_instructions_come_from_current_session_directory(tmp_path):
    skill_dir = tmp_path / "session-skills"
    skill_dir.mkdir()
    (skill_dir / "custom-check.md").write_text(
        "---\nname: custom-check\ndescription: session only\nkind: check\n---\n\n"
        "## execution\nUse image_reasoning.\n\n"
        "## check: custom | 自定义检查\napplies_to: *\nOnly this rule.",
        encoding="utf-8",
    )
    session = DiagramSession(
        _settings(), tmp_path / "generate", skill_dir=skill_dir
    )
    agent = MainAgent(_settings(), session, output_root=tmp_path)

    instructions = agent._check_skill_instructions()

    assert "Check Skill: custom-check" in instructions
    assert "Use image_reasoning" in instructions
    assert "Only this rule" in instructions


def test_batch_check_route_plans_then_runs_each_case_independently(
    tmp_path, monkeypatch
):
    root = tmp_path / "session"
    batch = root / "workspace" / "batch-check-demo"
    batch.mkdir(parents=True)
    (batch / "one.png").write_bytes(b"image")
    (batch / "one.md").write_text("one", encoding="utf-8")
    (batch / "two.jpg").write_bytes(b"image")
    session = DiagramSession(_settings(), root / "generate")
    monkeypatch.setattr(
        main_agent_module, "route_category", lambda *_args, **_kwargs: "check"
    )
    agent = MainAgent(
        _settings(), session, output_root=root, readable_root=root
    )
    captured = {"checks": []}

    class FakeSubAgent:
        tool_names = {"image_reasoning"}

        def run(self, task, *, allowed_tools=None):
            if "计划文件（必须用 write_file" not in task:
                captured["checks"].append((task, allowed_tools))
                report_relative = re.search(
                    r"建议报告目标（Skill 要求落盘时使用）：([^\n]+)", task
                ).group(1).strip()
                report_path = session.output_dir / report_relative
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    "image,check_id,check_name,result,findings\none.png,x,x,PASS,ok\n",
                    encoding="utf-8",
                )
                return "检查完成（1 项）：通过 1 项，不通过 0 项，不符合该分类 0 项。"
            captured["task"] = task
            captured["allowed_tools"] = allowed_tools
            plan_relative = re.search(
                r"计划文件（必须用 write_file 写到这个精确路径）：([^\n]+)", task
            ).group(1).strip()
            plan_path = session.output_dir / plan_relative
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps({
                "version": 1,
                "directory": "workspace/batch-check-demo",
                "cases": [
                    {
                        "id": "case-001",
                        "image": "workspace/batch-check-demo/one.png",
                        "documents": ["workspace/batch-check-demo/one.md"],
                    },
                    {
                        "id": "case-002",
                        "image": "workspace/batch-check-demo/two.jpg",
                        "documents": [],
                    },
                ],
                "warnings": [],
            }), encoding="utf-8")
            return "已写入计划并结束"

    agent._subagent = FakeSubAgent()

    reply = agent.chat(
        "批量质检 workspace/batch-check-demo 下的所有图片，按同名文档配对"
    )

    assert reply.startswith("批量质检完成：独立处理 2 个案例")
    assert "批量目录：workspace/batch-check-demo" in captured["task"]
    assert "写完计划后立即结束" in captured["task"]
    assert captured["allowed_tools"] == {"list_dir", "read_document", "write_file"}
    assert len(captured["checks"]) == 2
    assert all("image_reasoning" in task for task, _ in captured["checks"])
    assert all("Check Skill: check" in task for task, _ in captured["checks"])
    assert all("image_reasoning" in allowed for _, allowed in captured["checks"])
    summaries = list(session.output_dir.glob("batch_plans/*_summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["totals"] == {"passed": 2, "failed": 0, "not_applicable": 0}


def test_check_skill_execution_grades_documents_by_size():
    """Check Skill 的执行协议应描述文档分级：大文档逐份 delegate_task 提炼。"""
    text = Path("skills/Check.md").read_text(encoding="utf-8")
    execution = text.split("## execution", 1)[1].split("## batch", 1)[0]
    assert "delegate_task" in execution
    assert "force_read=true" in execution
    assert "20KB" in execution


def test_check_route_is_skill_driven_without_hardcoded_dispatch(tmp_path, monkeypatch):
    """非批量 check 路由进入主 Agent 工具循环（Skill 驱动），不再硬编码派单子 Agent。"""
    monkeypatch.setattr(
        main_agent_module, "route_category", lambda *_args, **_kwargs: "check"
    )
    session = DiagramSession(_settings(), tmp_path / "generate")
    agent = MainAgent(_settings(), session, output_root=tmp_path)

    def fail_subagent(*_args, **_kwargs):
        raise AssertionError("check 不应由硬编码编排直接派单子 Agent")

    agent._subagent.run = fail_subagent
    captured = {}

    def fake_chat_with_tools(messages, tools, should_cancel=None):
        captured["user"] = messages[-1]["content"]
        captured["tools"] = {tool["function"]["name"] for tool in tools}
        return SimpleNamespace(content="请先提供审查标准 Skill", tool_calls=[])

    agent._llm.chat_with_tools = fake_chat_with_tools
    reply = agent.chat("检查 workspace/ui.png 的敏感信息")

    assert reply == "请先提供审查标准 Skill"
    assert "use_skill" in captured["user"]
    assert "delegate_task" in captured["tools"]
    assert "image_reasoning" in captured["user"]

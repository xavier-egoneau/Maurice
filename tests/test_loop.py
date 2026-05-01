from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.contracts import ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import MockProvider
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillLoader, SkillRoot, SkillState
from maurice.system_skills.filesystem.tools import filesystem_tool_executors
from maurice.system_skills.dreaming.tools import dreaming_tool_executors
from maurice.system_skills.memory.tools import build_dream_input, memory_tool_executors
from maurice.system_skills.reminders.tools import reminders_tool_executors
from maurice.system_skills.self_update.tools import self_update_tool_executors
from maurice.system_skills.skills.tools import skill_authoring_tool_executors
from maurice.system_skills.vision.tools import vision_tool_executors
from maurice.system_skills.web.tools import web_tool_executors


def make_loop(tmp_path, provider, *, profile="safe", approval_store=None, executors=None):
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["filesystem", "memory", "dreaming", "skills", "self_update", "web", "reminders", "vision"],
    ).load()
    return AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(tmp_path / "sessions"),
        event_store=EventStore(tmp_path / "events.jsonl"),
        permission_context=PermissionContext(
            workspace_root=str(workspace),
            runtime_root=str(runtime),
        ),
        permission_profile=profile,
        tool_executors=executors or {},
        approval_store=approval_store,
        model="mock",
        system_prompt="Kernel prompt",
    )


def test_loop_persists_text_turn_and_events(tmp_path) -> None:
    provider = MockProvider(
        [
            {"type": "text_delta", "delta": "Salut"},
            {"type": "text_delta", "delta": " humain"},
            {"type": "status", "status": "completed"},
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Bonjour")

    assert result.assistant_text == "Salut humain"
    assert [message.role for message in result.session.messages] == ["user", "assistant"]
    event_names = [event.name for event in loop.event_store.read_all()]
    assert event_names == ["turn.started", "turn.completed"]
    assert provider.calls[0]["messages"][0]["content"] == "Bonjour"
    assert "Use filesystem tools" in provider.calls[0]["system"]


def test_agent_system_prompt_explains_content_root(tmp_path) -> None:
    from maurice.host.cli import _agent_system_prompt

    prompt = _agent_system_prompt(tmp_path / "workspace")

    assert "Agent content" in prompt
    assert "Workspace root" in prompt
    assert "Path resolution" in prompt
    assert "secrets" in prompt


def test_loop_preserves_provider_failure_error(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "status",
                "status": "failed",
                "error": {
                    "code": "provider_error",
                    "message": "Bad model.",
                    "retryable": False,
                },
            },
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Bonjour")

    assert result.status == "failed"
    assert result.error == "provider_error: Bad model."
    failed_event = loop.event_store.read_all()[-1]
    assert failed_event.name == "turn.failed"
    assert failed_event.payload["error"] == "provider_error: Bad model."


def test_mock_provider_can_call_declared_filesystem_tool(tmp_path) -> None:
    workspace_file = tmp_path / "workspace" / "notes.md"
    workspace_file.parent.mkdir()
    workspace_file.write_text("hello from disk", encoding="utf-8")
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "filesystem.read",
                    "arguments": {"path": str(workspace_file)},
                },
            },
            {"type": "text_delta", "delta": "Done."},
            {"type": "status", "status": "completed"},
        ]
    )

    def read_text(arguments):
        return ToolResult(
            ok=True,
            summary="File read.",
            data={"content": workspace_file.read_text(encoding="utf-8")},
            trust="local_mutable",
            artifacts=[{"type": "file", "path": arguments["path"]}],
            events=[],
            error=None,
        )

    loop = make_loop(
        tmp_path,
        provider,
        executors={"filesystem.read": read_text},
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Read notes")

    assert result.tool_results[0].ok
    assert result.tool_results[0].data["content"] == "hello from disk"
    assert [event.name for event in loop.event_store.read_all()] == [
        "turn.started",
        "tool.requested",
        "tool.started",
        "tool.completed",
        "turn.completed",
    ]
    assert [message.role for message in result.session.messages] == [
        "user",
        "assistant",  # tool call record
        "tool",       # tool result
        "assistant",  # final text
    ]


def test_loop_can_continue_after_tool_result_when_limit_allows(tmp_path) -> None:
    class SequentialProvider:
        def __init__(self):
            self.calls = []

        def stream(self, *, messages, model, tools, system, limits=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                yield {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "call_1",
                        "name": "filesystem.read",
                        "arguments": {"path": "notes.md"},
                    },
                }
                yield {"type": "status", "status": "completed"}
                return
            yield {"type": "text_delta", "delta": "J'ai lu le fichier et je continue."}
            yield {"type": "status", "status": "completed"}

    provider = SequentialProvider()

    def read_text(_arguments):
        return ToolResult(
            ok=True,
            summary="File read.",
            data={"content": "notes"},
            trust="local_mutable",
        )

    loop = make_loop(tmp_path, provider, executors={"filesystem.read": read_text})

    result = loop.run_turn(
        agent_id="main",
        session_id="sess_1",
        message="Read notes",
        limits={"max_tool_iterations": 2},
    )

    assert len(provider.calls) == 2
    assert any(message["role"] == "tool" for message in provider.calls[1])
    assert result.assistant_text == "J'ai lu le fichier et je continue."
    assert result.tool_results[0].ok


def test_loop_continues_after_tool_result_by_default(tmp_path) -> None:
    class SequentialProvider:
        def __init__(self):
            self.calls = []

        def stream(self, *, messages, model, tools, system, limits=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                yield {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "call_1",
                        "name": "filesystem.list",
                        "arguments": {"path": "."},
                    },
                }
                yield {"type": "status", "status": "completed"}
                return
            yield {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_2",
                    "name": "filesystem.move",
                    "arguments": {"source_path": "test/src", "target_path": "src"},
                },
            }
            yield {"type": "text_delta", "delta": "C'est remis en place."}
            yield {"type": "status", "status": "completed"}

    def list_entries(_arguments):
        return ToolResult(ok=True, summary="J'ai trouve `test`.", trust="local_mutable")

    def move_path(_arguments):
        return ToolResult(ok=True, summary="J'ai deplace `src`.", trust="local_mutable")

    provider = SequentialProvider()
    loop = make_loop(
        tmp_path,
        provider,
        profile="limited",
        executors={"filesystem.list": list_entries, "filesystem.move": move_path},
    )

    result = loop.run_turn(
        agent_id="main",
        session_id="sess_1",
        message="Remets src a la racine",
        limits={"max_tool_iterations": 8},
    )

    assert len(provider.calls) == 2
    assert [tool.summary for tool in result.tool_results] == ["J'ai trouve `test`.", "J'ai deplace `src`."]
    assert result.assistant_text == "C'est remis en place."


def test_loop_can_execute_text_tool_protocol_when_enabled(tmp_path) -> None:
    class TextToolProvider:
        def __init__(self):
            self.calls = []

        def stream(self, *, messages, model, tools, system, limits=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                yield {
                    "type": "text_delta",
                    "delta": (
                        "```maurice_tool_calls\n"
                        '[{"name":"filesystem.list","arguments":{"path":"."}}]\n'
                        "```"
                    ),
                }
                yield {"type": "status", "status": "completed"}
                return
            yield {"type": "text_delta", "delta": "J'ai trouve le dossier."}
            yield {"type": "status", "status": "completed"}

    def list_entries(_arguments):
        return ToolResult(
            ok=True,
            summary="J'ai trouve le dossier `app`.",
            data={"entries": [{"name": "app", "type": "directory"}]},
            trust="local_mutable",
        )

    provider = TextToolProvider()
    loop = make_loop(tmp_path, provider, executors={"filesystem.list": list_entries})

    result = loop.run_turn(
        agent_id="main",
        session_id="sess_1",
        message="Liste",
        limits={"max_tool_iterations": 2, "allow_text_tool_calls": True},
    )

    assert len(provider.calls) == 2
    assert result.tool_results[0].summary == "J'ai trouve le dossier `app`."
    assert result.assistant_text == "J'ai trouve le dossier."
    assert [message.role for message in result.session.messages] == ["user", "tool", "assistant"]


def test_loop_does_not_execute_text_tool_protocol_by_default(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "text_delta",
                "delta": (
                    "```maurice_tool_calls\n"
                    '[{"name":"filesystem.list","arguments":{"path":"."}}]\n'
                    "```"
                ),
            },
            {"type": "status", "status": "completed"},
        ]
    )
    loop = make_loop(tmp_path, provider, executors={"filesystem.list": lambda _arguments: {"ok": True}})

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Liste")

    assert result.tool_results == []
    assert "maurice_tool_calls" in result.assistant_text


def test_loop_removes_internal_user_prompt_after_turn(tmp_path) -> None:
    provider = MockProvider(
        [
            {"type": "text_delta", "delta": "Execution lancee"},
            {"type": "status", "status": "completed"},
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = loop.run_turn(
        agent_id="main",
        session_id="sess_1",
        message="Commande interne `/dev` : execute le plan.",
        message_metadata={"internal": True},
    )

    assert result.session.messages == []


def test_tool_execution_goes_through_permission_and_requests_approval(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "filesystem.write",
                    "arguments": {"path": "notes.md", "content": "hello"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    event_store = EventStore(tmp_path / "approvals-events.jsonl")
    approvals = ApprovalStore(tmp_path / "approvals.json", event_store=event_store)
    executed = False

    def write_text(_arguments):
        nonlocal executed
        executed = True
        return {"ok": True, "summary": "written", "data": None, "trust": "local_mutable"}

    loop = make_loop(
        tmp_path,
        provider,
        approval_store=approvals,
        executors={"filesystem.write": write_text},
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Write notes")

    assert not executed
    assert result.tool_results[0].error.code == "approval_required"
    assert approvals.list(status="pending")[0].tool_name == "filesystem.write"


def test_network_tool_approval_scope_uses_url_host(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "web.fetch",
                    "arguments": {"url": "https://example.com/news"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    approvals = ApprovalStore(tmp_path / "approvals.json")
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    loop = make_loop(
        tmp_path,
        provider,
        approval_store=approvals,
        executors=web_tool_executors(context),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Fetch news")

    assert result.tool_results[0].error.code == "approval_required"
    pending = approvals.list(status="pending")[0]
    assert pending.tool_name == "web.fetch"
    assert pending.scope == {"hosts": ["example.com"]}


def test_reminder_tool_can_schedule_job_through_agent_loop(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "reminders.create",
                    "arguments": {
                        "text": "Stretch",
                            "run_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    },
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    job_ids = []

    def schedule(payload):
        job_ids.append(payload["reminder_id"])
        return "job_1"

    loop = make_loop(
        tmp_path,
        provider,
        profile="limited",
        executors=reminders_tool_executors(context, schedule_reminder=schedule),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Remind me",
                           limits={"max_tool_iterations": 1})

    assert result.tool_results[0].ok
    assert job_ids == [result.tool_results[0].data["reminder"]["id"]]
    assert result.tool_results[0].data["reminder"]["job_id"] == "job_1"


def test_vision_inspect_tool_can_run_through_agent_loop(tmp_path) -> None:
    image = tmp_path / "workspace" / "pixel.gif"
    image.parent.mkdir()
    image.write_bytes(b"GIF89a\x01\x00\x02\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff,\x00\x00\x00\x00\x01\x00\x02\x00\x00\x02\x02D\x01\x00;")
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "vision.inspect",
                    "arguments": {"path": str(image)},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    loop = make_loop(
        tmp_path,
        provider,
        executors=vision_tool_executors(context),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Inspect image")

    assert result.tool_results[0].ok
    assert result.tool_results[0].data["format"] == "gif"
    assert result.tool_results[0].data["width"] == 1
    assert result.tool_results[0].data["height"] == 2


def test_approved_replay_allows_asked_tool_execution(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "filesystem.write",
                    "arguments": {"path": "notes.md", "content": "hello"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    approvals = ApprovalStore(tmp_path / "approvals.json")
    approval = approvals.request(
        agent_id="main",
        session_id="sess_1",
        correlation_id="turn_previous",
        tool_name="filesystem.write",
        permission_class="fs.write",
        scope={"paths": ["notes.md"]},
        arguments={"path": "notes.md", "content": "hello"},
        summary="Approve write",
        reason="test",
    )
    approvals.approve(approval.id)

    loop = make_loop(
        tmp_path,
        provider,
        approval_store=approvals,
        executors={
            "filesystem.write": lambda _arguments: {
                "ok": True,
                "summary": "File written.",
                "data": None,
                "trust": "local_mutable",
            }
        },
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Write notes")

    assert result.tool_results[0].ok
    assert result.tool_results[0].summary == "File written."


def test_loop_uses_real_filesystem_executors(tmp_path) -> None:
    workspace_file = tmp_path / "workspace" / "content" / "notes.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("real content", encoding="utf-8")
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "filesystem.read",
                    "arguments": {"path": "notes.md"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    loop = make_loop(
        tmp_path,
        provider,
        executors=filesystem_tool_executors(context),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Read notes")

    assert result.tool_results[0].ok
    assert result.tool_results[0].data["content"] == "real content"


def test_loop_denies_filesystem_read_outside_workspace(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "filesystem.read",
                    "arguments": {"path": str(outside)},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    loop = make_loop(
        tmp_path,
        provider,
        executors=filesystem_tool_executors(context),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Read outside")

    assert result.tool_results[0].error.code == "permission_denied"


def test_loop_uses_real_memory_executors(tmp_path) -> None:
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "memory.remember",
                    "arguments": {
                        "content": "Memory is a skill, not kernel behavior.",
                        "tags": ["architecture"],
                    },
                },
            },
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_2",
                    "name": "memory.search",
                    "arguments": {"query": "skill"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    loop = make_loop(
        tmp_path,
        provider,
        profile="limited",
        executors=memory_tool_executors(context),
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Remember")

    assert result.tool_results[0].ok
    assert result.tool_results[1].data["memories"][0]["content"] == (
        "Memory is a skill, not kernel behavior."
    )


def test_loop_uses_real_dreaming_executor_with_memory_input(tmp_path) -> None:
    context = PermissionContext(
        workspace_root=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "runtime"),
    )
    memory_tool_executors(context)["memory.remember"](
        {"content": "Dreams consume skill-provided signals.", "tags": ["dreaming"]}
    )
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "dreaming.run",
                    "arguments": {"skills": ["memory"]},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["filesystem", "memory", "dreaming"],
    ).load()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(tmp_path / "sessions"),
        event_store=EventStore(tmp_path / "events.jsonl"),
        permission_context=context,
        permission_profile="limited",
        tool_executors=dreaming_tool_executors(
            context,
            registry,
            dream_input_builders={"memory": lambda: build_dream_input(context)},
        ),
        model="mock",
        system_prompt="Kernel prompt",
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Dream")

    assert result.tool_results[0].ok
    assert result.tool_results[0].data["report"]["inputs"][0]["skill"] == "memory"


def test_loop_uses_skill_authoring_executor_for_future_reload(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (workspace / "skills").mkdir(parents=True)
    runtime.mkdir()
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    roots = [
        SkillRoot(path="maurice/system_skills", origin="system", mutable=False),
        SkillRoot(path=str(workspace / "skills"), origin="user", mutable=True),
    ]
    registry = SkillLoader(
        roots,
        enabled_skills=["filesystem", "memory", "dreaming", "skills"],
    ).load()
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "skills.create",
                    "arguments": {"name": "notes_helper"},
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(tmp_path / "sessions"),
        event_store=EventStore(tmp_path / "events.jsonl"),
        permission_context=context,
        permission_profile="limited",
        tool_executors=skill_authoring_tool_executors(
            context,
            roots,
            enabled_skills=["filesystem", "memory", "dreaming", "skills"],
        ),
        model="mock",
        system_prompt="Kernel prompt",
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Create skill")
    future_registry = SkillLoader(roots, enabled_skills=["notes_helper"]).load()

    assert result.tool_results[0].ok
    assert "notes_helper" not in registry.skills
    assert future_registry.skills["notes_helper"].state == SkillState.LOADED


def test_loop_uses_self_update_proposal_after_runtime_write_approval(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    registry = SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=["self_update"],
    ).load()
    args = {
        "target_type": "kernel",
        "target_name": "loop",
        "runtime_path": "$runtime/maurice/kernel/loop.py",
        "summary": "Improve loop testability.",
        "patch": "diff --git a/loop.py b/loop.py\n",
        "risk": "low",
        "test_plan": "Run pytest.",
        "mode": "proposal_only",
    }
    approvals = ApprovalStore(tmp_path / "approvals.json")
    approval = approvals.request(
        agent_id="main",
        session_id="sess_1",
        correlation_id="turn_previous",
        tool_name="self_update.propose",
        permission_class="runtime.write",
        scope={"targets": ["kernel"], "mode": "proposal_only"},
        arguments=args,
        summary="Approve runtime proposal.",
        reason="Limited profile asks for runtime write proposals.",
    )
    approvals.approve(approval.id)
    provider = MockProvider(
        [
            {
                "type": "tool_call",
                "tool_call": {
                    "id": "call_1",
                    "name": "self_update.propose",
                    "arguments": args,
                },
            },
            {"type": "status", "status": "completed"},
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(tmp_path / "sessions"),
        event_store=EventStore(tmp_path / "events.jsonl"),
        permission_context=context,
        permission_profile="limited",
        tool_executors=self_update_tool_executors(context),
        approval_store=approvals,
        model="mock",
        system_prompt="Kernel prompt",
    )

    result = loop.run_turn(agent_id="main", session_id="sess_1", message="Propose update")

    assert result.tool_results[0].ok
    proposal_path = result.tool_results[0].data["path"]
    assert proposal_path.startswith(str(workspace / "proposals" / "runtime"))
    assert not (runtime / "maurice" / "kernel" / "loop.py").exists()

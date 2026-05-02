from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from urllib import request

from maurice.host.command_registry import CommandRegistry, CommandResult, RuntimeCommand, core_commands
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.commands.gateway_server import (
    _build_gateway_http_server,
    _build_gateway_http_server_for_context,
    _gateway_agent_config_status,
    _gateway_agent_config_update,
    _gateway_model_status,
    _gateway_model_update,
    _record_gateway_exchange_in_store,
    _run_turn_via_global_server,
    _read_web_attachment,
    _store_web_uploads,
    _telegram_response_text,
)
from maurice.host.context import resolve_global_context, resolve_local_context
from maurice.host.gateway import InboundMessage, MessageRouter, OutboundMessage
from maurice.host.gateway import GatewayHttpServer
from maurice.host.agents import create_agent
from maurice.host.workspace import initialize_workspace
from maurice.host.paths import kernel_config_path
from maurice.kernel.config import load_workspace_config
from maurice.kernel.config import read_yaml_file, write_yaml_file
from maurice.host.credentials import load_workspace_credentials
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.contracts import ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.session import SessionStore


class DummyTurn:
    assistant_text = "Salut"
    tool_results = []
    status = "completed"
    correlation_id = "turn_dummy"


class ToolOnlyTurn:
    assistant_text = ""
    tool_results = [
        ToolResult(
            ok=True,
            summary="Listed 3 entries: AGENTS.md, DECISIONS.md, PLAN.md",
            trust="local_mutable",
        )
    ]
    status = "completed"
    correlation_id = "turn_tool_only"


class TokenTurn:
    assistant_text = "Salut"
    tool_results = []
    status = "completed"
    correlation_id = "turn_tokens"
    input_tokens = 64_000
    output_tokens = 1_000


class InterruptedTurn:
    assistant_text = "Non, pas encore.\n\nJ'ai seulement"
    tool_results = []
    status = "failed"
    error = "incomplete_stream: ChatGPT provider stream ended before response.completed."
    correlation_id = "turn_interrupted"


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def test_gateway_routes_inbound_message_to_turn_runner(tmp_path) -> None:
    calls = []

    def run_turn(**kwargs):
        calls.append(kwargs)
        return DummyTurn()

    router = MessageRouter(run_turn=run_turn)
    result = router.handle(
        {
            "channel": "local",
            "peer_id": "peer_1",
            "text": "Bonjour",
            "agent_id": "main",
        }
    )

    assert calls == [
            {
                "message": "Bonjour",
                "session_id": "local:peer_1",
                "agent_id": "main",
                "correlation_id": result.outbound.correlation_id,
                "source_channel": "local",
                "source_peer_id": "peer_1",
                "source_metadata": {},
                "limits": {"max_tool_iterations": 8},
            }
        ]
    assert result.outbound.text == "Salut"
    assert result.outbound.session_id == "local:peer_1"


def test_gateway_attaches_context_usage_metadata() -> None:
    router = MessageRouter(run_turn=lambda **_kwargs: TokenTurn())

    result = router.handle(
        {
            "channel": "web",
            "peer_id": "browser",
            "text": "Bonjour",
            "agent_id": "main",
        }
    )

    assert result.outbound.metadata["context"]["total_tokens"] == 65_000
    assert result.outbound.metadata["context"]["context_window_tokens"] == 128_000
    assert result.outbound.metadata["context"]["level"] == "medium"


def test_gateway_surfaces_interrupted_partial_response() -> None:
    router = MessageRouter(run_turn=lambda **_kwargs: InterruptedTurn())

    result = router.handle(
        {
            "channel": "web",
            "peer_id": "browser",
            "text": "du coup tu l'as corrige ?",
            "agent_id": "main",
        }
    )

    assert result.status == "failed"
    assert "J'ai seulement" in result.outbound.text
    assert "Réponse interrompue" in result.outbound.text
    assert result.outbound.metadata["turn_status"] == "failed"


def test_gateway_turn_uses_global_server_when_running(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    bundle = load_workspace_config(workspace)
    project = tmp_path / "outside-project"
    project.mkdir()
    ctx = resolve_global_context(
        workspace,
        agent=bundle.agents.agents["main"],
        bundle=bundle,
        active_project=project,
    )
    calls = []

    class FakeClient:
        def __init__(self, received_ctx) -> None:
            assert received_ctx == ctx

        def is_running(self) -> bool:
            return True

        def connect(self) -> None:
            calls.append(("connect",))

        def close(self) -> None:
            calls.append(("close",))

        def run_turn(self, message, **kwargs):
            calls.append(("turn", message, kwargs))
            yield {"type": "text_delta", "delta": "Salut"}
            yield {"type": "done", "status": "completed", "assistant_text": "Salut"}

    monkeypatch.setattr("maurice.host.commands.gateway_server.MauriceClient", FakeClient)

    result = _run_turn_via_global_server(
        ctx,
        message="bonjour",
        session_id="s1",
        agent_id="main",
        source_channel="local",
        source_peer_id="peer",
        source_metadata={"x": 1},
        limits={"max_tool_iterations": 1},
    )

    assert result.status == "completed"
    assert result.assistant_text == "Salut"
    assert calls[0] == ("connect",)
    assert calls[-1] == ("close",)
    assert calls[1][2]["agent_id"] == "main"
    assert calls[1][2]["source_channel"] == "local"
    assert calls[1][2]["source_metadata"]["x"] == 1
    assert calls[1][2]["source_metadata"]["active_project_root"] == str(project.resolve())


def test_gateway_honors_explicit_session_and_correlation_ids() -> None:
    calls = []

    def run_turn(**kwargs):
        calls.append(kwargs)
        return DummyTurn()

    router = MessageRouter(run_turn=run_turn)
    result = router.handle(
        InboundMessage(
            channel="web",
            peer_id="peer_1",
            text="Bonjour",
            session_id="session_1",
            correlation_id="corr_1",
        )
    )

    assert calls[0]["session_id"] == "session_1"
    assert calls[0]["correlation_id"] == "corr_1"
    assert result.outbound.correlation_id == "corr_1"


def test_gateway_emits_receive_and_send_events(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    router = MessageRouter(run_turn=lambda **_kwargs: DummyTurn(), event_store=event_store)

    router.handle({"channel": "local", "peer_id": "peer_1", "text": "Bonjour"})

    assert [event.name for event in event_store.read_all()] == [
        "gateway.message.received",
        "gateway.message.sent",
    ]


def test_gateway_handles_help_without_turn_runner() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    result = MessageRouter(run_turn=run_turn).handle(
        {"channel": "local", "peer_id": "peer_1", "text": "/help"}
    )

    assert not called
    assert "/new" in result.outbound.text
    assert "/add_agent" in result.outbound.text
    assert "/edit_agent <agent>" in result.outbound.text
    assert "Commandes Maurice" in result.outbound.text


def test_gateway_handles_start_as_help_without_turn_runner() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    result = MessageRouter(run_turn=run_turn).handle(
        {"channel": "local", "peer_id": "peer_1", "text": "/start"}
    )

    assert not called
    assert "/help" in result.outbound.text
    assert "/add_agent" in result.outbound.text


def test_gateway_help_uses_dynamic_command_registry() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    # Register through the normal default path for the executable /help command,
    # then add a skill-provided command to prove the help text is not hardcoded.
    command_registry = CommandRegistry()
    for command in core_commands():
        command_registry.register(command)
    command_registry.register(RuntimeCommand(name="/plan", description="preparer le plan", owner="dev"))

    result = MessageRouter(run_turn=run_turn, command_registry=command_registry).handle(
        {"channel": "local", "peer_id": "peer_1", "text": "/help"}
    )

    assert not called
    assert "/plan - preparer le plan" in result.outbound.text
    assert "/add_agent" not in result.outbound.text


def test_gateway_resets_session_without_turn_runner(tmp_path) -> None:
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create("main", session_id="local:peer_1")
    sessions.append_message("main", "local:peer_1", role="assistant", content="old grouped prompt")
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        event_store=EventStore(tmp_path / "events.jsonl"),
        reset_session=lambda agent_id, session_id: sessions.reset(agent_id, session_id),
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "/new", "agent_id": "main"})

    assert not called
    assert sessions.load("main", "local:peer_1").messages == []
    assert result.outbound.text == "Session reinitialisee. On repart proprement."


def test_gateway_handles_model_command_without_turn_runner() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        command_callbacks={"model_summary": lambda agent_id: f"Modele de {agent_id}"},
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "/model", "agent_id": "main"})

    assert not called
    assert result.outbound.text == "Modele de main"
    assert result.outbound.metadata["command"] == "/model"


def test_gateway_can_record_command_exchange() -> None:
    recorded = []

    router = MessageRouter(
        run_turn=lambda **_kwargs: DummyTurn(),
        command_callbacks={"model_summary": lambda agent_id: f"Modele de {agent_id}"},
        record_exchange=lambda inbound, outbound, include_user: recorded.append(
            (inbound.text, outbound.text, include_user)
        ),
    )

    router.handle({"channel": "web", "peer_id": "peer_1", "text": "/model", "agent_id": "main"})

    assert recorded == [("/model", "Modele de main", True)]


def test_gateway_command_can_trigger_agent_turn() -> None:
    calls = []

    def run_turn(**kwargs):
        calls.append(kwargs)
        return DummyTurn()

    registry = CommandRegistry()
    registry.register(
        RuntimeCommand(
            name="/dev",
            description="executer le plan",
            owner="dev",
            handler=lambda _context: CommandResult(
                text="Je lance le dev.",
                metadata={"command": "/dev", "agent_prompt": "Execute le plan maintenant."},
            ),
        )
    )

    result = MessageRouter(run_turn=run_turn, command_registry=registry).handle(
        {"channel": "web", "peer_id": "peer_1", "text": "/dev", "agent_id": "main"}
    )

    assert calls[0]["message"] == "Execute le plan maintenant."
    assert calls[0]["source_metadata"]["command"] == "/dev"
    assert calls[0]["source_metadata"]["original_text"] == "/dev"
    assert calls[0]["message_metadata"]["autonomy_internal"] is True
    assert calls[0]["message_metadata"]["visible_user_message"] == "/dev"
    assert result.outbound.text == "Salut"
    assert result.outbound.metadata["command"] == "/dev"


def test_gateway_agent_prompt_command_does_not_force_tool_retry() -> None:
    calls = []

    class IntentTurn:
        assistant_text = "Je commence par inspecter le projet existant."
        tool_results = []
        status = "completed"

    def run_turn(**kwargs):
        calls.append(kwargs)
        return IntentTurn()

    registry = CommandRegistry()
    registry.register(
        RuntimeCommand(
            name="/dev",
            description="executer le plan",
            owner="dev",
            handler=lambda _context: CommandResult(
                text="Je lance le dev.",
                metadata={
                    "command": "/dev",
                    "agent_prompt": "Execute le plan maintenant.",
                    "agent_limits": {"max_tool_iterations": 12},
                },
            ),
        )
    )

    result = MessageRouter(run_turn=run_turn, command_registry=registry).handle(
        {"channel": "web", "peer_id": "peer_1", "text": "/dev", "agent_id": "main"}
    )

    assert len(calls) == 1
    assert calls[0]["message"] == "Execute le plan maintenant."
    assert calls[0]["limits"] == {"max_tool_iterations": 12}
    assert result.outbound.text == "Je commence par inspecter le projet existant."


def test_gateway_agent_prompt_command_can_continue_until_activity() -> None:
    calls = []

    class IntentTurn:
        assistant_text = "Je commence par inspecter le projet existant."
        tool_results = []
        status = "completed"

    def run_turn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return IntentTurn()
        return ToolOnlyTurn()

    registry = CommandRegistry()
    registry.register(
        RuntimeCommand(
            name="/dev",
            description="executer le plan",
            owner="dev",
            handler=lambda _context: CommandResult(
                text="Je lance le dev.",
                metadata={
                    "command": "/dev",
                    "agent_prompt": "Execute le plan maintenant.",
                    "agent_limits": {"max_tool_iterations": 12},
                    "autonomy": {"requires_activity": True, "max_continuations": 2},
                },
            ),
        )
    )

    result = MessageRouter(run_turn=run_turn, command_registry=registry).handle(
        {"channel": "web", "peer_id": "peer_1", "text": "/dev", "agent_id": "main"}
    )

    assert len(calls) == 2
    assert calls[0]["message"] == "Execute le plan maintenant."
    assert "Continue en mode autonome" in calls[1]["message"]
    assert calls[1]["source_metadata"]["autonomy_continuation"] == 1
    assert result.outbound.text == "Listed 3 entries: AGENTS.md, DECISIONS.md, PLAN.md"


def test_gateway_agent_prompt_command_can_continue_on_non_action_text() -> None:
    calls = []

    class TextOnlyTurn:
        assistant_text = "La premiere decision est deja tranchee dans DECISIONS.md."
        tool_results = []
        status = "completed"

    class DoneTurn:
        assistant_text = "Tout est fait."
        tool_results = []
        status = "completed"

    def run_turn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return TextOnlyTurn()
        if len(calls) == 2:
            return ToolOnlyTurn()
        return DoneTurn()

    registry = CommandRegistry()
    registry.register(
        RuntimeCommand(
            name="/dev",
            description="executer le plan",
            owner="dev",
            handler=lambda _context: CommandResult(
                text="Je lance le dev.",
                metadata={
                    "command": "/dev",
                    "agent_prompt": "Execute le plan maintenant.",
                    "autonomy": {
                        "requires_activity": True,
                        "continue_without_activity": True,
                        "max_continuations": 4,
                    },
                },
            ),
        )
    )

    result = MessageRouter(run_turn=run_turn, command_registry=registry).handle(
        {"channel": "web", "peer_id": "peer_1", "text": "/dev", "agent_id": "main"}
    )

    assert len(calls) == 3
    assert result.outbound.text == "Tout est fait."


def test_gateway_agent_prompt_command_reports_no_activity_after_continuations() -> None:
    calls = []

    class IntentTurn:
        assistant_text = "Je corrige :"
        tool_results = []
        status = "completed"

    def run_turn(**kwargs):
        calls.append(kwargs)
        return IntentTurn()

    registry = CommandRegistry()
    registry.register(
        RuntimeCommand(
            name="/dev",
            description="executer le plan",
            owner="dev",
            handler=lambda _context: CommandResult(
                text="Je lance le dev.",
                metadata={
                    "command": "/dev",
                    "agent_prompt": "Execute le plan maintenant.",
                    "autonomy": {
                        "requires_activity": True,
                        "max_continuations": 2,
                        "no_activity_text": "Pas d'activite concrete.",
                    },
                },
            ),
        )
    )

    result = MessageRouter(run_turn=run_turn, command_registry=registry).handle(
        {"channel": "web", "peer_id": "peer_1", "text": "/dev", "agent_id": "main"}
    )

    assert len(calls) == 3
    assert result.outbound.text == "Pas d'activite concrete."


def test_gateway_skips_recording_when_persist_metadata_is_false() -> None:
    recorded = []

    router = MessageRouter(
        run_turn=lambda **_kwargs: DummyTurn(),
        record_exchange=lambda inbound, outbound, include_user: recorded.append(
            (inbound.text, outbound.text, include_user)
        ),
    )

    router.handle(
        {
            "channel": "web",
            "peer_id": "peer_1",
            "text": "/help",
            "agent_id": "main",
            "metadata": {"persist": False},
        }
    )

    assert recorded == []


def test_gateway_records_tool_only_outbound_without_duplicate_user() -> None:
    recorded = []

    router = MessageRouter(
        run_turn=lambda **_kwargs: ToolOnlyTurn(),
        record_exchange=lambda inbound, outbound, include_user: recorded.append(
            (inbound.text, outbound.text, include_user)
        ),
    )

    result = router.handle({"channel": "web", "peer_id": "peer_1", "text": "liste", "agent_id": "main"})

    assert result.outbound.text == "Listed 3 entries: AGENTS.md, DECISIONS.md, PLAN.md"
    assert result.outbound.metadata["kernel_correlation_id"] == "turn_tool_only"
    assert recorded == [("liste", "Listed 3 entries: AGENTS.md, DECISIONS.md, PLAN.md", False)]


def test_gateway_recorded_tool_only_reply_keeps_kernel_correlation_id(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create("main", session_id="web:main:1")
    _record_gateway_exchange_in_store(
        store,
        InboundMessage(channel="web", peer_id="web:main:1", text="veille"),
        OutboundMessage(
            channel="web",
            peer_id="web:main:1",
            agent_id="main",
            session_id="web:main:1",
            correlation_id="corr_gateway",
            text="Autorisation requise.",
            metadata={"kernel_correlation_id": "turn_kernel"},
        ),
        include_user=False,
    )

    session = store.load("main", "web:main:1")

    assert session.messages[-1].role == "assistant"
    assert session.messages[-1].correlation_id == "turn_kernel"


def test_gateway_handles_compact_command_without_turn_runner() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        command_callbacks={"compact_session": lambda agent_id, session_id: f"compact {agent_id} {session_id}"},
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "/compact", "agent_id": "main"})

    assert not called
    assert result.outbound.text == "compact main local:peer_1"
    assert result.outbound.metadata["command"] == "/compact"


def test_gateway_intercepts_message_before_turn_runner() -> None:
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        intercept_message=lambda message, agent_id, session_id, correlation_id: "Wizard reply",
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "nouvel agent"})

    assert not called
    assert result.outbound.text == "Wizard reply"
    assert result.outbound.metadata["intercepted"] is True


def test_gateway_approves_pending_action_before_rerun(tmp_path) -> None:
    approvals = ApprovalStore(tmp_path / "approvals.json")
    approval = approvals.request(
        agent_id="main",
        session_id="local:peer_1",
        correlation_id="corr_old",
        tool_name="host.agent_create",
        permission_class="host.control",
        scope={"actions": ["agents.create"]},
        arguments={"agent_id": "coding"},
        summary="Approve agent creation",
        reason="Host control asks before agent creation.",
    )
    calls = []

    def run_turn(**kwargs):
        calls.append(kwargs)
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        approval_store_for=lambda _agent_id: approvals,
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "ok", "agent_id": "main"})

    assert approvals.list()[0].status == "approved"
    assert approvals.list()[0].id == approval.id
    assert calls[0]["message"] == "ok"
    assert result.outbound.text == "Salut"


def test_gateway_can_approve_pending_action_for_session(tmp_path) -> None:
    approvals = ApprovalStore(tmp_path / "approvals.json")
    approval = approvals.request(
        agent_id="main",
        session_id="local:peer_1",
        correlation_id="corr_old",
        tool_name="web.search",
        permission_class="network.outbound",
        scope={"hosts": ["*"]},
        arguments={"query": "first"},
        summary="Approve search",
        reason="Network asks before search.",
        rememberable=True,
    )

    router = MessageRouter(
        run_turn=lambda **_kwargs: DummyTurn(),
        approval_store_for=lambda _agent_id: approvals,
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "session", "agent_id": "main"})

    approved = approvals.list(status="approved")
    assert approved[0].id == approval.id
    assert any(item.replay_scope == "tool_session" for item in approved)
    assert approvals.approved_for_replay(
        permission_class="network.outbound",
        scope={"hosts": ["*"]},
        tool_name="web.search",
        arguments={"query": "second"},
        agent_id="main",
        session_id="local:peer_1",
    )
    assert result.outbound.text == "Salut"


def test_gateway_denies_pending_action_without_rerun(tmp_path) -> None:
    approvals = ApprovalStore(tmp_path / "approvals.json")
    approvals.request(
        agent_id="main",
        session_id="local:peer_1",
        correlation_id="corr_old",
        tool_name="host.agent_create",
        permission_class="host.control",
        scope={"actions": ["agents.create"]},
        arguments={"agent_id": "coding"},
        summary="Approve agent creation",
        reason="Host control asks before agent creation.",
    )
    called = False

    def run_turn(**_kwargs):
        nonlocal called
        called = True
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        approval_store_for=lambda _agent_id: approvals,
    )

    result = router.handle({"channel": "local", "peer_id": "peer_1", "text": "non", "agent_id": "main"})

    assert approvals.list()[0].status == "denied"
    assert not called
    assert result.outbound.text == "Action refusee."


def test_gateway_http_server_health_and_message() -> None:
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            assert json.loads(response.read().decode("utf-8")) == {
                "ok": True,
                "status": "ready",
            }

        req = request.Request(
            f"http://{host}:{port}/message",
            data=json.dumps(
                {"channel": "http", "peer_id": "peer_1", "text": "Bonjour"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert payload["result"]["outbound"]["text"] == "Salut"
        assert payload["result"]["outbound"]["session_id"] == "http:peer_1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_serves_web_ui_and_agents() -> None:
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_agents=lambda: [{"id": "main", "provider": "mock", "model": "mock"}],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with request.urlopen(f"http://{host}:{port}/api/agents", timeout=5) as response:
            agents = json.loads(response.read().decode("utf-8"))

        assert "<title>Maurice</title>" in html
        assert "maurice_web_agent_id" in html
        assert 'id="upload-btn"' in html
        assert 'id="image-upload"' in html
        assert 'addEventListener("paste"' in html
        assert "clipboardData" in html
        assert "user-attachments" in html
        assert "/api/attachment" in html
        assert agents["agents"] == [{"id": "main", "provider": "mock", "model": "mock"}]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_can_require_web_token() -> None:
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_agents=lambda: [{"id": "main"}],
        web_token="secret-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        try:
            request.urlopen(f"http://{host}:{port}/api/agents", timeout=5)
            assert False, "expected forbidden response"
        except Exception as exc:
            assert getattr(exc, "code", None) == 403

        req = request.Request(
            f"http://{host}:{port}/api/agents",
            headers={"X-Maurice-Token": "secret-token"},
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert payload["agents"] == [{"id": "main"}]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_can_serve_local_context(tmp_path, monkeypatch) -> None:
    ctx = resolve_local_context(tmp_path)
    calls = []

    def fake_run_turn(ctx_arg, **kwargs):
        calls.append({"ctx": ctx_arg, **kwargs})
        return DummyTurn()

    monkeypatch.setattr("maurice.host.commands.gateway_server._run_turn_via_local_server", fake_run_turn)
    server = _build_gateway_http_server_for_context(
        ctx,
        agent_id=None,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/api/agents", timeout=5) as response:
            agents = json.loads(response.read().decode("utf-8"))
        req = request.Request(
            f"http://{host}:{port}/api/message",
            data=json.dumps(
                {
                    "agent_id": "main",
                    "session_id": "web:main:1",
                    "peer_id": "web:main:1",
                    "message": "Bonjour",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert agents["agents"] == [
            {"id": "main", "provider": "mock", "model": "mock", "default": True, "scope": "local"}
        ]
        assert payload["ok"] is True
        assert payload["result"]["outbound"]["text"] == "Salut"
        assert calls[0]["ctx"].scope == "local"
        assert calls[0]["ctx"].content_root == tmp_path.resolve()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_global_port_zero_means_free_port(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    captured = {}

    class FakeGatewayHttpServer:
        address = ("127.0.0.1", 43212)

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "maurice.host.commands.gateway_server.GatewayHttpServer",
        FakeGatewayHttpServer,
    )

    _build_gateway_http_server(
        workspace,
        agent_id=None,
        host="127.0.0.1",
        port=0,
    )

    assert captured["port"] == 0


def test_gateway_http_server_serves_web_session_history_and_reset(tmp_path) -> None:
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create("main", session_id="web:main:1")
    sessions.append_message("main", "web:main:1", role="user", content="Bonjour")

    def history(agent_id: str, session_id: str):
        session = sessions.load(agent_id, session_id)
        return [{"role": message.role, "content": message.content} for message in session.messages]

    def reset(agent_id: str, session_id: str):
        sessions.reset(agent_id, session_id)
        return True

    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_session_history=history,
        web_session_reset=reset,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/api/session?agent_id=main&session_id=web%3Amain%3A1", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        req = request.Request(
            f"http://{host}:{port}/api/session?agent_id=main&session_id=web%3Amain%3A1",
            method="DELETE",
        )
        with request.urlopen(req, timeout=5) as response:
            deleted = json.loads(response.read().decode("utf-8"))

        assert payload["messages"] == [{"role": "user", "content": "Bonjour"}]
        assert deleted["deleted"] is True
        assert sessions.load("main", "web:main:1").messages == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_serves_git_status_and_diff() -> None:
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_git_status=lambda: {
            "ok": True,
            "available": True,
            "files": [{"path": "README.md", "insertions": 1, "deletions": 0}],
        },
        web_git_diff=lambda path: {"ok": True, "path": path, "diff": "+hello"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/api/git/status", timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
        with request.urlopen(f"http://{host}:{port}/api/git/diff?path=README.md", timeout=5) as response:
            diff = json.loads(response.read().decode("utf-8"))

        assert status["available"] is True
        assert status["files"][0]["path"] == "README.md"
        assert diff == {"ok": True, "path": "README.md", "diff": "+hello"}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_serves_and_updates_model() -> None:
    updated = []
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_model_status=lambda agent_id: {
            "ok": True,
            "available": True,
            "agent_id": agent_id,
            "provider": "auth",
            "model": updated[-1] if updated else "gpt-5",
            "choices": [
                {"id": "gpt-5", "label": "GPT-5", "current": not updated},
                {"id": "gpt-5.4", "label": "GPT-5.4", "current": bool(updated)},
            ],
        },
        web_model_update=lambda agent_id, model: updated.append(model) or {
            "ok": True,
            "available": True,
            "agent_id": agent_id,
            "provider": "auth",
            "model": model,
            "choices": [],
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/api/model?agent_id=main", timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
        req = request.Request(
            f"http://{host}:{port}/api/model",
            data=json.dumps({"agent_id": "main", "model": "gpt-5.4"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            changed = json.loads(response.read().decode("utf-8"))

        assert status["model"] == "gpt-5"
        assert changed["model"] == "gpt-5.4"
        assert updated == ["gpt-5.4"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_model_update_writes_default_agent_kernel_model(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    kernel_data.setdefault("kernel", {})["model"] = {
        "provider": "auth",
        "protocol": "chatgpt_codex",
        "name": "gpt-5",
        "base_url": None,
        "credential": "chatgpt",
    }
    write_yaml_file(kernel_config_path(workspace), kernel_data)
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server.chatgpt_model_choices",
        lambda: [("gpt-5", "GPT-5"), ("gpt-5.4", "GPT-5.4")],
    )

    status = _gateway_model_status(workspace, "main")
    changed = _gateway_model_update(workspace, "main", "gpt-5.4")
    bundle = load_workspace_config(workspace)

    assert status["model"] == "gpt-5"
    assert changed["model"] == "gpt-5.4"
    assert bundle.kernel.model.name == "gpt-5.4"


def test_gateway_model_status_labels_include_model_id(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    kernel_data = read_yaml_file(kernel_config_path(workspace))
    kernel_data.setdefault("kernel", {})["model"] = {
        "provider": "ollama",
        "protocol": "ollama_chat",
        "name": "minimax-m2:latest",
        "base_url": "https://ollama.com",
        "credential": None,
    }
    write_yaml_file(kernel_config_path(workspace), kernel_data)
    monkeypatch.setattr(
        "maurice.host.commands.gateway_server.ollama_model_choices",
        lambda _base_url, api_key="": [("minimax-m2:latest", "447.8 GB")],
    )

    status = _gateway_model_status(workspace, "main")

    assert status["choices"][0]["id"] == "minimax-m2:latest"
    assert status["choices"][0]["label"] == "minimax-m2:latest - 447.8 GB"


def test_gateway_agent_config_update_replaces_skills_and_permission(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(
        workspace,
        agent_id="maurice_polo",
        permission_profile="limited",
        skills=["filesystem", "memory"],
    )

    status = _gateway_agent_config_status(workspace, "maurice_polo")
    changed = _gateway_agent_config_update(
        workspace,
        "maurice_polo",
        {
            "permission_profile": "safe",
            "skills": ["filesystem", "explore", "dev"],
        },
    )
    bundle = load_workspace_config(workspace)

    assert any(skill["id"] == "explore" for skill in status["skills"])
    assert changed["updated"] is True
    assert bundle.agents.agents["maurice_polo"].permission_profile == "safe"
    assert bundle.agents.agents["maurice_polo"].skills == ["filesystem", "explore", "dev"]


def test_gateway_agent_config_update_changes_provider_and_telegram(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")
    create_agent(workspace, agent_id="maurice_polo", permission_profile="limited")

    changed = _gateway_agent_config_update(
        workspace,
        "maurice_polo",
        {
            "provider": "openai_api",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "openai-secret",
            "web_search_base_url": "https://search.example",
            "telegram_enabled": True,
            "telegram_token": "telegram-secret",
            "telegram_allowed_users": "123, 456",
        },
    )
    bundle = load_workspace_config(workspace)
    agent = bundle.agents.agents["maurice_polo"]
    credentials = load_workspace_credentials(workspace).credentials
    telegram = bundle.host.channels["telegram_maurice_polo"]
    skills_data = read_yaml_file(workspace / "skills.yaml")

    assert changed["updated"] is True
    assert changed["web_search"]["configured"] is True
    assert changed["web_search"]["base_url"] == "https://search.example"
    assert agent.model["provider"] == "api"
    assert agent.model["protocol"] == "openai_chat_completions"
    assert agent.model["name"] == "gpt-4o-mini"
    assert "openai" in agent.credentials
    assert credentials["openai"].value == "openai-secret"
    assert credentials["telegram_bot_maurice_polo"].value == "telegram-secret"
    assert telegram["enabled"] is True
    assert telegram["allowed_users"] == [123, 456]
    assert "telegram" in agent.channels
    assert skills_data["skills"]["web"]["base_url"] == "https://search.example"


def test_gateway_agent_config_update_rejects_invalid_web_search_url(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    (runtime / "maurice").mkdir(parents=True)
    initialize_workspace(workspace, runtime, permission_profile="limited")

    changed = _gateway_agent_config_update(
        workspace,
        "main",
        {"web_search_base_url": "not-a-url"},
    )

    assert changed == {"ok": False, "error": "invalid_web_search_url"}


def test_gateway_http_server_agent_config_endpoint() -> None:
    updated = []
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
        web_agent_config=lambda agent_id: {
            "ok": True,
            "available": True,
            "agent": {"id": agent_id, "permission_profile": "limited", "skills": ["filesystem"]},
            "permissions": [],
            "skills": [{"id": "filesystem", "description": "files", "enabled": True}],
            "model": {"choices": []},
            "telegram": {"enabled": False},
        },
        web_agent_update=lambda agent_id, payload: updated.append((agent_id, payload["skills"])) or {
            "ok": True,
            "updated": True,
            "agent": {"id": agent_id},
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        with request.urlopen(f"http://{host}:{port}/api/agent?agent_id=main", timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
        req = request.Request(
            f"http://{host}:{port}/api/agent",
            data=json.dumps({"agent_id": "main", "skills": ["filesystem", "dev"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            changed = json.loads(response.read().decode("utf-8"))

        assert status["agent"]["id"] == "main"
        assert changed["updated"] is True
        assert updated == [("main", ["filesystem", "dev"])]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_web_session_history_hides_internal_system_messages(tmp_path) -> None:
    from maurice.host.cli import _gateway_web_session_history

    sessions = SessionStore(tmp_path / "sessions")
    sessions.create("main", session_id="web:main:1")
    sessions.append_message(
        "main",
        "web:main:1",
        role="system",
        content="Session compactee localement. Internal summary.",
        metadata={"compacted": True},
    )
    sessions.append_message(
        "main",
        "web:main:1",
        role="user",
        content="Commande interne `/dev` : execute le plan.",
    )
    sessions.append_message("main", "web:main:1", role="user", content="Commande interne `/dev` legacy.")
    sessions.append_message("main", "web:main:1", role="user", content="Commande interne `/dev` : execute le plan.", metadata={"internal": True})
    sessions.append_message("main", "web:main:1", role="user", content="Bonjour")
    sessions.append_message("main", "web:main:1", role="assistant", content="Salut")

    assert _gateway_web_session_history(tmp_path, "main", "web:main:1") == [
        {
            "role": "user",
            "content": "Bonjour",
            "created_at": sessions.load("main", "web:main:1").messages[4].created_at.isoformat(),
            "metadata": {},
        },
        {
            "role": "assistant",
            "content": "Salut",
            "created_at": sessions.load("main", "web:main:1").messages[5].created_at.isoformat(),
            "metadata": {},
        },
    ]


def test_gateway_web_session_history_attaches_tool_activity(tmp_path) -> None:
    from maurice.host.cli import _gateway_web_session_history

    sessions = SessionStore(tmp_path / "sessions")
    sessions.create("main", session_id="web:main:1")
    sessions.append_message(
        "main",
        "web:main:1",
        role="user",
        content="Lis le README",
        correlation_id="corr_1",
    )
    sessions.append_message(
        "main",
        "web:main:1",
        role="tool_call",
        content="",
        correlation_id="corr_1",
        metadata={
            "tool_call_id": "call_1",
            "tool_name": "filesystem.read",
            "tool_arguments": {"path": "README.md"},
        },
    )
    sessions.append_message(
        "main",
        "web:main:1",
        role="tool",
        content="README lu.",
        correlation_id="corr_1",
        metadata={"tool_call_id": "call_1", "tool_name": "filesystem.read", "ok": True},
    )
    sessions.append_message(
        "main",
        "web:main:1",
        role="assistant",
        content="Voici le resume.",
        correlation_id="corr_1",
        metadata={
            "context": {
                "total_tokens": 65_000,
                "context_window_tokens": 128_000,
                "percent": 51,
                "visual_ratio": 0.5078125,
                "level": "medium",
            }
        },
    )

    history = _gateway_web_session_history(tmp_path, "main", "web:main:1")

    assert history[1]["tools"] == [
        {
            "id": "call_1",
            "name": "filesystem.read",
            "label": "lire un fichier `README.md`",
            "status": "ok",
            "ok": True,
            "summary": "README lu.",
            "created_at": history[1]["tools"][0]["created_at"],
            "completed_at": history[1]["tools"][0]["completed_at"],
        }
    ]
    assert history[1]["metadata"]["context"]["total_tokens"] == 65_000


def test_telegram_response_text_appends_context_summary() -> None:
    text = _telegram_response_text(
        "Salut",
        {
            "context": {
                "total_tokens": 65_000,
                "context_window_tokens": 128_000,
                "percent": 51,
            }
        },
    )

    assert text == "Salut\n\ncontext 65,000 / 128,000 tokens (51%)"


def test_gateway_http_server_web_api_message_routes_to_router() -> None:
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        req = request.Request(
            f"http://{host}:{port}/api/message",
            data=json.dumps(
                {
                    "agent_id": "main",
                    "session_id": "web:main:1",
                    "peer_id": "web:main:1",
                    "message": "Bonjour",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert payload["result"]["outbound"]["text"] == "Salut"
        assert payload["result"]["outbound"]["session_id"] == "web:main:1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_web_api_message_accepts_image_upload(tmp_path) -> None:
    seen = {}

    def run_turn(**kwargs):
        seen.update(kwargs)
        return DummyTurn()

    def web_uploads(agent_id, session_id, attachments):
        return _store_web_uploads(
            tmp_path / ".maurice" / "prov" / "uploads" / agent_id,
            session_id=session_id,
            attachments=attachments,
        )

    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=run_turn),
        web_uploads=web_uploads,
        web_attachment=_read_web_attachment,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        data_url = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")
        req = request.Request(
            f"http://{host}:{port}/api/message",
            data=json.dumps(
                {
                    "agent_id": "main",
                    "session_id": "web:main:1",
                    "peer_id": "web:main:1",
                    "message": "Que vois-tu ?",
                    "attachments": [
                        {"name": "pixel.png", "type": "image/png", "data": data_url}
                    ],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert "vision.analyze" in seen["message"]
        assert "pixel.png" in seen["message"]
        assert data_url not in seen["message"]
        assert "base64" not in seen["message"]
        assert seen["message_metadata"]["visible_user_message"] == "Que vois-tu ?"
        assert seen["message_metadata"]["attachments"][0]["name"] == "pixel.png"
        assert "data" not in seen["message_metadata"]["attachments"][0]
        assert "data" not in seen["source_metadata"]["attachments"][0]
        assert "data" not in payload["result"]["inbound"]["metadata"]["attachments"][0]
        stored_path = payload["result"]["inbound"]["metadata"]["attachments"][0]["path"]
        assert "/.maurice/prov/uploads/" in stored_path
        assert Path(stored_path).read_bytes() == PNG_1X1
        with request.urlopen(
            f"http://{host}:{port}/api/attachment?path={stored_path}",
            timeout=5,
        ) as response:
            assert response.headers["Content-Type"] == "image/png"
            assert response.read() == PNG_1X1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_can_disable_web_message_persistence() -> None:
    seen = {}

    def run_turn(**_kwargs):
        return DummyTurn()

    router = MessageRouter(
        run_turn=run_turn,
        record_exchange=lambda inbound, outbound, include_user: seen.update(
            {"persist": inbound.metadata.get("persist")}
        ),
    )
    server = GatewayHttpServer(host="127.0.0.1", port=0, router=router)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        req = request.Request(
            f"http://{host}:{port}/api/message",
            data=json.dumps(
                {
                    "agent_id": "main",
                    "session_id": "web:main:1",
                    "peer_id": "web:main:1",
                    "message": "/help",
                    "persist": False,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert seen == {}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_gateway_http_server_routes_channel_adapter_message(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    server = GatewayHttpServer(
        host="127.0.0.1",
        port=0,
        router=MessageRouter(run_turn=lambda **_kwargs: DummyTurn(), event_store=event_store),
        channels=ChannelAdapterRegistry.from_config(
            {"local_http": {"adapter": "local_http", "enabled": True, "agent": "main"}}
        ),
        event_store=event_store,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.address
        req = request.Request(
            f"http://{host}:{port}/channels/local_http/message",
            data=json.dumps({"peer": "browser", "message": "Bonjour"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert payload["result"]["outbound"]["text"] == "Salut"
        assert payload["delivery"]["status"] == "delivered"
        assert [event.name for event in event_store.read_all()] == [
            "gateway.message.received",
            "gateway.message.sent",
            "channel.delivery.succeeded",
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)

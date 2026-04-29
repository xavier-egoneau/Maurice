from __future__ import annotations

import json
import threading
from urllib import request

from maurice.host.command_registry import CommandRegistry, CommandResult, RuntimeCommand, core_commands
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.gateway import InboundMessage, MessageRouter
from maurice.host.gateway import GatewayHttpServer
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.contracts import ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.session import SessionStore


class DummyTurn:
    assistant_text = "Salut"
    tool_results = []
    status = "completed"


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
    assert recorded == [("liste", "Listed 3 entries: AGENTS.md, DECISIONS.md, PLAN.md", False)]


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
        assert agents["agents"] == [{"id": "main", "provider": "mock", "model": "mock"}]
    finally:
        server.shutdown()
        thread.join(timeout=5)


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

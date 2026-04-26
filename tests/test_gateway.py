from __future__ import annotations

import json
import threading
from urllib import request

from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.gateway import InboundMessage, MessageRouter
from maurice.host.gateway import GatewayHttpServer
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.events import EventStore
from maurice.kernel.session import SessionStore


class DummyTurn:
    assistant_text = "Salut"
    tool_results = []
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

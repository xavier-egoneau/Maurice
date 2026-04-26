"""Channel-neutral gateway contracts and routing."""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.kernel.contracts import MauriceModel
from maurice.kernel.events import EventStore
from maurice.kernel.loop import TurnResult


def new_correlation_id() -> str:
    return f"corr_{uuid4().hex}"


class InboundMessage(MauriceModel):
    channel: str
    peer_id: str
    text: str
    agent_id: str = "main"
    session_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundMessage(MauriceModel):
    channel: str
    peer_id: str
    agent_id: str
    session_id: str
    correlation_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayResult(MauriceModel):
    inbound: InboundMessage
    outbound: OutboundMessage
    status: str


RunTurn = Callable[..., TurnResult]


class MessageRouter:
    """Route channel-neutral inbound messages to the configured agent runtime."""

    def __init__(
        self,
        *,
        run_turn: RunTurn,
        event_store: EventStore | None = None,
        default_agent_id: str = "main",
    ) -> None:
        self.run_turn = run_turn
        self.event_store = event_store
        self.default_agent_id = default_agent_id

    def handle(self, inbound: InboundMessage | dict[str, Any]) -> GatewayResult:
        message = InboundMessage.model_validate(inbound)
        agent_id = message.agent_id or self.default_agent_id
        session_id = message.session_id or self._session_id(message)
        correlation_id = message.correlation_id or new_correlation_id()
        self._emit(
            "gateway.message.received",
            message,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )

        if message.text.strip() in ("/new", "/reset"):
            outbound = OutboundMessage(
                channel=message.channel,
                peer_id=message.peer_id,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                text="Session reset is not implemented in the gateway yet.",
            )
            self._emit(
                "gateway.message.sent",
                message,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={"text": outbound.text},
            )
            return GatewayResult(inbound=message, outbound=outbound, status="completed")

        if message.text.strip() == "/help":
            outbound = OutboundMessage(
                channel=message.channel,
                peer_id=message.peer_id,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                text="/new - reset the session\n/help - show this help",
            )
            self._emit(
                "gateway.message.sent",
                message,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={"text": outbound.text},
            )
            return GatewayResult(inbound=message, outbound=outbound, status="completed")

        turn = self.run_turn(
            message=message.text,
            session_id=session_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
        )
        text = turn.assistant_text or "\n".join(result.summary for result in turn.tool_results)
        outbound = OutboundMessage(
            channel=message.channel,
            peer_id=message.peer_id,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            text=text,
            metadata={"turn_status": turn.status},
        )
        self._emit(
            "gateway.message.sent",
            message,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"text": outbound.text, "turn_status": turn.status},
        )
        return GatewayResult(inbound=message, outbound=outbound, status=turn.status)

    def _session_id(self, message: InboundMessage) -> str:
        return f"{message.channel}:{message.peer_id}"

    def _emit(
        self,
        name: str,
        message: InboundMessage,
        *,
        agent_id: str,
        session_id: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.event_store is None:
            return
        self.event_store.emit(
            name=name,
            kind="progress",
            origin="host.gateway",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={
                "channel": message.channel,
                "peer_id": message.peer_id,
                **(payload or {"text": message.text}),
            },
        )


class GatewayHttpServer:
    """Small stdlib HTTP server for channel-neutral local gateway traffic."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        router: MessageRouter,
        channels: Any | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.router = router
        self.channels = channels
        self.event_store = event_store
        handler = self._handler_class(router, channels, event_store)
        self.server = ThreadingHTTPServer((host, port), handler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _handler_class(
        router: MessageRouter,
        channels: Any | None,
        event_store: EventStore | None,
    ):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health":
                    self._write_json(404, {"ok": False, "error": "not_found"})
                    return
                self._write_json(200, {"ok": True, "status": "ready"})

            def do_POST(self) -> None:
                if self.path != "/message":
                    channel = self._channel_path()
                    if channel is None or channels is None:
                        self._write_json(404, {"ok": False, "error": "not_found"})
                        return
                    try:
                        payload = self._read_payload()
                        inbound = channels.normalize(channel, payload)
                        result = router.handle(inbound)
                        delivery = channels.deliver(result, event_store=event_store)
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "result": result.model_dump(mode="json"),
                            "delivery": delivery.model_dump(mode="json"),
                        },
                    )
                    return
                try:
                    payload = self._read_payload()
                    result = router.handle(payload)
                except Exception as exc:
                    self._write_json(400, {"ok": False, "error": str(exc)})
                    return
                self._write_json(200, {"ok": True, "result": result.model_dump(mode="json")})

            def _read_payload(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("JSON payload must be an object.")
                return payload

            def _channel_path(self) -> str | None:
                prefix = "/channels/"
                suffix = "/message"
                if not self.path.startswith(prefix) or not self.path.endswith(suffix):
                    return None
                channel = self.path[len(prefix) : -len(suffix)]
                return channel or None

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        return Handler

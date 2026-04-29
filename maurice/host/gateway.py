"""Channel-neutral gateway contracts and routing."""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.host.command_registry import CommandContext, CommandRegistry, default_command_registry
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
ApprovalStoreFor = Callable[[str], Any]
ResetSession = Callable[[str, str], None]
InterceptMessage = Callable[[InboundMessage, str, str, str], str | None]
RecordExchange = Callable[[InboundMessage, OutboundMessage, bool], None]


APPROVAL_ACCEPT_WORDS = {"ok", "oui", "yes", "y", "go", "vas-y", "vasy", "approve", "approuve"}
APPROVAL_DENY_WORDS = {"non", "no", "n", "deny", "refuse", "annule", "annuler", "stop"}


class MessageRouter:
    """Route channel-neutral inbound messages to the configured agent runtime."""

    def __init__(
        self,
        *,
        run_turn: RunTurn,
        event_store: EventStore | None = None,
        default_agent_id: str = "main",
        approval_store_for: ApprovalStoreFor | None = None,
        reset_session: ResetSession | None = None,
        intercept_message: InterceptMessage | None = None,
        command_registry: CommandRegistry | None = None,
        command_callbacks: dict[str, Callable[..., Any]] | None = None,
        record_exchange: RecordExchange | None = None,
    ) -> None:
        self.run_turn = run_turn
        self.event_store = event_store
        self.default_agent_id = default_agent_id
        self.approval_store_for = approval_store_for
        self.reset_session = reset_session
        self.intercept_message = intercept_message
        self.command_registry = command_registry or default_command_registry()
        self.command_callbacks = command_callbacks or {}
        self.record_exchange = record_exchange

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

        command_result = self.command_registry.dispatch(
            CommandContext(
                message_text=message.text,
                channel=message.channel,
                peer_id=message.peer_id,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                callbacks={
                    "command_registry": self.command_registry,
                    "reset_session": self.reset_session,
                    **self.command_callbacks,
                },
            )
        )
        if command_result is not None:
            if command_result.metadata.get("command") == "/new":
                self._emit(
                    "gateway.session.reset",
                    message,
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    payload={},
                )
            agent_prompt = command_result.metadata.get("agent_prompt")
            if isinstance(agent_prompt, str) and agent_prompt.strip():
                agent_limits = command_result.metadata.get("agent_limits")
                if not isinstance(agent_limits, dict):
                    agent_limits = {}
                autonomy = command_result.metadata.get("autonomy")
                if not isinstance(autonomy, dict):
                    autonomy = {}
                command_name = command_result.metadata.get("command") or ""
                turn = self.run_turn(
                    message=agent_prompt,
                    session_id=session_id,
                    agent_id=agent_id,
                    correlation_id=correlation_id,
                    source_channel=message.channel,
                    source_peer_id=message.peer_id,
                    source_metadata={
                        **message.metadata,
                        "command": command_name,
                        "original_text": message.text,
                    },
                    limits=agent_limits,
                    message_metadata={
                        "autonomy_internal": True,
                        "command": command_name,
                        "visible_user_message": message.text,
                    },
                )
                tool_activity = bool(turn.tool_results)
                any_tool_activity = tool_activity
                _log_autonomy_turn(command_name, 0, turn)
                max_continuations = _positive_int(autonomy.get("max_continuations"), default=0)
                max_seconds = _positive_int(autonomy.get("max_seconds"), default=0)
                continue_without_activity = autonomy.get("continue_without_activity") is True
                continuation_count = 0
                started_at = monotonic()
                while (
                    continuation_count < max_continuations
                    and (not tool_activity or continue_without_activity)
                    and (max_seconds <= 0 or monotonic() - started_at < max_seconds)
                    and _should_continue_autonomous_command(
                        turn.assistant_text,
                        continue_without_activity=continue_without_activity,
                    )
                ):
                    continuation_count += 1
                    continue_prompt = autonomy.get("continue_prompt")
                    if not isinstance(continue_prompt, str) or not continue_prompt.strip():
                        continue_prompt = (
                            "Continue en mode autonome. Tu viens d'annoncer une action sans la realiser. "
                            "Avance concretement dans le projet actif avec les capacites disponibles. "
                            "Si tu es bloque, explique le blocage precis."
                        )
                    turn = self.run_turn(
                        message=continue_prompt,
                        session_id=session_id,
                        agent_id=agent_id,
                        correlation_id=correlation_id,
                        source_channel=message.channel,
                        source_peer_id=message.peer_id,
                        source_metadata={
                            **message.metadata,
                            "command": command_name,
                            "original_text": message.text,
                            "autonomy_continuation": continuation_count,
                        },
                        limits=agent_limits,
                        message_metadata={
                            "autonomy_internal": True,
                            "command": command_name,
                            "visible_user_message": message.text,
                            "autonomy_continuation": continuation_count,
                        },
                    )
                    tool_activity = bool(turn.tool_results)
                    any_tool_activity = any_tool_activity or tool_activity
                    _log_autonomy_turn(command_name, continuation_count, turn)
                text = turn.assistant_text or "\n".join(result.summary for result in turn.tool_results)
                if autonomy.get("requires_activity") is True and not any_tool_activity:
                    no_activity_text = autonomy.get("no_activity_text")
                    if not isinstance(no_activity_text, str) or not no_activity_text.strip():
                        no_activity_text = (
                            "Je n'arrive pas a passer de l'intention a l'action avec ce modele : "
                            "il repond en texte, mais n'utilise aucune capacite pour lire ou modifier le projet. "
                            "Change de modele/provider ou relance quand le modele sait utiliser les outils."
                        )
                    text = no_activity_text
                outbound = OutboundMessage(
                    channel=message.channel,
                    peer_id=message.peer_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    text=text,
                    metadata={
                        "command": command_name,
                        "format": command_result.format,
                        "turn_status": turn.status,
                    },
                )
                self._emit(
                    "gateway.message.sent",
                    message,
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    payload={
                        "text": outbound.text,
                        "command": command_name,
                        "format": command_result.format,
                        "turn_status": turn.status,
                    },
                )
                self._record_exchange(message, outbound, include_user=True)
                return GatewayResult(inbound=message, outbound=outbound, status=turn.status)
            outbound = OutboundMessage(
                channel=message.channel,
                peer_id=message.peer_id,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                text=command_result.text,
                metadata={
                    "command": command_result.metadata.get("command"),
                    "format": command_result.format,
                },
            )
            self._emit(
                "gateway.message.sent",
                message,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={
                    "text": outbound.text,
                    "command": command_result.metadata.get("command"),
                    "format": command_result.format,
                },
            )
            self._record_exchange(message, outbound, include_user=True)
            return GatewayResult(inbound=message, outbound=outbound, status="completed")

        if self.intercept_message is not None:
            intercepted_text = self.intercept_message(message, agent_id, session_id, correlation_id)
            if intercepted_text is not None:
                outbound = OutboundMessage(
                    channel=message.channel,
                    peer_id=message.peer_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    text=intercepted_text,
                    metadata={"intercepted": True},
                )
                self._emit(
                    "gateway.message.sent",
                    message,
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    payload={"text": outbound.text, "intercepted": True},
                )
                self._record_exchange(message, outbound, include_user=True)
                return GatewayResult(inbound=message, outbound=outbound, status="completed")

        approval_result = self._resolve_pending_approval_from_message(
            message,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        if approval_result == "denied":
            outbound = OutboundMessage(
                channel=message.channel,
                peer_id=message.peer_id,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                text="Action refusee.",
                metadata={"approval": "denied"},
            )
            self._emit(
                "gateway.message.sent",
                message,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={"text": outbound.text, "approval": "denied"},
            )
            self._record_exchange(message, outbound, include_user=True)
            return GatewayResult(inbound=message, outbound=outbound, status="completed")

        turn = self.run_turn(
            message=message.text,
            session_id=session_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            source_channel=message.channel,
            source_peer_id=message.peer_id,
            source_metadata=message.metadata,
            limits={"max_tool_iterations": 8},
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
        if not turn.assistant_text and text:
            self._record_exchange(message, outbound, include_user=False)
        return GatewayResult(inbound=message, outbound=outbound, status=turn.status)

    def _record_exchange(
        self,
        inbound: InboundMessage,
        outbound: OutboundMessage,
        *,
        include_user: bool,
    ) -> None:
        if self.record_exchange is None:
            return
        if inbound.metadata.get("persist") is False:
            return
        self.record_exchange(inbound, outbound, include_user)

    def _resolve_pending_approval_from_message(
        self,
        message: InboundMessage,
        *,
        agent_id: str,
        session_id: str,
        correlation_id: str,
    ) -> str | None:
        if self.approval_store_for is None:
            return None
        normalized = message.text.strip().lower()
        if normalized not in APPROVAL_ACCEPT_WORDS and normalized not in APPROVAL_DENY_WORDS:
            return None
        store = self.approval_store_for(agent_id)
        pending = [
            approval
            for approval in store.list(status="pending")
            if approval.agent_id == agent_id and approval.session_id == session_id
        ]
        if not pending:
            return None
        approval = pending[-1]
        if normalized in APPROVAL_DENY_WORDS:
            store.deny(approval.id)
            self._emit(
                "approval.denied.from_channel",
                message,
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={"approval_id": approval.id},
            )
            return "denied"
        store.approve(approval.id)
        self._emit(
            "approval.approved.from_channel",
            message,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"approval_id": approval.id},
        )
        return "approved"

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


def _positive_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _should_continue_autonomous_command(text: str, *, continue_without_activity: bool = False) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return True
    if "?" in normalized:
        return False
    if any(marker in normalized for marker in ("bloque", "blocked", "besoin de", "il manque", "je ne peux pas")):
        return False
    done_markers = (
        "c'est fait", "c'est fini", "j'ai terminé", "j'ai termine",
        "tout est fait", "tout est terminé", "tout est termine",
        "tâche terminée", "tache terminee", "mission accomplie",
        "all done", "travail terminé", "travail termine",
    )
    if any(marker in normalized for marker in done_markers):
        return False
    if continue_without_activity:
        return True
    intent_prefixes = (
        "je commence",
        "je vais",
        "je verifie",
        "je vérifie",
        "je corrige",
        "je cree",
        "je crée",
        "je reprends",
        "je passe",
        "je demarre",
        "je démarre",
        "je lance",
    )
    if normalized.startswith(intent_prefixes):
        return True
    return normalized.endswith(":")


def _log_autonomy_turn(command: str, turn_number: int, turn: Any) -> None:
    tool_count = len(turn.tool_results)
    ok_count = sum(1 for r in turn.tool_results if r.ok)
    err_count = tool_count - ok_count
    error_codes = [r.error.code for r in turn.tool_results if r.error and r.error.code]
    write_count = sum(
        1 for r in turn.tool_results
        if r.ok and any(w in (r.summary or "") for w in ("écrit", "deplace", "cree", "mkdir"))
    )
    text_preview = (turn.assistant_text or "").strip().replace("\n", " ")[:80]
    parts = [f"[autonomy] {command} tour={turn_number} outils={ok_count}/{tool_count}"]
    if write_count:
        parts.append(f"ecritures={write_count}")
    if err_count:
        parts.append(f"erreurs={err_count} codes={error_codes}")
    if text_preview:
        parts.append(f"texte={text_preview!r}")
    print(" ".join(parts), file=sys.stderr, flush=True)


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
        web_agents: Callable[[], list[dict[str, Any]]] | None = None,
        web_session_history: Callable[[str, str], list[dict[str, Any]]] | None = None,
        web_session_reset: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.router = router
        self.channels = channels
        self.event_store = event_store
        handler = self._handler_class(
            router,
            channels,
            event_store,
            web_agents=web_agents,
            web_session_history=web_session_history,
            web_session_reset=web_session_reset,
        )
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
        *,
        web_agents: Callable[[], list[dict[str, Any]]] | None,
        web_session_history: Callable[[str, str], list[dict[str, Any]]] | None,
        web_session_reset: Callable[[str, str], bool] | None,
    ):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._write_html(200, _web_ui_html())
                    return
                if parsed.path == "/health":
                    self._write_json(200, {"ok": True, "status": "ready"})
                    return
                if parsed.path == "/api/agents":
                    agents = web_agents() if web_agents is not None else [{"id": router.default_agent_id}]
                    self._write_json(200, {"ok": True, "agents": agents})
                    return
                if parsed.path == "/api/session":
                    query = parse_qs(parsed.query)
                    agent_id = query.get("agent_id", [router.default_agent_id])[0]
                    session_id = query.get("session_id", [""])[0]
                    if not session_id:
                        self._write_json(400, {"ok": False, "error": "missing_session_id"})
                        return
                    messages = (
                        web_session_history(agent_id, session_id)
                        if web_session_history is not None
                        else []
                    )
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "messages": messages,
                        },
                    )
                    return
                self._write_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:
                if self.path == "/api/message":
                    try:
                        payload = self._read_payload()
                        result = router.handle(
                            {
                                "channel": "web",
                                "peer_id": str(payload.get("peer_id") or payload.get("session_id") or "browser"),
                                "text": str(payload.get("message") or payload.get("text") or ""),
                                "agent_id": str(payload.get("agent_id") or router.default_agent_id),
                                "session_id": payload.get("session_id"),
                                "metadata": {"persist": payload.get("persist", True) is not False},
                            }
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(200, {"ok": True, "result": result.model_dump(mode="json")})
                    return
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

            def do_DELETE(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/api/session":
                    self._write_json(404, {"ok": False, "error": "not_found"})
                    return
                query = parse_qs(parsed.query)
                agent_id = query.get("agent_id", [router.default_agent_id])[0]
                session_id = query.get("session_id", [""])[0]
                if not session_id:
                    self._write_json(400, {"ok": False, "error": "missing_session_id"})
                    return
                deleted = (
                    web_session_reset(agent_id, session_id)
                    if web_session_reset is not None
                    else False
                )
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "deleted": deleted,
                    },
                )

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

            def _write_html(self, status: int, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        return Handler


def _web_ui_html() -> str:
    return (Path(__file__).parent / "web_ui.html").read_text(encoding="utf-8")

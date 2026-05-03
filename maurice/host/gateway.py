"""Channel-neutral gateway contracts and routing."""

from __future__ import annotations

from collections.abc import Callable
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlparse
from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.host.autonomy import run_autonomous_command, should_continue_autonomous_command
from maurice.host.command_registry import CommandContext, CommandRegistry, default_command_registry
from maurice.host.context_meter import context_usage
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
WebUploads = Callable[[str, str, list[dict[str, Any]]], list[dict[str, Any]]]
WebAttachment = Callable[[str], tuple[str, bytes] | None]
WebTurnCancel = Callable[[str, str], bool]


APPROVAL_ACCEPT_WORDS = {"ok", "oui", "yes", "y", "go", "vas-y", "vasy", "approve", "approuve"}
APPROVAL_SESSION_WORDS = {
    "session",
    "ok session",
    "oui session",
    "yes session",
    "approve session",
    "approuve session",
}
APPROVAL_DENY_WORDS = {"non", "no", "n", "deny", "refuse", "annule", "annuler", "stop"}


def _turn_context_usage(turn: TurnResult) -> dict[str, Any] | None:
    return context_usage(
        int(getattr(turn, "input_tokens", 0) or 0),
        int(getattr(turn, "output_tokens", 0) or 0),
    )


def _turn_response_text(turn: TurnResult) -> str:
    text = turn.assistant_text or "\n".join(result.summary for result in turn.tool_results)
    if turn.status != "completed" and turn.error:
        warning = f"Réponse interrompue : {turn.error}"
        return f"{text}\n\n{warning}".strip() if text else warning
    return text


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
        self._active_turns: dict[tuple[str, str], threading.Event] = {}
        self._active_turns_lock = threading.Lock()

    def cancel(self, agent_id: str | None, session_id: str | None) -> bool:
        key = (agent_id or self.default_agent_id, session_id or "")
        with self._active_turns_lock:
            event = self._active_turns.get(key)
        if event is None:
            return False
        event.set()
        return True

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
                    "cancel_turn": self.cancel,
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
                cancel_event = self._begin_turn(agent_id, session_id)
                try:
                    initial_turn = self.run_turn(
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
                        cancel_event=cancel_event,
                    )
                    turn, any_tool_activity = run_autonomous_command(
                        run_turn=self.run_turn,
                        initial_turn=initial_turn,
                        session_id=session_id,
                        agent_id=agent_id,
                        correlation_id=correlation_id,
                        source_channel=message.channel,
                        source_peer_id=message.peer_id,
                        source_metadata={**message.metadata},
                        agent_limits=agent_limits,
                        command_name=command_name,
                        autonomy_config=autonomy,
                        original_text=message.text,
                        cancel_event=cancel_event,
                    )
                finally:
                    self._finish_turn(agent_id, session_id, cancel_event)
                text = _turn_response_text(turn)
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
                        "kernel_correlation_id": getattr(turn, "correlation_id", "") or correlation_id,
                        "tool_activity": getattr(turn, "tool_activity", []),
                        "context": _turn_context_usage(turn),
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
                    **command_result.metadata,
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

        message_metadata: dict[str, Any] = {}
        if message.metadata.get("attachments"):
            message_metadata["attachments"] = message.metadata["attachments"]
        if message.metadata.get("visible_user_message") is not None:
            message_metadata["visible_user_message"] = message.metadata.get("visible_user_message")
        cancel_event = self._begin_turn(agent_id, session_id)
        try:
            turn = self.run_turn(
                message=message.text,
                session_id=session_id,
                agent_id=agent_id,
                correlation_id=correlation_id,
                source_channel=message.channel,
                source_peer_id=message.peer_id,
                source_metadata=message.metadata,
                limits={"max_tool_iterations": 8},
                cancel_event=cancel_event,
                **({"message_metadata": message_metadata} if message_metadata else {}),
            )
        finally:
            self._finish_turn(agent_id, session_id, cancel_event)
        text = _turn_response_text(turn)
        outbound = OutboundMessage(
            channel=message.channel,
            peer_id=message.peer_id,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            text=text,
            metadata={
                "turn_status": turn.status,
                "kernel_correlation_id": getattr(turn, "correlation_id", "") or correlation_id,
                "tool_activity": getattr(turn, "tool_activity", []),
                "context": _turn_context_usage(turn),
            },
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

    def _begin_turn(self, agent_id: str, session_id: str) -> threading.Event:
        event = threading.Event()
        with self._active_turns_lock:
            self._active_turns[(agent_id, session_id)] = event
        return event

    def _finish_turn(self, agent_id: str, session_id: str, event: threading.Event) -> None:
        with self._active_turns_lock:
            if self._active_turns.get((agent_id, session_id)) is event:
                del self._active_turns[(agent_id, session_id)]

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
        if (
            normalized not in APPROVAL_ACCEPT_WORDS
            and normalized not in APPROVAL_SESSION_WORDS
            and normalized not in APPROVAL_DENY_WORDS
        ):
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
        replay_scope = "tool_session" if normalized in APPROVAL_SESSION_WORDS else "exact"
        if replay_scope == "tool_session" and approval.rememberable:
            store.remember_tool_for_session(
                agent_id=approval.agent_id,
                session_id=approval.session_id,
                tool_name=approval.tool_name,
                permission_class=approval.permission_class,
                scope=approval.scope,
                reason=approval.reason,
            )
        self._emit(
            "approval.approved.from_channel",
            message,
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"approval_id": approval.id, "replay_scope": replay_scope},
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
        web_git_status: Callable[[], dict[str, Any]] | None = None,
        web_git_diff: Callable[[str], dict[str, Any]] | None = None,
        web_model_status: Callable[[str], dict[str, Any]] | None = None,
        web_model_update: Callable[[str, str], dict[str, Any]] | None = None,
        web_agent_config: Callable[[str], dict[str, Any]] | None = None,
        web_agent_update: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        web_uploads: WebUploads | None = None,
        web_attachment: WebAttachment | None = None,
        web_turn_cancel: WebTurnCancel | None = None,
        web_token: str | None = None,
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
            web_git_status=web_git_status,
            web_git_diff=web_git_diff,
            web_model_status=web_model_status,
            web_model_update=web_model_update,
            web_agent_config=web_agent_config,
            web_agent_update=web_agent_update,
            web_uploads=web_uploads,
            web_attachment=web_attachment,
            web_turn_cancel=web_turn_cancel,
            web_token=web_token,
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
        web_git_status: Callable[[], dict[str, Any]] | None,
        web_git_diff: Callable[[str], dict[str, Any]] | None,
        web_model_status: Callable[[str], dict[str, Any]] | None,
        web_model_update: Callable[[str, str], dict[str, Any]] | None,
        web_agent_config: Callable[[str], dict[str, Any]] | None,
        web_agent_update: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
        web_uploads: WebUploads | None,
        web_attachment: WebAttachment | None,
        web_turn_cancel: WebTurnCancel | None,
        web_token: str | None,
    ):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    self._write_html(200, _web_ui_html())
                    return
                if parsed.path == "/health":
                    self._write_json(200, {"ok": True, "status": "ready"})
                    return
                if parsed.path == "/api/agents":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    agents = web_agents() if web_agents is not None else [{"id": router.default_agent_id}]
                    self._write_json(200, {"ok": True, "agents": agents})
                    return
                if parsed.path == "/api/session":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
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
                if parsed.path == "/api/attachment":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    if web_attachment is None:
                        self._write_json(404, {"ok": False, "error": "attachment_unavailable"})
                        return
                    query = parse_qs(parsed.query)
                    path = query.get("path", [""])[0]
                    if not path:
                        self._write_json(400, {"ok": False, "error": "missing_path"})
                        return
                    attachment = web_attachment(path)
                    if attachment is None:
                        self._write_json(404, {"ok": False, "error": "attachment_not_found"})
                        return
                    mime_type, body = attachment
                    self._write_bytes(200, body, mime_type)
                    return
                if parsed.path == "/api/git/status":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    payload = web_git_status() if web_git_status is not None else {"ok": True, "available": False}
                    self._write_json(200, payload)
                    return
                if parsed.path == "/api/git/diff":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    query = parse_qs(parsed.query)
                    path = query.get("path", [""])[0]
                    if not path:
                        self._write_json(400, {"ok": False, "error": "missing_path"})
                        return
                    payload = (
                        web_git_diff(path)
                        if web_git_diff is not None
                        else {"ok": False, "error": "git_diff_unavailable", "diff": ""}
                    )
                    self._write_json(200, payload)
                    return
                if parsed.path == "/api/model":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    query = parse_qs(parsed.query)
                    agent_id = query.get("agent_id", [router.default_agent_id])[0]
                    payload = (
                        web_model_status(agent_id)
                        if web_model_status is not None
                        else {"ok": False, "available": False, "error": "model_unavailable"}
                    )
                    self._write_json(200, payload)
                    return
                if parsed.path == "/api/agent":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    query = parse_qs(parsed.query)
                    agent_id = query.get("agent_id", [router.default_agent_id])[0]
                    payload = (
                        web_agent_config(agent_id)
                        if web_agent_config is not None
                        else {"ok": False, "available": False, "error": "agent_config_unavailable"}
                    )
                    self._write_json(200, payload)
                    return
                self._write_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/message/cancel":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    try:
                        payload = self._read_payload()
                        agent_id = str(payload.get("agent_id") or router.default_agent_id)
                        session_id = str(payload.get("session_id") or "")
                        local_cancelled = router.cancel(agent_id, session_id)
                        remote_cancelled = (
                            web_turn_cancel(agent_id, session_id)
                            if web_turn_cancel is not None
                            else False
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "cancelled": bool(local_cancelled or remote_cancelled),
                        },
                    )
                    return
                if parsed.path == "/api/message":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    try:
                        payload = self._read_payload()
                        agent_id = str(payload.get("agent_id") or router.default_agent_id)
                        session_id = payload.get("session_id")
                        session_id = str(session_id) if session_id else ""
                        message_text = str(payload.get("message") or payload.get("text") or "")
                        attachments = payload.get("attachments")
                        uploaded: list[dict[str, Any]] = []
                        if isinstance(attachments, list) and attachments:
                            if web_uploads is None:
                                self._write_json(400, {"ok": False, "error": "uploads_unavailable"})
                                return
                            # Decode and persist browser-provided base64 before the agent turn.
                            # The paid/main model only receives local paths; image bytes are sent
                            # later by the vision tool to the configured local vision backend.
                            uploaded = web_uploads(agent_id, session_id, attachments)
                            message_text = _message_with_uploads(message_text, uploaded)
                        metadata = {"persist": payload.get("persist", True) is not False}
                        if uploaded:
                            metadata["attachments"] = uploaded
                            metadata["visible_user_message"] = str(payload.get("message") or payload.get("text") or "")
                        result = router.handle(
                            {
                                "channel": "web",
                                "peer_id": str(payload.get("peer_id") or payload.get("session_id") or "browser"),
                                "text": message_text,
                                "agent_id": agent_id,
                                "session_id": session_id or None,
                                "metadata": metadata,
                            }
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(200, {"ok": True, "result": result.model_dump(mode="json")})
                    return
                if parsed.path == "/api/model":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    try:
                        payload = self._read_payload()
                        agent_id = str(payload.get("agent_id") or router.default_agent_id)
                        model = str(payload.get("model") or "").strip()
                        if not model:
                            self._write_json(400, {"ok": False, "error": "missing_model"})
                            return
                        result = (
                            web_model_update(agent_id, model)
                            if web_model_update is not None
                            else {"ok": False, "error": "model_update_unavailable"}
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(200, result)
                    return
                if parsed.path == "/api/agent":
                    if not self._authorized(parsed):
                        self._write_json(403, {"ok": False, "error": "forbidden"})
                        return
                    try:
                        payload = self._read_payload()
                        agent_id = str(payload.get("agent_id") or router.default_agent_id)
                        result = (
                            web_agent_update(agent_id, payload)
                            if web_agent_update is not None
                            else {"ok": False, "error": "agent_update_unavailable"}
                        )
                    except Exception as exc:
                        self._write_json(400, {"ok": False, "error": str(exc)})
                        return
                    self._write_json(200, result)
                    return
                if parsed.path != "/message":
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
                if not self._authorized(parsed):
                    self._write_json(403, {"ok": False, "error": "forbidden"})
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
                parsed = urlparse(self.path)
                if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
                    return None
                channel = parsed.path[len(prefix) : -len(suffix)]
                return channel or None

            def _authorized(self, parsed: Any) -> bool:
                if not web_token:
                    return True
                supplied = self.headers.get("X-Maurice-Token", "")
                if supplied == web_token:
                    return True
                query = parse_qs(parsed.query)
                return query.get("token", [""])[0] == web_token

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

            def _write_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        return Handler


@functools.cache
def _web_ui_html() -> str:
    return (Path(__file__).parent / "web_ui.html").read_text(encoding="utf-8")


def _message_with_uploads(message: str, uploads: list[dict[str, Any]]) -> str:
    text = message.strip() or "Analyse l'image jointe."
    if not uploads:
        return text
    lines = [
        text,
        "",
        "Images jointes par l'utilisateur :",
    ]
    for upload in uploads:
        name = upload.get("name") or Path(str(upload.get("path") or "")).name
        lines.append(f"- {name}: `{upload.get('path')}`")
    lines.extend(
        [
            "",
            "Utilise `vision.inspect` ou `vision.analyze` sur ces chemins si la demande porte sur l'image. "
            "Pour une description visuelle, appelle `vision.analyze` avec le prompt utilisateur.",
        ]
    )
    return "\n".join(lines)

"""Host channel adapters."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import Field

from maurice.host.gateway import GatewayResult, InboundMessage, OutboundMessage
from maurice.kernel.contracts import MauriceModel
from maurice.kernel.events import EventStore


class ChannelDeliveryResult(MauriceModel):
    channel: str
    peer_id: str
    status: str
    external_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalHttpChannelAdapter:
    """Normalize local HTTP channel payloads and deliver responses inline."""

    adapter = "local_http"

    def __init__(self, *, channel: str = "local_http", agent_id: str = "main") -> None:
        self.channel = channel
        self.agent_id = agent_id

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        peer_id = payload.get("peer_id") or payload.get("peer")
        text = payload.get("text") or payload.get("message")
        if not isinstance(peer_id, str) or not peer_id.strip():
            raise ValueError("local_http payload requires peer_id or peer.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("local_http payload requires text or message.")
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("local_http metadata must be an object.")
        return InboundMessage(
            channel=self.channel,
            peer_id=peer_id,
            text=text,
            agent_id=payload.get("agent_id") or self.agent_id,
            session_id=payload.get("session_id"),
            correlation_id=payload.get("correlation_id"),
            metadata=metadata,
        )

    def deliver(
        self,
        outbound: OutboundMessage,
        *,
        event_store: EventStore | None = None,
    ) -> ChannelDeliveryResult:
        delivery = ChannelDeliveryResult(
            channel=outbound.channel,
            peer_id=outbound.peer_id,
            status="delivered",
            external_id=f"local_{uuid4().hex}",
            metadata={"mode": "inline_response"},
        )
        if event_store is not None:
            event_store.emit(
                name="channel.delivery.succeeded",
                kind="progress",
                origin="host.channel.local_http",
                agent_id=outbound.agent_id,
                session_id=outbound.session_id,
                correlation_id=outbound.correlation_id,
                payload=delivery.model_dump(mode="json"),
            )
        return delivery


class ChannelAdapterRegistry:
    def __init__(self, adapters: dict[str, LocalHttpChannelAdapter]) -> None:
        self.adapters = adapters

    @classmethod
    def from_config(
        cls,
        channels: dict[str, Any],
        *,
        default_agent_id: str = "main",
    ) -> "ChannelAdapterRegistry":
        adapters: dict[str, LocalHttpChannelAdapter] = {}
        for channel, raw_config in channels.items():
            config = raw_config or {}
            if not isinstance(config, dict):
                continue
            if config.get("enabled", True) is False:
                continue
            adapter_name = config.get("adapter")
            if adapter_name == "local_http":
                adapters[channel] = LocalHttpChannelAdapter(
                    channel=channel,
                    agent_id=config.get("agent") or default_agent_id,
                )
        adapters.setdefault("local_http", LocalHttpChannelAdapter(agent_id=default_agent_id))
        return cls(adapters)

    def normalize(self, channel: str, payload: dict[str, Any]) -> InboundMessage:
        return self._adapter(channel).normalize(payload)

    def deliver(
        self,
        result: GatewayResult,
        *,
        event_store: EventStore | None = None,
    ) -> ChannelDeliveryResult:
        return self._adapter(result.outbound.channel).deliver(
            result.outbound,
            event_store=event_store,
        )

    def _adapter(self, channel: str) -> LocalHttpChannelAdapter:
        try:
            return self.adapters[channel]
        except KeyError as exc:
            raise ValueError(f"unknown channel adapter: {channel}") from exc

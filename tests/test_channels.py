from __future__ import annotations

from maurice.host.channels import ChannelAdapterRegistry, LocalHttpChannelAdapter
from maurice.host.gateway import OutboundMessage
from maurice.kernel.events import EventStore


def test_local_http_adapter_normalizes_channel_payload() -> None:
    adapter = LocalHttpChannelAdapter(channel="local_http", agent_id="main")

    inbound = adapter.normalize({"peer": "browser", "message": "Bonjour"})

    assert inbound.channel == "local_http"
    assert inbound.peer_id == "browser"
    assert inbound.text == "Bonjour"
    assert inbound.agent_id == "main"


def test_local_http_adapter_records_inline_delivery(tmp_path) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    adapter = LocalHttpChannelAdapter(channel="local_http", agent_id="main")

    delivery = adapter.deliver(
        OutboundMessage(
            channel="local_http",
            peer_id="browser",
            agent_id="main",
            session_id="local_http:browser",
            correlation_id="corr_1",
            text="Salut",
        ),
        event_store=event_store,
    )

    assert delivery.status == "delivered"
    assert delivery.external_id is not None
    assert [event.name for event in event_store.read_all()] == ["channel.delivery.succeeded"]


def test_channel_registry_uses_configured_local_http_adapter() -> None:
    registry = ChannelAdapterRegistry.from_config(
        {
            "local": {
                "adapter": "local_http",
                "enabled": True,
                "agent": "main",
            }
        }
    )

    inbound = registry.normalize("local", {"peer_id": "peer_1", "text": "Hello"})

    assert inbound.channel == "local"
    assert inbound.peer_id == "peer_1"

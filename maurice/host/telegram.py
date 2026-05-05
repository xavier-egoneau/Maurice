"""Telegram API utilities and channel helpers."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from time import monotonic
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice.host.autonomy_progress import AutonomyProgress, ProgressCallback

from maurice.kernel.config import ConfigBundle, load_workspace_config
from maurice.host.credentials import load_workspace_credentials


def _credential_value(workspace: Path, name: str) -> str:
    creds = load_workspace_credentials(workspace)
    record = creds.credentials.get(name)
    if record is None:
        return ""
    return record.value or ""


def _telegram_channel_configured(bundle: ConfigBundle) -> bool:
    return any(config.get("enabled", True) is not False for _, config in _telegram_channel_configs(bundle))


def _telegram_channel_configs(bundle: ConfigBundle) -> list[tuple[str, dict[str, Any]]]:
    channels = []
    for name, config in bundle.host.channels.items():
        if not isinstance(config, dict):
            continue
        if config.get("adapter", "telegram") != "telegram":
            continue
        if name == "telegram" or name.startswith("telegram_") or config.get("adapter") == "telegram":
            channels.append((name, config))
    return channels


def _telegram_channel_for_agent(bundle: ConfigBundle, agent_id: str | None) -> tuple[str, dict[str, Any]] | None:
    configs = _telegram_channel_configs(bundle)
    if agent_id:
        for name, config in configs:
            if str(config.get("agent") or "main") == agent_id:
                return name, config
    for name, config in configs:
        if name == "telegram":
            return name, config
    return configs[0] if configs else None


def _telegram_offset_path(workspace: Path, agent_id: str, channel_name: str) -> Path:
    filename = "telegram.offset" if channel_name == "telegram" else f"{channel_name}.offset"
    return workspace / "agents" / agent_id / filename


def _validate_telegram_first_message(token: str, allowed_users: list[int]) -> None:
    print("")
    print("Validation Telegram")
    print("1. Ouvre ton bot dans Telegram.")
    print("2. Envoie-lui un message, par exemple: salut Maurice")
    print("3. Reviens ici et appuie sur Entree.")
    input("Entree quand le message est envoye: ")
    try:
        updates = _telegram_get_updates(token)
    except (OSError, ValueError) as exc:
        print(f"Validation Telegram impossible pour le moment: {exc}")
        return
    seen_ids = _telegram_sender_ids(updates)
    matching = sorted(set(seen_ids).intersection(allowed_users))
    if matching:
        print(f"Validation Telegram OK: message recu depuis id {matching[0]}.")
        return
    if seen_ids:
        print(f"Message recu, mais depuis un id non autorise: {', '.join(str(i) for i in sorted(set(seen_ids)))}")
        print("Ajoute cet id dans la liste autorisee puis relance l'onboarding.")
        return
    print("Aucun message recent trouve. Tu pourras relancer l'onboarding apres avoir envoye un message au bot.")


def _telegram_get_updates(
    token: str,
    *,
    offset: int | None = None,
    timeout_seconds: int = 0,
) -> list[dict[str, Any]]:
    query = []
    if offset is not None:
        query.append(f"offset={offset}")
    if timeout_seconds:
        query.append(f"timeout={timeout_seconds}")
    suffix = f"?{'&'.join(query)}" if query else ""
    url = f"https://api.telegram.org/bot{token}/getUpdates{suffix}"
    try:
        with urlrequest.urlopen(url, timeout=max(timeout_seconds + 5, 10)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise ValueError(detail) from exc
    if not payload.get("ok"):
        raise ValueError(payload.get("description") or "Telegram getUpdates failed.")
    result = payload.get("result") or []
    return result if isinstance(result, list) else []


def _telegram_bot_username(token: str) -> str:
    try:
        payload = _telegram_api_json(token, "getMe", {})
    except (OSError, ValueError):
        return ""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = result.get("username")
    return username if isinstance(username, str) else ""


def _telegram_send_message(token: str, chat_id: int, text: str) -> None:
    _telegram_api_json(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text or "(empty response)"},
    )


def _telegram_set_my_commands(token: str, commands: list[dict[str, str]]) -> None:
    if not commands:
        return
    _telegram_api_json(token, "setMyCommands", {"commands": commands})


def _telegram_send_chat_action(token: str, chat_id: int, action: str = "typing") -> None:
    _telegram_api_json(
        token,
        "sendChatAction",
        {"chat_id": chat_id, "action": action},
    )


def _telegram_api_json(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise ValueError(detail) from exc
    if not result.get("ok"):
        raise ValueError(result.get("description") or f"Telegram {method} failed.")
    return result


def _telegram_update_to_inbound(
    update: dict[str, Any],
    *,
    agent_id: str,
    allowed_users: list[int],
    allowed_chats: list[int],
) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return None
    if not _telegram_sender_allowed(user_id, allowed_users):
        return None
    if not _telegram_chat_allowed(chat, user_id=user_id, chat_id=chat_id, allowed_chats=allowed_chats):
        return None
    return {
        "channel": "telegram",
        "peer_id": str(user_id),
        "text": text,
        "agent_id": agent_id,
        "metadata": {
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message.get("message_id"),
        },
    }


def _telegram_sender_allowed(user_id: int, allowed_users: list[int]) -> bool:
    return not allowed_users or user_id in allowed_users


def _telegram_chat_allowed(
    chat: dict[str, Any],
    *,
    user_id: int,
    chat_id: int,
    allowed_chats: list[int],
) -> bool:
    chat_type = str(chat.get("type") or "")
    if chat_type in {"group", "supergroup", "channel"} or chat_id < 0:
        return chat_id in allowed_chats
    if allowed_chats:
        return chat_id in allowed_chats or chat_id == user_id
    return chat_id == user_id


def _telegram_allowed_chats_with_private_users(
    allowed_users: list[int],
    allowed_chats: list[int],
) -> list[int]:
    return list(dict.fromkeys([*allowed_chats, *allowed_users]))


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int)]


def _read_int_file(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_int_file(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def _redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def _telegram_sender_ids(updates: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for update in updates:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if isinstance(sender_id, int):
            ids.append(sender_id)
    return ids


def _telegram_start_chat_action(token: str, chat_id: int):
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.is_set():
            try:
                _telegram_send_chat_action(token, chat_id, "typing")
            except Exception:
                return
            stop_event.wait(4)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=0.2)

    return stop


def make_telegram_progress_callback(
    token: str,
    chat_id: int,
    *,
    min_interval_seconds: float = 60.0,
    heartbeat_seconds: float = 120.0,
) -> ProgressCallback:
    """Return a ProgressCallback that sends Telegram messages during long autonomous runs.

    Sending strategy:
    - Always on is_done or is_blocked (final states)
    - When write_count > 0 AND enough time has passed since last send
    - Heartbeat if no message sent in heartbeat_seconds (shows the run is alive)
    """
    last_sent: list[float] = [0.0]

    def _fmt_elapsed(seconds: float) -> str:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def callback(progress: AutonomyProgress) -> None:
        now = monotonic()
        elapsed_since = now - last_sent[0]
        force = progress.is_done or progress.is_blocked
        significant = progress.write_count > 0 and elapsed_since >= min_interval_seconds
        heartbeat = elapsed_since >= heartbeat_seconds and last_sent[0] > 0
        if not (force or significant or heartbeat):
            return
        elapsed = _fmt_elapsed(progress.elapsed_seconds)
        if progress.status == "cancelled":
            text = f"⏹ {progress.command} interrompu · tour {progress.turn}/{progress.max_turns} · {elapsed}"
        elif progress.status != "completed":
            text = f"⚠ {progress.command} interrompu · tour {progress.turn}/{progress.max_turns} · {elapsed}"
        elif progress.is_done:
            files = f" · {progress.write_count} fichier(s)" if progress.write_count else ""
            text = f"✓ {progress.command} terminé · {progress.turn} tour(s){files} · {elapsed}"
        elif progress.is_blocked:
            preview = progress.assistant_text_preview[:80]
            text = f"⚠ {progress.command} — blocage : \"{preview}\""
        else:
            writes = f" · {progress.write_count} fichier(s) modifié(s)" if progress.write_count else ""
            errors = f" · {progress.error_count} erreur(s)" if progress.error_count else ""
            preview = f"\n\"{progress.assistant_text_preview[:80]}\"" if progress.assistant_text_preview else ""
            text = (
                f"⟳ {progress.command} — tour {progress.turn}/{progress.max_turns} · {elapsed}"
                f"\n{progress.tool_ok_count}/{progress.tool_count} outil(s){writes}{errors}{preview}"
            )
        try:
            _telegram_send_message(token, chat_id, text)
            last_sent[0] = now
        except Exception:
            pass

    return callback
    return stop

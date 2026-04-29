"""Auto-split from cli.py."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from maurice import __version__
from maurice.host.agent_wizard import clear_agent_creation_wizard, handle_agent_creation_wizard
from maurice.host.agents import archive_agent, create_agent, delete_agent, disable_agent, list_agents, update_agent
from maurice.host.auth import (
    CHATGPT_CREDENTIAL_NAME, ChatGPTAuthFlow, clear_chatgpt_auth,
    get_valid_chatgpt_access_token, load_chatgpt_auth, save_chatgpt_auth,
)
from maurice.host.channels import ChannelAdapterRegistry
from maurice.host.command_registry import CommandRegistry, default_command_registry
from maurice.host.credentials import (
    CredentialRecord, CredentialsStore, credentials_path,
    ensure_workspace_credentials_migrated, load_workspace_credentials, write_workspace_credentials,
)
from maurice.host.dashboard import build_dashboard_snapshot
from maurice.host.delivery import (
    _schedule_reminder_callback, _deliver_reminder_result, _build_daily_digest,
    _deliver_daily_digest, _emit_daily_event, _cancel_job_callback,
    _latest_dream_report, _human_datetime,
)
from maurice.host.gateway import GatewayHttpServer, MessageRouter
from maurice.host.migration import inspect_jarvis_workspace, migrate_jarvis_workspace
from maurice.host.model_catalog import chatgpt_model_choices, format_bytes, ollama_model_choices
from maurice.host.monitoring import build_monitoring_snapshot, read_event_tail
from maurice.host.output import (
    _yes_no, _status_marker, _short, _ansi_padding, _compact_text,
    _supports_color, _color, _print_title, _print_dim,
)
from maurice.host.paths import (
    agents_config_path, ensure_workspace_config_migrated, host_config_path,
    kernel_config_path, maurice_home, workspace_skills_config_path,
)
from maurice.host.runtime import (
    run_one_turn, _resolve_agent, _agent_system_prompt, _active_dev_project_path,
    _provider_for_config, _effective_model_config, _model_credential,
    _effective_model_label, _default_agent,
)
from maurice.host.secret_capture import capture_pending_secret
from maurice.host.self_update import (
    apply_runtime_proposal, list_runtime_proposals, run_proposal_tests, validate_runtime_proposal,
)
from maurice.host.service import check_install, inspect_service_status, read_service_logs
from maurice.host.telegram import (
    _credential_value, _telegram_channel_configured, _telegram_channel_configs,
    _telegram_channel_for_agent, _telegram_offset_path, _validate_telegram_first_message,
    _telegram_get_updates, _telegram_bot_username, _telegram_send_message,
    _telegram_send_chat_action, _telegram_api_json, _telegram_update_to_inbound,
    _int_list, _read_int_file, _write_int_file, _redact_secret,
    _telegram_sender_ids, _telegram_start_chat_action,
)
from maurice.host.workspace import ensure_workspace_content_migrated, initialize_workspace
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.compaction import CompactionConfig
from maurice.kernel.config import ConfigBundle, load_workspace_config, read_yaml_file, write_yaml_file
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop, TurnResult
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.providers import (
    ApiProvider, ChatGPTCodexProvider, MockProvider,
    OllamaCompatibleProvider, OpenAICompatibleProvider, UnsupportedProvider,
)
from maurice.kernel.runs import RunApprovalStore, RunCoordinationStore, RunExecutor, RunStore
from maurice.kernel.scheduler import JobRunner, JobStatus, JobStore, SchedulerService, utc_now
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillLoader
from maurice.system_skills.dev.planner import PLAN_WIZARD_FILE, clear_plan_wizard, handle_plan_wizard
from maurice.system_skills.reminders.tools import fire_reminder

_ASCII_MAURICE = """\
[#E8761A]███╗   ███╗ █████╗ ██╗   ██╗██████╗ ██╗ ██████╗███████╗[/]
[#E8761A]████╗ ████║██╔══██╗██║   ██║██╔══██╗██║██╔════╝██╔════╝[/]
[#DA6010]██╔████╔██║███████║██║   ██║██████╔╝██║██║     █████╗  [/]
[#C04E08]██║╚██╔╝██║██╔══██║██║   ██║██╔══██╗██║██║     ██╔══╝  [/]
[#A03A00]██║ ╚═╝ ██║██║  ██║╚██████╔╝██║  ██║██║╚██████╗███████╗[/]
[#7A2A00]╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝╚══════╝[/]"""


def _dashboard(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    watch: bool = False,
    refresh_seconds: float = 1.0,
    plain: bool = False,
) -> None:
    if not plain and not watch and sys.stdout.isatty():
        try:
            _dashboard_textual(
                workspace_root,
                agent_id=agent_id,
                event_limit=event_limit,
                refresh_seconds=refresh_seconds,
            )
            return
        except ImportError:
            pass
    if not plain and _dashboard_rich(
        workspace_root,
        agent_id=agent_id,
        event_limit=event_limit,
        watch=watch,
        refresh_seconds=refresh_seconds,
    ):
        return
    frame = 0
    try:
        while True:
            if watch:
                print("\033[2J\033[H", end="")
            _render_dashboard(workspace_root, agent_id=agent_id, event_limit=event_limit, frame=frame)
            if not watch:
                return
            frame += 1
            time.sleep(max(refresh_seconds, 0.2))
    except KeyboardInterrupt:
        print("")



def _dashboard_textual(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    refresh_seconds: float,
) -> None:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import DataTable, Static, TabbedContent, TabPane
    from rich.text import Text

    class MauriceHeader(Static):
        def render(self) -> str:
            return _ASCII_MAURICE

    class MauriceStatus(Static):
        def update_snapshot(self, bundle: ConfigBundle, snapshot) -> None:
            gateway = f"{snapshot.runtime.gateway.get('host')}:{snapshot.runtime.gateway.get('port')}"
            telegram = "ok" if _telegram_channel_configured(bundle) else "-"
            provider = _effective_model_label(bundle, _default_agent(bundle))
            scheduler = "ok" if snapshot.runtime.scheduler_enabled else "-"
            self.update(
                f"Service [#E8761A]{gateway}[/]   Automatismes [#E8761A]{scheduler}[/]   "
                f"Telegram [#E8761A]{telegram}[/]   Modele [#E8761A]{provider}[/]"
            )

    class HelpBar(Static):
        def update_for_tab(self, tab_id: str) -> None:
            commands = {
                "tab-cluster": "q Quitter   r Rafraichir",
                "tab-jobs": "q Quitter   r Rafraichir   d Desactiver l'automatisme selectionne",
                "tab-runs": "q Quitter   r Rafraichir",
                "tab-models": "q Quitter   r Rafraichir   m Changer le modele",
                "tab-security": "q Quitter   r Rafraichir   p Changer la permission selectionnee",
                "tab-skills": "q Quitter   r Rafraichir   t Activer/desactiver la capacite user selectionnee",
                "tab-logs": "q Quitter   r Rafraichir",
            }
            self.update(commands.get(tab_id, "q Quitter   r Rafraichir"))

    class MauriceDashboard(App):
        TITLE = "Maurice"
        ENABLE_COMMAND_PALETTE = False
        CSS = """
        Screen { background: #120A04; color: #C8A882; }
        MauriceHeader { height: 8; padding: 0 2; background: #120A04; }
        MauriceStatus { height: 1; padding: 0 2; background: #1E0E06; color: #7A5C3A; }
        TabbedContent { height: 1fr; }
        TabPane { padding: 0; overflow-y: hidden; overflow-x: hidden; }
        Tabs { background: #1A0D07; }
        Tab { color: #7A5C3A; background: #1A0D07; }
        Tab:hover { color: #C8803A; background: #1A0D07; }
        Tab.-active { color: #E8761A; background: #1A0D07; }
        Underline > .underline--bar { color: #E8761A; }
        DataTable { height: 1fr; background: #120A04; color: #C8A882; }
        DataTable > .datatable--header { color: #DA7756; text-style: bold; }
        HelpBar { height: 1; padding: 0 1; background: #24303A; color: #F3D9AE; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quitter"),
            Binding("r", "refresh_data", "Rafraichir"),
            Binding("p", "change_permission", "Permission"),
            Binding("d", "disable_automation", "Auto off"),
            Binding("t", "toggle_user_skill", "Capacite"),
            Binding("m", "change_model", "Modele"),
        ]

        def compose(self) -> ComposeResult:
            self._frame = 0
            self._active_tab = "tab-cluster"
            self._automation_rows = []
            self._model_rows = []
            self._permission_rows = []
            self._skill_rows = []
            yield MauriceHeader()
            yield MauriceStatus(id="status-bar")
            with TabbedContent():
                with TabPane("Cluster", id="tab-cluster"):
                    yield DataTable(id="cluster-table")
                with TabPane("Automatismes", id="tab-jobs"):
                    yield DataTable(id="jobs-table")
                with TabPane("Sessions", id="tab-runs"):
                    yield DataTable(id="runs-table")
                with TabPane("Modeles", id="tab-models"):
                    yield DataTable(id="models-table")
                with TabPane("Permissions", id="tab-security"):
                    yield DataTable(id="security-table")
                with TabPane("Capacites", id="tab-skills"):
                    yield DataTable(id="skills-table")
                with TabPane("Journal", id="tab-logs"):
                    yield DataTable(id="logs-table")
            yield HelpBar(id="help-bar")

        def on_mount(self) -> None:
            self._init_tables()
            self.action_refresh_data()
            self.query_one("#help-bar", HelpBar).update_for_tab("tab-cluster")
            self.set_interval(max(refresh_seconds, 0.2), self.action_refresh_data)

        def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
            self._active_tab = event.pane.id or "tab-cluster"
            self.query_one("#help-bar", HelpBar).update_for_tab(self._active_tab)

        def _init_tables(self) -> None:
            self.query_one("#cluster-table", DataTable).add_columns("Agent", "Modele", "Statut", "Permissions", "Acces")
            self.query_one("#jobs-table", DataTable).add_columns("Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme")
            self.query_one("#runs-table", DataTable).add_columns("Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour")
            self.query_one("#models-table", DataTable).add_columns("Agent", "Type", "Modele", "Connexion")
            self.query_one("#security-table", DataTable).add_columns("Agent", "Mode", "Maximum global", "Changement")
            self.query_one("#skills-table", DataTable).add_columns("Agent", "Capacite", "Source", "Active", "Etat", "Problemes")
            self.query_one("#logs-table", DataTable).add_columns("Heure", "Niveau", "Source", "Agent", "Session", "Message")

        def action_refresh_data(self) -> None:
            bundle = load_workspace_config(workspace_root)
            selected_agent = _resolve_agent(bundle, agent_id)
            monitor = build_monitoring_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
            dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
            self.query_one("#status-bar", MauriceStatus).update_snapshot(bundle, monitor)
            self._replace_rows("#cluster-table", [[row.label, row.model, _status_marker(row.status, self._frame), row.permission, row.access] for row in dashboard.agents])
            self._replace_rows("#jobs-table", [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations])
            self._replace_rows("#runs-table", [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions])
            self._replace_rows("#models-table", [[row.agent_id, row.provider, row.model, row.auth_state] for row in dashboard.models])
            self._replace_rows("#security-table", [[row.agent_id, row.current_profile, row.global_maximum, row.escalation] for row in dashboard.permissions])
            self._replace_rows("#skills-table", [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills])
            self._replace_rows("#logs-table", [[row.time, _log_level_text(Text, row.level), row.source, row.agent_id, row.session_id, row.message] for row in dashboard.logs])
            self._automation_rows = dashboard.automations
            self._model_rows = dashboard.models
            self._permission_rows = dashboard.permissions
            self._skill_rows = dashboard.skills
            self._frame += 1

        def _replace_rows(self, selector: str, rows: list[list[str]]) -> None:
            table = self.query_one(selector, DataTable)
            table.clear(columns=False)
            for row in rows or [["-" for _ in table.columns]]:
                table.add_row(*row)

        def action_change_permission(self) -> None:
            if self._active_tab != "tab-security":
                self.notify("Va dans l'onglet Permissions pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#security-table", self._permission_rows)
            if row is None:
                self.notify("Ouvre l'onglet Permissions et selectionne un agent.", severity="warning")
                return
            profiles = ["safe", "limited", "power"]
            next_profile = profiles[(profiles.index(row.current_profile) + 1) % len(profiles)]
            try:
                update_agent(
                    workspace_root,
                    agent_id=row.agent_id,
                    permission_profile=next_profile,
                    confirmed_permission_elevation=False,
                )
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
            self.notify(f"Permission de {row.agent_id}: {next_profile}")
            self.action_refresh_data()

        def action_disable_automation(self) -> None:
            if self._active_tab != "tab-jobs":
                self.notify("Va dans l'onglet Automatismes pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#jobs-table", self._automation_rows)
            if row is None:
                self.notify("Ouvre l'onglet Automatismes et selectionne une ligne.", severity="warning")
                return
            if not row.enabled:
                self.notify("Cet automatisme est deja inactif.", severity="warning")
                return
            bundle = load_workspace_config(workspace_root)
            workspace = Path(bundle.host.workspace_root)
            try:
                JobStore(workspace / "agents" / row.owner_agent / "jobs.json").cancel(row.job_id)
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
            self.notify(f"Automatisme desactive: {row.name}")
            self.action_refresh_data()

        def action_toggle_user_skill(self) -> None:
            if self._active_tab != "tab-skills":
                self.notify("Va dans l'onglet Capacites pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#skills-table", self._skill_rows)
            if row is None:
                self.notify("Ouvre l'onglet Capacites et selectionne une ligne.", severity="warning")
                return
            if row.source != "user":
                self.notify("Les capacites systeme sont protegees ici. Seules les capacites user sont modifiables.", severity="warning", timeout=6)
                return
            bundle = load_workspace_config(workspace_root)
            agent = bundle.agents.agents[row.agent_id]
            skills = list(agent.skills or bundle.kernel.skills)
            if row.enabled and row.name in skills:
                skills.remove(row.name)
                action = "desactivee"
            elif not row.enabled and row.name not in skills:
                skills.append(row.name)
                action = "activee"
            else:
                self.notify("Aucun changement necessaire.", severity="warning")
                return
            update_agent(workspace_root, agent_id=row.agent_id, skills=skills)
            self.notify(f"Capacite {row.name} {action} pour {row.agent_id}")
            self.action_refresh_data()

        def action_change_model(self) -> None:
            if self._active_tab != "tab-models":
                self.notify("Va dans l'onglet Modeles pour utiliser cette action.", severity="warning")
                return
            row = self._selected_model_row("#models-table", self._model_rows)
            agent = row.agent_id if row is not None else "main"
            try:
                with self.suspend():
                    _onboard_agent_model(workspace_root, agent_id=agent)
            except SystemExit as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            except Exception as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify(f"Modele mis a jour pour {agent}")
            self.action_refresh_data()

        def _selected_model_row(self, selector: str, rows):
            table = self.query_one(selector, DataTable)
            if not rows:
                return None
            index = getattr(table, "cursor_row", 0) or 0
            if index < 0 or index >= len(rows):
                return None
            return rows[index]

    MauriceDashboard().run()



def _dashboard_rich(
    workspace_root: Path,
    *,
    agent_id: str | None,
    event_limit: int,
    watch: bool,
    refresh_seconds: float,
) -> bool:
    try:
        from rich.console import Console, Group
        from rich.live import Live
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        return False

    console = Console()

    def build(frame: int):
        bundle = load_workspace_config(workspace_root)
        selected_agent = _resolve_agent(bundle, agent_id)
        snapshot = build_monitoring_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
        dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
        spinner = "|/-\\"[frame % 4]
        provider = _effective_model_label(bundle, selected_agent)
        gateway = f"{snapshot.runtime.gateway.get('host')}:{snapshot.runtime.gateway.get('port')}"
        telegram = "ok" if _telegram_channel_configured(bundle) else "-"
        title = Text("MAURICE", style="bold #E8761A")
        header = Text.from_markup(_ASCII_MAURICE)
        status = Text.from_markup(
            f"Service [bold #E8761A]{gateway}[/]   Automatismes [bold #E8761A]{'ok' if snapshot.runtime.scheduler_enabled else '-'}[/]   "
            f"Telegram [bold #E8761A]{telegram}[/]   Modele [bold #E8761A]{provider}[/]   {spinner}"
        )
        tabs = Text("Cluster   Automatismes   Sessions   Modeles   Permissions   Capacites   Journal", style="#E8761A")
        return Group(
            title,
            header,
            status,
            tabs,
            _rich_table(Table, "Cluster", ["Agent", "Modele", "Statut", "Permissions", "Acces"], [[row.label, row.model, row.status, row.permission, row.access] for row in dashboard.agents]),
            _rich_table(Table, "Automatismes", ["Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme"], [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations]),
            _rich_table(Table, "Sessions", ["Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour"], [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions]),
            _rich_table(Table, "Capacites", ["Agent", "Capacite", "Source", "Active", "Etat", "Problemes"], [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills]),
            _rich_table(Table, "Journal", ["Heure", "Niveau", "Source", "Agent", "Session", "Message"], [[row.time, row.level, row.source, row.agent_id, row.session_id, row.message] for row in dashboard.logs]),
        )

    try:
        if watch:
            frame = 0
            with Live(build(frame), console=console, refresh_per_second=4, screen=False) as live:
                while True:
                    time.sleep(max(refresh_seconds, 0.2))
                    frame += 1
                    live.update(build(frame))
        else:
            console.print(build(0))
    except KeyboardInterrupt:
        console.print("")
    return True



def _rich_table(table_cls, title: str, headers: list[str], rows: list[list[str]]):
    table = table_cls(
        title=title,
        border_style="#5C3317",
        header_style="bold #DA7756",
        style="#C8A882",
        title_style="bold #E8761A",
        expand=False,
    )
    for header in headers:
        table.add_column(header)
    for row in rows or [["-" for _ in headers]]:
        table.add_row(*[str(value) for value in row])
    return table



def _cluster_rows(bundle: ConfigBundle, snapshot) -> list[list[str]]:
    active_agents = _active_agents(snapshot.events)
    rows = []
    for agent in snapshot.agents:
        marker = "*" if agent.default else " "
        status = "active" if agent.id in active_agents else agent.status
        rows.append(
            [
                f"{marker} {agent.id}",
                _agent_model_name(bundle, agent.id),
                status,
                agent.permission_profile,
                ",".join(agent.channels) or "-",
            ]
        )
    return rows



def _log_level_text(text_cls, level: str):
    styles = {
        "info": "#7A9A7A",
        "warning": "yellow",
        "error": "bold red",
    }
    return text_cls(level, style=styles.get(level, "white"))



def _model_rows(bundle: ConfigBundle, snapshot) -> list[list[str]]:
    return [
        [
            agent.id,
            str(_effective_model_config(bundle, bundle.agents.agents.get(agent.id)).get("provider") or "mock"),
            _agent_model_name(bundle, agent.id),
        ]
        for agent in snapshot.agents
    ]



def _security_rows(snapshot) -> list[list[str]]:
    return [
        [
            agent.id,
            agent.permission_profile,
            ",".join(agent.credentials) or "-",
            ",".join(agent.channels) or "-",
        ]
        for agent in snapshot.agents
    ]



def _event_rows(events) -> list[list[str]]:
    return [
        [
            event.time.strftime("%H:%M:%S"),
            event.agent_id,
            event.session_id,
            event.name,
        ]
        for event in events
    ]



def _render_dashboard(workspace_root: Path, *, agent_id: str | None, event_limit: int, frame: int) -> None:
    bundle = load_workspace_config(workspace_root)
    selected_agent = _resolve_agent(bundle, agent_id)
    dashboard = build_dashboard_snapshot(workspace_root, agent_id=selected_agent.id, event_limit=event_limit)
    spinner = "|/-\\"[frame % 4]

    print(_color(f"MAURICE {spinner}", "1;38;5;208"))
    print(_color("local-first agent runtime", "38;5;130"))
    print(_dashboard_status_line([
        ("Service", dashboard.status.service),
        ("Automatismes", dashboard.status.automatismes),
        ("Telegram", dashboard.status.telegram),
        ("Modele", dashboard.status.modele),
    ]))
    print(_color("Cluster  Automatismes  Sessions  Modeles  Permissions  Capacites  Journal", "38;5;130"))
    print(_color("=" * 88, "38;5;52"))
    print("")

    print(_dashboard_table(["Agent", "Modele", "Statut", "Permissions", "Acces"], [[row.label, row.model, _status_marker(row.status), row.permission, row.access] for row in dashboard.agents]))
    print("")
    print(_dashboard_status_line([
        ("Agents", str(len(dashboard.agents))),
        ("Automatismes", str(len(dashboard.automations))),
        ("Sessions", str(len(dashboard.sessions))),
        ("Capacites", str(len(dashboard.skills))),
    ]))
    print("")
    print(_color("Automatismes", "1;38;5;208"))
    print(_dashboard_table(["Automatisme", "Agent", "Etat", "Actif", "Prochaine fois", "Rythme", "Probleme"], [[row.name, row.owner_agent, row.status, _yes_no(row.enabled), row.next_run, row.recurrence, row.last_problem] for row in dashboard.automations]))
    print("")
    print(_color("Sessions", "1;38;5;208"))
    print(_dashboard_table(["Session", "Agent", "Origine", "Etat", "Dernier signe", "Mis a jour"], [[row.session_id, row.agent_id, row.origin, row.status, row.last_event, row.updated_at] for row in dashboard.sessions]))
    print("")
    print(_color("Capacites", "1;38;5;208"))
    print(_dashboard_table(["Agent", "Capacite", "Source", "Active", "Etat", "Problemes"], [[row.agent_id, row.name, row.source, _yes_no(row.enabled), row.state, row.issues] for row in dashboard.skills]))
    print("")
    print(_color("Journal", "1;38;5;208"))
    if not dashboard.logs:
        print("- none")
        return
    for row in dashboard.logs:
        print(f"  {row.time}  {row.level:<7} {row.agent_id:<10} {row.session_id:<22} {row.message}")



def _dashboard_status_line(items: list[tuple[str, str]]) -> str:
    parts = []
    for label, value in items:
        parts.append(f"{_color(label, '38;5;130')} {_color(value, '1;38;5;208')}")
    return "   ".join(parts)



def _dashboard_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["-" for _ in headers]]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [border]
    lines.append(
        "|"
        + "|".join(f" {_color(headers[index], '1;38;5;208'):<{widths[index] + _ansi_padding(headers[index])}} " for index in range(len(headers)))
        + "|"
    )
    lines.append(border)
    for row in rows:
        lines.append("|" + "|".join(f" {row[index]:<{widths[index]}} " for index in range(len(row))) + "|")
    lines.append(border)
    return "\n".join(lines)



def _job_rows(jobs) -> list[list[str]]:
    rows = []
    for job in jobs[:8]:
        rows.append(
            [
                job.name,
                job.owner,
                str(job.status),
                job.run_at.strftime("%H:%M:%S"),
                str(job.interval_seconds or "-"),
                _short(job.last_error or "-", 28),
            ]
        )
    return rows



def _run_rows(runs) -> list[list[str]]:
    rows = []
    for run in runs[:8]:
        rows.append(
            [
                run.id[-8:],
                str(run.state),
                _short(run.task, 40),
                run.updated_at.strftime("%H:%M:%S"),
            ]
        )
    return rows



def _skill_rows(skills) -> list[list[str]]:
    rows = []
    for skill in skills[:12]:
        issue = "; ".join(skill.errors or skill.suggested_fixes) or "-"
        rows.append([skill.name, skill.state, _short(issue, 40)])
    return rows



def _active_agents(events) -> set[str]:
    active: set[str] = set()
    for event in events[-20:]:
        if event.name in {"turn.started", "tool.started", "gateway.message.received", "job.started"}:
            active.add(event.agent_id)
        if event.name in {"turn.completed", "turn.failed", "tool.completed", "tool.failed", "job.completed", "job.failed"}:
            active.discard(event.agent_id)
    return active



def _compact_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "{}"
    return "{" + ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) + "}"



def _agent_model_name(bundle: ConfigBundle, agent_id: str) -> str:
    agent = bundle.agents.agents.get(agent_id)
    model = _effective_model_config(bundle, agent)
    provider = model.get("provider") or "mock"
    name = model.get("name") or provider
    return f"{provider}/{name}"


"""Smallest useful one-turn agent loop."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Any
from urllib.parse import urlparse

from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.classifier import Classifier, ClassifierDecision, ClassifierOutcome
from maurice.kernel.compaction import CompactionConfig, CompactionLevel, compact_messages
from maurice.kernel.shell_parser import parse as parse_shell
from maurice.kernel.contracts import (
    PermissionClass,
    ProviderChunkType,
    ProviderChunk,
    ProviderStatus,
    ToolCall,
    ToolDeclaration,
    ToolError,
    ToolResult,
    TrustLabel,
)
from maurice.kernel.events import EventStore
from maurice.kernel.permissions import (
    PermissionContext,
    PermissionDecision,
    PermissionProfileName,
    evaluate_permission,
)
from maurice.kernel.providers import Provider
from maurice.kernel.session import SessionMessage, SessionRecord, SessionStore
from maurice.kernel.skills import SkillRegistry
from maurice.kernel.tool_labels import tool_action_label, tool_short_label, tool_target

ToolExecutor = Callable[[dict[str, Any]], ToolResult | dict[str, Any]]
ApprovalCallback = Callable[[str, dict[str, Any], str, str], bool | str | dict[str, Any]]
TextDeltaCallback = Callable[[str], None]
ToolStartedCallback = Callable[[str, dict[str, Any]], None]  # tool_name, arguments


@dataclass
class TurnResult:
    session: SessionRecord
    correlation_id: str
    assistant_text: str
    tool_results: list[ToolResult]
    status: str
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_activity: list[dict[str, Any]] = field(default_factory=list)


class ToolExecutionError(RuntimeError):
    pass


def _approval_response(value: bool | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "approved": bool(value.get("approved")),
            "scope": str(value.get("scope") or "exact"),
        }
    if isinstance(value, str):
        normalized = value.strip().lower()
        return {
            "approved": normalized in {"y", "yes", "o", "oui", "ok", "session", "tool_session"},
            "scope": "session" if normalized in {"session", "tool_session"} else "exact",
        }
    return {"approved": bool(value), "scope": "exact"}


def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool results that have no matching tool call (orphaned after interrupted turns)."""
    result: list[dict[str, Any]] = []
    previous_tool_call_ids: set[str] = set()
    for msg in messages:
        meta = msg.get("metadata") or {}
        if msg.get("role") == "tool_call":
            call_id = meta.get("tool_call_id")
            if call_id:
                previous_tool_call_ids.add(call_id)
        if msg.get("role") == "tool":
            call_id = meta.get("tool_call_id")
            if call_id and call_id not in previous_tool_call_ids:
                continue
        result.append(msg)
    return result


class AgentLoop:
    def __init__(
        self,
        *,
        provider: Provider,
        registry: SkillRegistry,
        session_store: SessionStore,
        event_store: EventStore,
        permission_context: PermissionContext,
        permission_profile: PermissionProfileName,
        tool_executors: dict[str, ToolExecutor] | None = None,
        approval_store: ApprovalStore | None = None,
        model: str = "mock",
        system_prompt: str = "",
        compaction_config: CompactionConfig | None = None,
        classifier: Classifier | None = None,
        approval_callback: ApprovalCallback | None = None,
        text_delta_callback: TextDeltaCallback | None = None,
        tool_started_callback: ToolStartedCallback | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.session_store = session_store
        self.event_store = event_store
        self.permission_context = permission_context
        self.permission_profile = permission_profile
        self.tool_executors = tool_executors or {}
        self.approval_store = approval_store
        self.model = model
        self.system_prompt = system_prompt
        self.compaction_config = compaction_config
        self.classifier = classifier
        self.approval_callback = approval_callback
        self.text_delta_callback = text_delta_callback
        self.tool_started_callback = tool_started_callback

    def run_turn(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        limits: dict[str, Any] | None = None,
        message_metadata: dict[str, Any] | None = None,
    ) -> TurnResult:
        session = self._load_or_create_session(agent_id, session_id)
        turn = self.session_store.start_turn(agent_id, session.id)
        correlation_id = turn.correlation_id
        message_metadata = message_metadata or {}
        internal_message = message_metadata.get("internal") is True
        autonomy_internal = message_metadata.get("autonomy_internal") is True
        self.event_store.emit(
            name="turn.started",
            kind="progress",
            origin="kernel",
            agent_id=agent_id,
            session_id=session.id,
            correlation_id=correlation_id,
            payload={"message": message},
        )
        if self.compaction_config and self.compaction_config.context_window_tokens > 0:
            self._compact_if_needed(agent_id, session.id, correlation_id)

        self.session_store.append_message(
            agent_id,
            session.id,
            role="user",
            content=message,
            correlation_id=correlation_id,
            metadata=message_metadata,
        )

        assistant_text = ""
        tool_results: list[ToolResult] = []
        tool_activity: list[dict[str, Any]] = []
        status = "completed"
        provider_error: str | None = None
        max_tool_iterations = _max_tool_iterations(limits)
        total_input_tokens = 0
        total_output_tokens = 0
        try:
            for iteration in range(max_tool_iterations):
                iteration_text = ""
                iteration_tool_results: list[ToolResult] = []
                terminal_status_seen = False
                for chunk in self.provider.stream(
                    messages=self._provider_messages(agent_id, session.id),
                    model=self.model,
                    tools=list(self.registry.tools.values()),
                    system=self._system_prompt(),
                    limits=limits or {},
                ):
                    chunk = ProviderChunk.model_validate(chunk)
                    if chunk.type == ProviderChunkType.TEXT_DELTA:
                        iteration_text += chunk.delta
                        if self.text_delta_callback is not None:
                            self.text_delta_callback(chunk.delta)
                        continue
                    if chunk.type == ProviderChunkType.TOOL_CALL and chunk.tool_call is not None:
                        # Record the tool call in session so providers can include
                        # the matching function_call before the function_call_output.
                        self.session_store.append_message(
                            agent_id,
                            session.id,
                            role="tool_call",
                            content="",
                            correlation_id=correlation_id,
                            metadata={
                                "tool_call_id": chunk.tool_call.id,
                                "tool_name": chunk.tool_call.name,
                                "tool_arguments": chunk.tool_call.arguments,
                            },
                        )
                        _result = self._handle_tool_call(
                            chunk.tool_call,
                            agent_id=agent_id,
                            session_id=session.id,
                            correlation_id=correlation_id,
                        )
                        iteration_tool_results.append(_result)
                        tool_activity.append(
                            _tool_activity_entry(chunk.tool_call.name, chunk.tool_call.arguments, _result)
                        )
                        continue
                    if chunk.type == ProviderChunkType.USAGE and chunk.usage is not None:
                        total_input_tokens += chunk.usage.input_tokens
                        total_output_tokens += chunk.usage.output_tokens
                        continue
                    if chunk.type == ProviderChunkType.STATUS:
                        terminal_status_seen = True
                        if chunk.status == ProviderStatus.FAILED:
                            status = "failed"
                            if chunk.error is not None:
                                provider_error = f"{chunk.error.code}: {chunk.error.message}"
                        continue
                if not terminal_status_seen:
                    status = "failed"
                    provider_error = "provider_error: Provider stream ended without terminal status."
                if limits and limits.get("allow_text_tool_calls") and iteration_text:
                    text_tool_calls = self._text_tool_calls(iteration_text)
                    if text_tool_calls:
                        iteration_text = ""
                        for tool_call in text_tool_calls:
                            self.session_store.append_message(
                                agent_id,
                                session.id,
                                role="tool_call",
                                content="",
                                correlation_id=correlation_id,
                                metadata={
                                    "tool_call_id": tool_call.id,
                                    "tool_name": tool_call.name,
                                    "tool_arguments": tool_call.arguments,
                                },
                            )
                            _result = self._handle_tool_call(
                                tool_call,
                                agent_id=agent_id,
                                session_id=session.id,
                                correlation_id=correlation_id,
                            )
                            iteration_tool_results.append(_result)
                            tool_activity.append(
                                _tool_activity_entry(tool_call.name, tool_call.arguments, _result)
                            )
                if iteration_text:
                    assistant_text += iteration_text
                    self.session_store.append_message(
                        agent_id,
                        session.id,
                        role="assistant",
                        content=iteration_text,
                        correlation_id=correlation_id,
                        metadata={"autonomy_internal": True} if autonomy_internal else {},
                    )
                tool_results.extend(iteration_tool_results)
                if status == "failed" or not iteration_tool_results or iteration_text:
                    break
                if any(result.error and result.error.code == "approval_required" for result in iteration_tool_results):
                    break
        except Exception as exc:
            status = "failed"
            self.event_store.emit(
                name="turn.failed",
                kind="fact",
                origin="kernel",
                agent_id=agent_id,
                session_id=session.id,
                correlation_id=correlation_id,
                payload={"error": str(exc)},
            )
            self.session_store.complete_turn(
                agent_id, session.id, correlation_id, status="failed"
            )
            if internal_message:
                self._remove_internal_messages(agent_id, session.id, correlation_id)
            raise

        self.session_store.complete_turn(
            agent_id,
            session.id,
            correlation_id,
            status="completed" if status == "completed" else "failed",
        )
        event_name = "turn.completed" if status == "completed" else "turn.failed"
        self.event_store.emit(
            name=event_name,
            kind="fact",
            origin="kernel",
            agent_id=agent_id,
            session_id=session.id,
            correlation_id=correlation_id,
            payload={
                "assistant_text": assistant_text,
                "tool_results": [result.model_dump(mode="json") for result in tool_results],
                "error": provider_error,
            },
        )
        if internal_message:
            self._remove_internal_messages(agent_id, session.id, correlation_id)
        # Fall back to character-based estimation if provider didn't report usage
        if not total_input_tokens:
            from maurice.kernel.compaction import estimate_tokens
            msgs = self._provider_messages(agent_id, session.id)
            total_input_tokens = estimate_tokens(msgs, self._system_prompt())
        if not total_output_tokens:
            total_output_tokens = len(assistant_text) // 4

        return TurnResult(
            session=self.session_store.load(agent_id, session.id),
            correlation_id=correlation_id,
            assistant_text=assistant_text,
            tool_results=tool_results,
            status=status,
            error=provider_error,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            tool_activity=tool_activity,
        )

    def _handle_tool_call(
        self,
        tool_call: ToolCall,
        *,
        agent_id: str,
        session_id: str,
        correlation_id: str,
    ) -> ToolResult:
        declaration = self.registry.tools.get(tool_call.name)
        if declaration is None:
            result = self._failed_tool_result(
                f"Unknown tool: {tool_call.name}", code="unknown_tool"
            )
            self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
            return result

        self.event_store.emit(
            name="tool.requested",
            kind="fact",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"tool_call": tool_call.model_dump(mode="json")},
        )

        evaluation = evaluate_permission(
            self.permission_profile,
            declaration.permission.permission_class,
            self._requested_permission_scope(declaration, tool_call),
            self.permission_context,
        )
        if evaluation.decision == PermissionDecision.DENY:
            result = self._failed_tool_result(evaluation.reason, code="permission_denied")
            self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
            return result

        if PermissionClass(declaration.permission.permission_class) == PermissionClass.SHELL_EXEC:
            shell_result = self._check_shell_command(
                tool_call, agent_id, session_id, correlation_id
            )
            if shell_result is not None:
                return shell_result

        if evaluation.decision == PermissionDecision.ASK:
            outcome = self._run_classifier(
                declaration, tool_call, agent_id, session_id, correlation_id
            )
            if outcome.ran and outcome.decision.block:
                result = self._failed_tool_result(
                    f"Classifier: {outcome.decision.reason}",
                    code="classifier_blocked",
                )
                self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
                return result
            if not outcome.ran:
                # Normal ask flow: check saved approval or request human approval
                approved = None
                if self.approval_store is not None:
                    approved = self.approval_store.approved_for_replay(
                        permission_class=declaration.permission.permission_class,
                        scope=self._requested_permission_scope(declaration, tool_call),
                        tool_name=declaration.name,
                        arguments=tool_call.arguments,
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                if approved is None:
                    if self.approval_callback is not None:
                        approval_response = _approval_response(
                            self.approval_callback(
                                declaration.name,
                                tool_call.arguments,
                                declaration.permission.permission_class,
                                evaluation.reason,
                            )
                        )
                        approved = approval_response["approved"]
                        if (
                            approved
                            and self.approval_store is not None
                            and evaluation.rememberable
                            and approval_response["scope"] == "session"
                        ):
                            self.approval_store.remember_tool_for_session(
                                agent_id=agent_id,
                                session_id=session_id,
                                tool_name=declaration.name,
                                permission_class=declaration.permission.permission_class,
                                scope=self._requested_permission_scope(declaration, tool_call),
                                reason=evaluation.reason,
                            )
                        elif approved and self.approval_store is not None and evaluation.rememberable:
                            self.approval_store.remember(
                                agent_id=agent_id,
                                session_id=session_id,
                                tool_name=declaration.name,
                                permission_class=declaration.permission.permission_class,
                                scope=self._requested_permission_scope(declaration, tool_call),
                                arguments=tool_call.arguments,
                            )
                        if not approved:
                            action_label = tool_action_label(declaration.name, tool_call.arguments)
                            result = self._failed_tool_result(
                                f"{action_label} refusé.",
                                code="approval_denied",
                                retryable=False,
                            )
                            self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
                            return result
                    else:
                        action_label = tool_action_label(declaration.name, tool_call.arguments)
                        if self.approval_store is not None:
                            self.approval_store.request(
                                agent_id=agent_id,
                                session_id=session_id,
                                correlation_id=correlation_id,
                                tool_name=declaration.name,
                                permission_class=declaration.permission.permission_class,
                                scope=self._requested_permission_scope(declaration, tool_call),
                                arguments=tool_call.arguments,
                                summary=f"Autoriser : {action_label}",
                                reason=evaluation.reason,
                                rememberable=evaluation.rememberable,
                            )
                        result = self._failed_tool_result(
                            f"Autorisation requise pour {action_label}. Reponds `ok` pour approuver, `session` pour approuver cette action dans cette session, ou `non` pour refuser.",
                            code="approval_required",
                            retryable=True,
                        )
                        self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
                        return result

        self.event_store.emit(
            name="tool.started",
            kind="progress",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"tool_name": declaration.name},
        )
        if self.tool_started_callback is not None:
            self.tool_started_callback(declaration.name, tool_call.arguments)
        result = self._execute_tool(declaration, tool_call)
        self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
        return result

    def _run_classifier(
        self,
        declaration: ToolDeclaration,
        tool_call: ToolCall,
        agent_id: str,
        session_id: str,
        correlation_id: str,
    ) -> ClassifierOutcome:
        if self.classifier is None:
            return ClassifierOutcome(decision=None)
        decision = self.classifier.classify(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            permission_class=declaration.permission.permission_class,
            profile=self.permission_profile,
        )
        if decision is None:
            self.event_store.emit(
                name="classifier.circuit_open",
                kind="audit",
                origin="kernel",
                agent_id=agent_id,
                session_id=session_id,
                correlation_id=correlation_id,
                payload={"tool_name": tool_call.name},
            )
            return ClassifierOutcome(decision=None, circuit_open=True)
        self.event_store.emit(
            name="classifier.decided",
            kind="audit",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={
                "tool_name": tool_call.name,
                "block": decision.block,
                "stage": decision.stage,
                "cached": decision.cached,
                "reason": decision.reason,
            },
        )
        return ClassifierOutcome(decision=decision)

    def _check_shell_command(
        self,
        tool_call: ToolCall,
        agent_id: str,
        session_id: str,
        correlation_id: str,
    ) -> ToolResult | None:
        """Return a blocking ToolResult if the shell command is unsafe, else None."""
        command = tool_call.arguments.get("command", "")
        if not isinstance(command, str):
            return None
        parsed = parse_shell(command)
        if parsed.safe:
            return None
        self.event_store.emit(
            name="shell.blocked",
            kind="audit",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={
                "command": command,
                "risk_level": parsed.risk_level,
                "reason": parsed.reason,
                "too_complex": parsed.too_complex,
            },
        )
        if parsed.risk_level == "critical":
            msg = (
                f"Commande bloquée (risque critique) : {parsed.reason} "
                "Cette commande ne peut pas être approuvée automatiquement."
            )
            result = self._failed_tool_result(msg, code="shell_blocked")
        else:
            label = "trop complexe à analyser" if parsed.too_complex else "risquée"
            msg = (
                f"Commande {label} : {parsed.reason} "
                "Réponds `ok` pour confirmer explicitement ou `non` pour refuser."
            )
            if self.approval_store is not None:
                self.approval_store.request(
                    agent_id=agent_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    tool_name=tool_call.name,
                    permission_class=PermissionClass.SHELL_EXEC,
                    scope={"commands": [command]},
                    arguments=tool_call.arguments,
                    summary=f"Shell: {command[:80]}",
                    reason=parsed.reason,
                    rememberable=False,
                )
            result = self._failed_tool_result(msg, code="approval_required", retryable=True)
        self._record_tool_result(tool_call, result, agent_id, session_id, correlation_id)
        return result

    def _execute_tool(self, declaration: ToolDeclaration, tool_call: ToolCall) -> ToolResult:
        executor = self.tool_executors.get(declaration.name) or self.tool_executors.get(
            declaration.executor
        )
        if executor is None:
            return self._failed_tool_result(
                f"No executor registered for {declaration.name}.",
                code="executor_missing",
            )
        raw_result = executor(tool_call.arguments)
        return ToolResult.model_validate(raw_result)

    def _requested_permission_scope(
        self, declaration: ToolDeclaration, tool_call: ToolCall
    ) -> dict[str, Any]:
        arguments = tool_call.arguments
        permission_class = PermissionClass(declaration.permission.permission_class)
        if permission_class in (PermissionClass.FS_READ, PermissionClass.FS_WRITE):
            if "paths" in arguments:
                return {"paths": arguments["paths"]}
            if "path" in arguments:
                return {"paths": [arguments["path"]]}
            if "source_path" in arguments or "target_path" in arguments:
                return {
                    "paths": [
                        path
                        for path in (arguments.get("source_path"), arguments.get("target_path"))
                        if path
                    ]
                }
        if permission_class == PermissionClass.NETWORK_OUTBOUND:
            if "host" in arguments:
                return {"hosts": [arguments["host"]]}
            if "url" in arguments:
                host = _host_from_url(arguments["url"])
                if host:
                    return {"hosts": [host]}
            if "base_url" in arguments:
                host = _host_from_url(arguments["base_url"])
                if host:
                    return {"hosts": [host]}
        if permission_class == PermissionClass.SHELL_EXEC:
            scope: dict[str, Any] = {}
            if "command" in arguments:
                scope["commands"] = [arguments["command"]]
            if "cwd" in arguments:
                scope["cwd"] = [arguments["cwd"]]
            if "timeout_seconds" in arguments:
                scope["timeout_seconds"] = arguments["timeout_seconds"]
            return scope
        if permission_class == PermissionClass.SECRET_READ and "credential" in arguments:
            return {"credentials": [arguments["credential"]]}
        if permission_class == PermissionClass.AGENT_SPAWN:
            scope = {}
            if "agent" in arguments:
                scope["agents"] = [arguments["agent"]]
            if "max_parallel" in arguments:
                scope["max_parallel"] = arguments["max_parallel"]
            if "max_depth" in arguments:
                scope["max_depth"] = arguments["max_depth"]
            return scope
        if permission_class == PermissionClass.HOST_CONTROL and "action" in arguments:
            return {"actions": [arguments["action"]]}
        if permission_class == PermissionClass.RUNTIME_WRITE:
            scope = {}
            if "target" in arguments:
                scope["targets"] = [arguments["target"]]
            elif arguments.get("target_type") == "system_skill" and "target_name" in arguments:
                scope["targets"] = [f"system_skill:{arguments['target_name']}"]
            elif "target_type" in arguments:
                scope["targets"] = [arguments["target_type"]]
            if "mode" in arguments:
                scope["mode"] = arguments["mode"]
            else:
                scope["mode"] = "proposal_only"
            return scope
        return declaration.permission.scope

    def _record_tool_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        agent_id: str,
        session_id: str,
        correlation_id: str,
    ) -> None:
        name = "tool.completed" if result.ok else "tool.failed"
        self.event_store.emit(
            name=name,
            kind="fact",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "result": result.model_dump(mode="json"),
            },
        )
        self.session_store.append_message(
            agent_id,
            session_id,
            role="tool",
            content=_tool_result_content(result),
            correlation_id=correlation_id,
            metadata={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "ok": result.ok,
            },
        )

    def _compact_if_needed(self, agent_id: str, session_id: str, correlation_id: str) -> None:
        if not self.compaction_config:
            return
        messages = self._provider_messages(agent_id, session_id)
        system = self._system_prompt()
        compacted, level = compact_messages(
            messages,
            config=self.compaction_config,
            system=system,
            provider=self.provider,
            model=self.model,
        )
        if level == CompactionLevel.NONE:
            return
        session = self.session_store.load(agent_id, session_id)
        session.messages = [
            SessionMessage(
                role=m["role"],
                content=str(m.get("content") or ""),
                metadata=m.get("metadata") or {},
            )
            for m in compacted
        ]
        self.session_store.save(session)
        self.event_store.emit(
            name="session.compacted",
            kind="fact",
            origin="kernel",
            agent_id=agent_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload={"level": int(level), "level_name": level.name},
        )

    def _provider_messages(self, agent_id: str, session_id: str) -> list[dict[str, Any]]:
        session = self.session_store.load(agent_id, session_id)
        messages = [
            {
                "role": message.role,
                "content": message.content,
                "metadata": message.metadata,
            }
            for message in session.messages
        ]
        return _sanitize_messages(messages)

    def _system_prompt(self) -> str:
        fragments = [
            skill.prompt
            for skill in self.registry.loaded().values()
            if skill.prompt.strip()
        ]
        return "\n\n".join([self.system_prompt, *fragments]).strip()

    def _load_or_create_session(self, agent_id: str, session_id: str) -> SessionRecord:
        try:
            return self.session_store.load(agent_id, session_id)
        except FileNotFoundError:
            return self.session_store.create(agent_id, session_id=session_id)

    def _remove_internal_messages(self, agent_id: str, session_id: str, correlation_id: str) -> None:
        session = self.session_store.load(agent_id, session_id)
        session.messages = [
            message
            for message in session.messages
            if message.correlation_id != correlation_id
        ]
        self.session_store.save(session)

    def _text_tool_calls(self, text: str) -> list[ToolCall]:
        match = re.search(r"```maurice_tool_calls\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if match is None:
            return []
        try:
            payload = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            raw_calls = payload.get("tool_calls") or payload.get("calls") or []
        else:
            raw_calls = payload
        if not isinstance(raw_calls, list):
            return []
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue
            name = raw_call.get("name") or raw_call.get("tool")
            arguments = raw_call.get("arguments") or raw_call.get("args") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                continue
            try:
                calls.append(
                    ToolCall(
                        id=str(raw_call.get("id") or f"text_call_{index}"),
                        name=name,
                        arguments=arguments,
                    )
                )
            except ValueError:
                continue
        return calls

    @staticmethod
    def _failed_tool_result(
        message: str, *, code: str, retryable: bool = False
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            summary=message,
            data=None,
            trust=TrustLabel.TRUSTED,
            artifacts=[],
            events=[],
            error=ToolError(code=code, message=message, retryable=retryable),
        )


def _tool_activity_entry(name: str, arguments: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    return {
        "name": name,
        "label": tool_short_label(name),
        "target": tool_target(name, arguments),
        "ok": result.ok,
        "summary": result.summary.splitlines()[0][:100] if result.summary else "",
        "error": result.error.code if result.error else None,
    }


def _tool_result_content(result: ToolResult) -> str:
    if result.data is None:
        return result.summary
    try:
        data = json.dumps(result.data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        data = str(result.data)
    return f"{result.summary}\n\nData:\n{data}"


def _host_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return urlparse(value).hostname


_DEFAULT_MAX_TOOL_ITERATIONS = 10


def _max_tool_iterations(limits: dict[str, Any] | None) -> int:
    raw = (limits or {}).get("max_tool_iterations", _DEFAULT_MAX_TOOL_ITERATIONS)
    if not isinstance(raw, int):
        return _DEFAULT_MAX_TOOL_ITERATIONS
    return max(1, min(raw, 100))

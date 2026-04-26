"""Smallest useful one-turn agent loop."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.contracts import (
    PermissionClass,
    ProviderChunkType,
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
from maurice.kernel.session import SessionRecord, SessionStore
from maurice.kernel.skills import SkillRegistry

ToolExecutor = Callable[[dict[str, Any]], ToolResult | dict[str, Any]]


@dataclass
class TurnResult:
    session: SessionRecord
    correlation_id: str
    assistant_text: str
    tool_results: list[ToolResult]
    status: str
    error: str | None = None


class ToolExecutionError(RuntimeError):
    pass


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

    def run_turn(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        limits: dict[str, Any] | None = None,
    ) -> TurnResult:
        session = self._load_or_create_session(agent_id, session_id)
        turn = self.session_store.start_turn(agent_id, session.id)
        correlation_id = turn.correlation_id
        self.event_store.emit(
            name="turn.started",
            kind="progress",
            origin="kernel",
            agent_id=agent_id,
            session_id=session.id,
            correlation_id=correlation_id,
            payload={"message": message},
        )
        self.session_store.append_message(
            agent_id,
            session.id,
            role="user",
            content=message,
            correlation_id=correlation_id,
        )

        assistant_text = ""
        tool_results: list[ToolResult] = []
        status = "completed"
        provider_error: str | None = None
        try:
            for chunk in self.provider.stream(
                messages=self._provider_messages(agent_id, session.id),
                model=self.model,
                tools=list(self.registry.tools.values()),
                system=self._system_prompt(),
                limits=limits or {},
            ):
                if chunk.type == ProviderChunkType.TEXT_DELTA:
                    assistant_text += chunk.delta
                    continue
                if chunk.type == ProviderChunkType.TOOL_CALL and chunk.tool_call is not None:
                    tool_results.append(
                        self._handle_tool_call(
                            chunk.tool_call,
                            agent_id=agent_id,
                            session_id=session.id,
                            correlation_id=correlation_id,
                        )
                    )
                    continue
                if chunk.type == ProviderChunkType.STATUS and chunk.status == ProviderStatus.FAILED:
                    status = "failed"
                    if chunk.error is not None:
                        provider_error = f"{chunk.error.code}: {chunk.error.message}"
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
            raise

        if assistant_text:
            self.session_store.append_message(
                agent_id,
                session.id,
                role="assistant",
                content=assistant_text,
                correlation_id=correlation_id,
            )
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
        return TurnResult(
            session=self.session_store.load(agent_id, session.id),
            correlation_id=correlation_id,
            assistant_text=assistant_text,
            tool_results=tool_results,
            status=status,
            error=provider_error,
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

        if evaluation.decision == PermissionDecision.ASK:
            approved = None
            if self.approval_store is not None:
                approved = self.approval_store.approved_for_replay(
                    permission_class=declaration.permission.permission_class,
                    scope=self._requested_permission_scope(declaration, tool_call),
                    tool_name=declaration.name,
                    arguments=tool_call.arguments,
                )
            if approved is None:
                if self.approval_store is not None:
                    self.approval_store.request(
                        agent_id=agent_id,
                        session_id=session_id,
                        correlation_id=correlation_id,
                        tool_name=declaration.name,
                        permission_class=declaration.permission.permission_class,
                        scope=self._requested_permission_scope(declaration, tool_call),
                        arguments=tool_call.arguments,
                        summary=f"Approve {declaration.name}",
                        reason=evaluation.reason,
                        rememberable=evaluation.rememberable,
                    )
                result = self._failed_tool_result(
                    "Tool execution requires approval.",
                    code="approval_required",
                    retryable=True,
                )
                self._record_tool_result(
                    tool_call, result, agent_id, session_id, correlation_id
                )
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
        result = self._execute_tool(declaration, tool_call)
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
            content=result.summary,
            correlation_id=correlation_id,
            metadata={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "ok": result.ok,
            },
        )

    def _provider_messages(self, agent_id: str, session_id: str) -> list[dict[str, Any]]:
        session = self.session_store.load(agent_id, session_id)
        return [
            {
                "role": message.role,
                "content": message.content,
                "metadata": message.metadata,
            }
            for message in session.messages
        ]

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


def _host_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return urlparse(value).hostname

"""Typed runtime contracts for Maurice.

The models in this module mirror the public contract shapes documented in
CONTRACTS.md. They intentionally avoid feature behavior; the kernel, host, and
skills build on these envelopes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MauriceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
    )


class ProviderChunkType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    STATUS = "status"


class ProviderStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TrustLabel(StrEnum):
    TRUSTED = "trusted"
    LOCAL_MUTABLE = "local_mutable"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    SKILL_GENERATED = "skill_generated"


class PermissionClass(StrEnum):
    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    NETWORK_OUTBOUND = "network.outbound"
    SHELL_EXEC = "shell.exec"
    SECRET_READ = "secret.read"
    AGENT_SPAWN = "agent.spawn"
    HOST_CONTROL = "host.control"
    RUNTIME_WRITE = "runtime.write"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionTtl(StrEnum):
    TURN = "turn"
    SESSION = "session"
    DURATION = "duration"
    FOREVER = "forever"


class EventKind(StrEnum):
    FACT = "fact"
    PROGRESS = "progress"
    SNAPSHOT = "snapshot"
    AUDIT = "audit"


class ToolCall(MauriceModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class UsageMetadata(MauriceModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ProviderError(MauriceModel):
    code: str
    message: str
    retryable: bool = False


class ProviderChunk(MauriceModel):
    type: ProviderChunkType
    delta: str = ""
    tool_call: ToolCall | None = None
    usage: UsageMetadata | None = None
    status: ProviderStatus | None = None
    error: ProviderError | None = None

    @model_validator(mode="after")
    def required_payload_matches_type(self) -> "ProviderChunk":
        if self.type == ProviderChunkType.TOOL_CALL and self.tool_call is None:
            raise ValueError("tool_call chunks require tool_call")
        if self.type == ProviderChunkType.USAGE and self.usage is None:
            raise ValueError("usage chunks require usage")
        if self.type == ProviderChunkType.STATUS and self.status is None:
            raise ValueError("status chunks require status")
        return self


class PermissionScope(MauriceModel):
    permission_class: PermissionClass = Field(alias="class")
    scope: dict[str, Any] = Field(default_factory=dict)


class PermissionRule(PermissionScope):
    decision: PermissionDecision
    ttl: PermissionTtl = PermissionTtl.TURN
    rememberable: bool = False
    reason: str = ""


class ToolPermission(MauriceModel):
    permission_class: PermissionClass = Field(alias="class")
    scope: dict[str, Any] = Field(default_factory=dict)


class ToolTrust(MauriceModel):
    input: TrustLabel
    output: TrustLabel


class ToolDeclaration(MauriceModel):
    name: str
    owner_skill: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    permission: ToolPermission
    trust: ToolTrust
    executor: str

    @field_validator("name")
    @classmethod
    def canonical_tool_name(cls, value: str) -> str:
        if value.count(".") != 1 or any(not part for part in value.split(".")):
            raise ValueError("tool names must use <skill>.<tool>")
        return value


class ToolArtifact(MauriceModel):
    type: str
    path: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ToolEvent(MauriceModel):
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolError(MauriceModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(MauriceModel):
    ok: bool
    summary: str
    data: Any = None
    trust: TrustLabel
    artifacts: list[ToolArtifact] = Field(default_factory=list)
    events: list[ToolEvent] = Field(default_factory=list)
    error: ToolError | None = None

    @model_validator(mode="after")
    def error_matches_status(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("successful tool results must not include error")
        if not self.ok and self.error is None:
            raise ValueError("failed tool results require error")
        return self


class Event(MauriceModel):
    id: str
    time: datetime
    kind: EventKind
    name: str
    origin: str
    agent_id: str
    session_id: str
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class PendingApproval(MauriceModel):
    id: str
    agent_id: str
    session_id: str
    correlation_id: str
    tool_name: str
    permission_class: PermissionClass
    scope: dict[str, Any] = Field(default_factory=dict)
    arguments_hash: str
    summary: str
    reason: str
    created_at: datetime
    expires_at: datetime
    rememberable: bool = False
    status: PendingApprovalStatus = PendingApprovalStatus.PENDING


class SkillOrigin(StrEnum):
    SYSTEM = "system"
    USER = "user"


class SkillPermission(MauriceModel):
    permission_class: PermissionClass = Field(alias="class")
    scope: dict[str, Any] = Field(default_factory=dict)


class SkillToolExport(MauriceModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    permission_class: PermissionClass
    permission_scope: dict[str, Any] = Field(default_factory=dict)
    trust: ToolTrust = Field(
        default_factory=lambda: ToolTrust(input=TrustLabel.LOCAL_MUTABLE, output=TrustLabel.LOCAL_MUTABLE)
    )
    executor: str = ""


class SkillRequires(MauriceModel):
    binaries: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class SkillDependencies(MauriceModel):
    skills: list[str] = Field(default_factory=list)
    optional_skills: list[str] = Field(default_factory=list)


class SkillStorage(MauriceModel):
    engine: str
    path: str
    schema_version: int = Field(ge=0)
    migrations: list[str] = Field(default_factory=list)


class SkillDreams(MauriceModel):
    attachment: str | None = None
    input_builder: str | None = None


class SkillEvents(MauriceModel):
    state_publisher: str | None = None


class SkillManifest(MauriceModel):
    name: str
    version: str
    origin: SkillOrigin
    mutable: bool
    description: str
    config_namespace: str
    required: bool = False
    requires: SkillRequires = Field(default_factory=SkillRequires)
    dependencies: SkillDependencies = Field(default_factory=SkillDependencies)
    permissions: list[SkillPermission] = Field(default_factory=list)
    tools: list[SkillToolExport] = Field(default_factory=list)
    backend: dict[str, Any] | str | None = None
    storage: SkillStorage | None = None
    dreams: SkillDreams | None = None
    events: SkillEvents | None = None


class AgentConfig(MauriceModel):
    id: str
    workspace: str
    skills: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    permission_profile: Literal["safe", "limited", "power"]
    status: Literal["active", "disabled", "archived"] = "active"
    default: bool = False
    channels: list[str] = Field(default_factory=list)
    model: dict[str, Any] | None = None
    event_stream: str | None = None


class SubagentRunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubagentRun(MauriceModel):
    id: str
    parent_agent_id: str
    task: str
    workspace: str
    write_scope: dict[str, Any] = Field(default_factory=dict)
    permission_scope: dict[str, Any] = Field(default_factory=dict)
    context_inheritance: Literal["none", "current_task", "linked_session"] = "current_task"
    base_agent: str | None = None
    state: SubagentRunState = SubagentRunState.CREATED
    event_stream: str | None = None
    safe_to_resume: bool = False


class DreamFreshness(MauriceModel):
    generated_at: datetime
    expires_at: datetime | None = None


class DreamSignal(MauriceModel):
    id: str
    type: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class DreamInput(MauriceModel):
    skill: str
    trust: TrustLabel
    freshness: DreamFreshness
    signals: list[DreamSignal] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class DreamAction(MauriceModel):
    id: str
    owner_skill: str
    type: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = True


class DreamReport(MauriceModel):
    id: str
    generated_at: datetime
    status: Literal["completed", "failed"]
    summary: str
    inputs: list[DreamInput] = Field(default_factory=list)
    proposed_actions: list[DreamAction] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

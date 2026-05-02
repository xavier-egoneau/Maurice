"""Permission profile resolution and scoped checks."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from maurice.kernel.contracts import (
    MauriceModel,
    PermissionClass,
    PermissionDecision,
    PermissionRule,
)

PermissionProfileName = Literal["safe", "limited", "power"]

PROFILE_ORDER: dict[PermissionProfileName, int] = {
    "safe": 0,
    "limited": 1,
    "power": 2,
}


class PermissionContext(MauriceModel):
    workspace_root: str
    runtime_root: str
    agent_workspace_root: str | None = None
    active_project_root: str | None = None
    home_root: str | None = None
    maurice_home_root: str | None = None

    def variables(self) -> dict[str, str]:
        workspace = Path(self.workspace_root).expanduser().resolve()
        agent_workspace = Path(self.agent_workspace_root).expanduser().resolve() if self.agent_workspace_root else workspace
        active_project = (
            Path(self.active_project_root).expanduser().resolve()
            if self.active_project_root
            else agent_workspace / "content"
        )
        return {
            "$workspace": str(workspace),
            "$agent_workspace": str(agent_workspace),
            "$agent_content": str(agent_workspace / "content"),
            "$project": str(active_project),
            "$runtime": str(Path(self.runtime_root).expanduser().resolve()),
            "$home": str(Path(self.home_root or Path.home()).expanduser().resolve()),
            "$maurice_home": str(
                Path(self.maurice_home_root or Path.home() / ".maurice")
                .expanduser()
                .resolve()
            ),
        }


class PermissionEvaluation(MauriceModel):
    decision: PermissionDecision
    permission_class: PermissionClass
    scope: dict[str, Any] = Field(default_factory=dict)
    rememberable: bool = False
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == PermissionDecision.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.decision == PermissionDecision.ASK

    @property
    def denied(self) -> bool:
        return self.decision == PermissionDecision.DENY


PROFILE_RULES: dict[PermissionProfileName, dict[PermissionClass, PermissionRule]] = {
    "safe": {
        PermissionClass.FS_READ: PermissionRule.model_validate(
            {
                "class": "fs.read",
                "decision": "allow",
                "scope": {
                    "paths": ["$workspace/**", "$project/**"],
                    "exclude": ["$workspace/secrets/**", "$project/secrets/**"],
                },
                "rememberable": False,
                "reason": "Safe profile allows workspace reads.",
            }
        ),
        PermissionClass.FS_WRITE: PermissionRule.model_validate(
            {
                "class": "fs.write",
                "decision": "ask",
                "scope": {
                    "paths": ["$workspace/**", "$project/**"],
                    "exclude": ["$workspace/secrets/**", "$project/secrets/**"],
                },
                "rememberable": True,
                "reason": "Safe profile asks before workspace writes.",
            }
        ),
        PermissionClass.NETWORK_OUTBOUND: PermissionRule.model_validate(
            {
                "class": "network.outbound",
                "decision": "ask",
                "scope": {"hosts": ["*"]},
                "rememberable": True,
                "reason": "Safe profile asks before network access.",
            }
        ),
        PermissionClass.SHELL_EXEC: PermissionRule.model_validate(
            {
                "class": "shell.exec",
                "decision": "deny",
                "scope": {"commands": []},
                "rememberable": False,
                "reason": "Safe profile denies shell execution.",
            }
        ),
        PermissionClass.SECRET_READ: PermissionRule.model_validate(
            {
                "class": "secret.read",
                "decision": "ask",
                "scope": {"credentials": []},
                "rememberable": False,
                "reason": "Safe profile asks before reading secrets.",
            }
        ),
        PermissionClass.AGENT_SPAWN: PermissionRule.model_validate(
            {
                "class": "agent.spawn",
                "decision": "deny",
                "scope": {"agents": [], "max_parallel": 0},
                "rememberable": False,
                "reason": "Safe profile denies agent spawning.",
            }
        ),
        PermissionClass.HOST_CONTROL: PermissionRule.model_validate(
            {
                "class": "host.control",
                "decision": "deny",
                "scope": {"actions": []},
                "rememberable": False,
                "reason": "Safe profile denies host control.",
            }
        ),
        PermissionClass.RUNTIME_WRITE: PermissionRule.model_validate(
            {
                "class": "runtime.write",
                "decision": "deny",
                "scope": {"mode": "proposal_only"},
                "rememberable": False,
                "reason": "Safe profile denies runtime writes.",
            }
        ),
    },
    "limited": {
        PermissionClass.FS_READ: PermissionRule.model_validate(
            {
                "class": "fs.read",
                "decision": "allow",
                "scope": {
                    "paths": ["$workspace/**", "$project/**"],
                    "exclude": ["$workspace/secrets/**", "$project/secrets/**", "$runtime/**"],
                },
                "rememberable": False,
                "reason": "Limited profile allows workspace reads.",
            }
        ),
        PermissionClass.FS_WRITE: PermissionRule.model_validate(
            {
                "class": "fs.write",
                "decision": "allow",
                "scope": {
                    "paths": ["$workspace/**", "$project/**"],
                    "exclude": ["$workspace/secrets/**", "$project/secrets/**", "$runtime/**"],
                },
                "rememberable": False,
                "reason": "Limited profile allows workspace writes.",
            }
        ),
        PermissionClass.NETWORK_OUTBOUND: PermissionRule.model_validate(
            {
                "class": "network.outbound",
                "decision": "ask",
                "scope": {"hosts": ["*"]},
                "rememberable": True,
                "reason": "Limited profile asks before network access.",
            }
        ),
        PermissionClass.SHELL_EXEC: PermissionRule.model_validate(
            {
                "class": "shell.exec",
                "decision": "ask",
                "scope": {
                    "commands": ["git", "pytest", "ruff"],
                    "cwd": ["$workspace/**", "$project/**"],
                    "timeout_seconds_max": 300,
                },
                "rememberable": True,
                "reason": "Limited profile asks before scoped shell execution.",
            }
        ),
        PermissionClass.SECRET_READ: PermissionRule.model_validate(
            {
                "class": "secret.read",
                "decision": "ask",
                "scope": {"credentials": []},
                "rememberable": False,
                "reason": "Limited profile asks before reading secrets.",
            }
        ),
        PermissionClass.AGENT_SPAWN: PermissionRule.model_validate(
            {
                "class": "agent.spawn",
                "decision": "ask",
                "scope": {"agents": [], "max_parallel": 3, "max_depth": 2},
                "rememberable": True,
                "reason": "Limited profile asks before spawning agents.",
            }
        ),
        PermissionClass.HOST_CONTROL: PermissionRule.model_validate(
            {
                "class": "host.control",
                "decision": "ask",
                "scope": {
                    "actions": [
                        "logs.read",
                        "service.status",
                        "credentials.list",
                        "credentials.capture",
                        "agents.list",
                        "agents.create",
                        "agents.update",
                        "agents.delete",
                        "telegram.configure",
                    ]
                },
                "rememberable": True,
                "reason": "Limited profile asks before host control.",
            }
        ),
        PermissionClass.RUNTIME_WRITE: PermissionRule.model_validate(
            {
                "class": "runtime.write",
                "decision": "ask",
                "scope": {
                    "targets": ["kernel", "system_skill:*"],
                    "mode": "proposal_only",
                },
                "rememberable": False,
                "reason": "Limited profile asks for runtime write proposals.",
            }
        ),
    },
    "power": {
        PermissionClass.FS_READ: PermissionRule.model_validate(
            {
                "class": "fs.read",
                "decision": "allow",
                "scope": {
                    "paths": ["$workspace/**", "$project/**", "$home/**"],
                    "exclude": [
                        "$runtime/**",
                        "$maurice_home/**",
                        "$project/secrets/**",
                        "$home/.ssh/**",
                        "$home/.gnupg/**",
                    ],
                },
                "rememberable": False,
                "reason": "Power profile allows broad reads outside protected paths.",
            }
        ),
        PermissionClass.FS_WRITE: PermissionRule.model_validate(
            {
                "class": "fs.write",
                "decision": "allow",
                "scope": {
                    "paths": ["$workspace/**", "$project/**"],
                    "exclude": ["$runtime/**", "$workspace/secrets/**", "$project/secrets/**"],
                },
                "rememberable": False,
                "reason": "Power profile allows workspace writes.",
            }
        ),
        PermissionClass.NETWORK_OUTBOUND: PermissionRule.model_validate(
            {
                "class": "network.outbound",
                "decision": "allow",
                "scope": {"hosts": ["*"]},
                "rememberable": False,
                "reason": "Power profile allows network access.",
            }
        ),
        PermissionClass.SHELL_EXEC: PermissionRule.model_validate(
            {
                "class": "shell.exec",
                "decision": "ask",
                "scope": {
                    "commands": ["*"],
                    "cwd": ["$workspace/**", "$project/**"],
                    "timeout_seconds_max": 900,
                },
                "rememberable": True,
                "reason": "Power profile asks before shell execution.",
            }
        ),
        PermissionClass.SECRET_READ: PermissionRule.model_validate(
            {
                "class": "secret.read",
                "decision": "ask",
                "scope": {"credentials": []},
                "rememberable": False,
                "reason": "Power profile asks before reading secrets.",
            }
        ),
        PermissionClass.AGENT_SPAWN: PermissionRule.model_validate(
            {
                "class": "agent.spawn",
                "decision": "allow",
                "scope": {"agents": ["*"], "max_parallel": 6, "max_depth": 3},
                "rememberable": False,
                "reason": "Power profile allows agent spawning.",
            }
        ),
        PermissionClass.HOST_CONTROL: PermissionRule.model_validate(
            {
                "class": "host.control",
                "decision": "ask",
                "scope": {
                    "actions": [
                        "logs.read",
                        "service.status",
                        "service.restart",
                        "credentials.list",
                        "credentials.capture",
                        "agents.list",
                        "agents.create",
                        "agents.update",
                        "agents.delete",
                        "telegram.configure",
                    ]
                },
                "rememberable": True,
                "reason": "Power profile asks before host control.",
            }
        ),
        PermissionClass.RUNTIME_WRITE: PermissionRule.model_validate(
            {
                "class": "runtime.write",
                "decision": "ask",
                "scope": {
                    "targets": ["kernel", "host", "system_skill:*"],
                    "mode": "proposal_only",
                },
                "rememberable": False,
                "reason": "Power profile asks for runtime write proposals.",
            }
        ),
    },
}


def profile_rule(
    profile: PermissionProfileName, permission_class: PermissionClass | str
) -> PermissionRule:
    return PROFILE_RULES[profile][PermissionClass(permission_class)]


def agent_profile_requires_confirmation(
    global_profile: PermissionProfileName,
    agent_profile: PermissionProfileName,
    *,
    confirmed: bool = False,
) -> bool:
    more_permissive = PROFILE_ORDER[agent_profile] > PROFILE_ORDER[global_profile]
    return more_permissive and not confirmed


def evaluate_permission(
    profile: PermissionProfileName,
    permission_class: PermissionClass | str,
    requested_scope: dict[str, Any],
    context: PermissionContext,
) -> PermissionEvaluation:
    rule = profile_rule(profile, permission_class)
    permission_class_value = PermissionClass(permission_class)
    in_scope = scope_contains(rule.scope, requested_scope, context, permission_class_value)
    if in_scope:
        return PermissionEvaluation(
            decision=rule.decision,
            permission_class=permission_class_value,
            scope=rule.scope,
            rememberable=rule.rememberable,
            reason=rule.reason,
        )
    return PermissionEvaluation(
        decision=PermissionDecision.DENY,
        permission_class=permission_class_value,
        scope=rule.scope,
        rememberable=False,
        reason=f"Requested {permission_class_value} scope is outside profile scope.",
    )


def scope_contains(
    allowed: dict[str, Any],
    requested: dict[str, Any],
    context: PermissionContext,
    permission_class: PermissionClass,
) -> bool:
    if permission_class in (PermissionClass.FS_READ, PermissionClass.FS_WRITE):
        return _paths_allowed(allowed, requested, context)
    if permission_class == PermissionClass.NETWORK_OUTBOUND:
        return _values_allowed(allowed.get("hosts", []), requested.get("hosts", []))
    if permission_class == PermissionClass.SHELL_EXEC:
        return _shell_allowed(allowed, requested, context)
    if permission_class == PermissionClass.SECRET_READ:
        return _values_allowed(
            allowed.get("credentials", []), requested.get("credentials", [])
        )
    if permission_class == PermissionClass.AGENT_SPAWN:
        return _agent_spawn_allowed(allowed, requested)
    if permission_class == PermissionClass.HOST_CONTROL:
        return _values_allowed(allowed.get("actions", []), requested.get("actions", []))
    if permission_class == PermissionClass.RUNTIME_WRITE:
        return _runtime_write_allowed(allowed, requested)
    return False


def _paths_allowed(
    allowed: dict[str, Any], requested: dict[str, Any], context: PermissionContext
) -> bool:
    requested_paths = requested.get("paths", [])
    if not requested_paths:
        return False
    allowed_patterns = [_expand_pattern(p, context) for p in allowed.get("paths", [])]
    excluded_patterns = [_expand_pattern(p, context) for p in allowed.get("exclude", [])]
    for requested_path in requested_paths:
        resolved = _resolve_requested_path(str(requested_path), context)
        if any(_matches(resolved, pattern) for pattern in excluded_patterns):
            return False
        if not any(_matches(resolved, pattern) for pattern in allowed_patterns):
            return False
    return True


def _shell_allowed(
    allowed: dict[str, Any], requested: dict[str, Any], context: PermissionContext
) -> bool:
    commands_ok = _values_allowed(
        allowed.get("commands", []), requested.get("commands", [])
    )
    if not commands_ok:
        return False
    timeout = requested.get("timeout_seconds")
    timeout_max = allowed.get("timeout_seconds_max")
    if timeout is not None and timeout_max is not None and timeout > timeout_max:
        return False
    cwd_values = requested.get("cwd", [])
    if isinstance(cwd_values, str):
        cwd_values = [cwd_values]
    return _paths_allowed({"paths": allowed.get("cwd", []), "exclude": []}, {"paths": cwd_values}, context)


def _agent_spawn_allowed(allowed: dict[str, Any], requested: dict[str, Any]) -> bool:
    if not _values_allowed(allowed.get("agents", []), requested.get("agents", [])):
        return False
    if requested.get("max_parallel", 0) > allowed.get("max_parallel", 0):
        return False
    if requested.get("max_depth", 0) > allowed.get("max_depth", 0):
        return False
    return True


def _runtime_write_allowed(allowed: dict[str, Any], requested: dict[str, Any]) -> bool:
    if allowed.get("mode") != requested.get("mode", "proposal_only"):
        return False
    return _values_allowed(allowed.get("targets", []), requested.get("targets", []))


def _values_allowed(allowed_values: list[str], requested_values: list[str] | str) -> bool:
    if isinstance(requested_values, str):
        requested_values = [requested_values]
    if not requested_values:
        return False
    return all(
        any(fnmatch.fnmatch(value, pattern) for pattern in allowed_values)
        for value in requested_values
    )


def _expand_pattern(pattern: str, context: PermissionContext) -> str:
    return _expand_value(pattern, context)


def _expand_value(value: str, context: PermissionContext) -> str:
    expanded = value
    for variable, replacement in context.variables().items():
        expanded = expanded.replace(variable, replacement)
    return expanded


def _resolve_requested_path(path: str, context: PermissionContext) -> str:
    expanded = _expand_value(path, context)
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        variables = context.variables()
        first_part = candidate.parts[0] if candidate.parts else ""
        if first_part == "$project":
            candidate = Path(variables["$project"]).joinpath(*candidate.parts[1:])
        elif first_part == "$agent_content":
            candidate = Path(variables["$agent_content"]).joinpath(*candidate.parts[1:])
        elif first_part == "$agent_workspace":
            candidate = Path(variables["$agent_workspace"]).joinpath(*candidate.parts[1:])
        elif first_part in {"agents", "config", "content", "sessions", "skills"}:
            candidate = Path(variables["$workspace"]) / candidate
        else:
            candidate = Path(variables["$project"]) / candidate
    return str(candidate.resolve())


def _matches(value: str, pattern: str) -> bool:
    return fnmatch.fnmatch(value, pattern) or value == pattern.rstrip("/**")

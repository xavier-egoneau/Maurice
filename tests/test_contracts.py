from __future__ import annotations

import pytest
from pydantic import ValidationError

from maurice.kernel.contracts import (
    AgentConfig,
    DreamInput,
    DreamReport,
    Event,
    PendingApproval,
    PermissionRule,
    ProviderChunk,
    SkillManifest,
    SubagentRun,
    ToolDeclaration,
    ToolResult,
)


def test_provider_chunk_validates_tool_call_shape() -> None:
    chunk = ProviderChunk.model_validate(
        {
            "type": "tool_call",
            "tool_call": {
                "id": "call_1",
                "name": "filesystem.read",
                "arguments": {"path": "README.md"},
            },
        }
    )

    assert chunk.tool_call is not None
    assert chunk.tool_call.name == "filesystem.read"


def test_provider_chunk_requires_payload_for_kind() -> None:
    with pytest.raises(ValidationError):
        ProviderChunk.model_validate({"type": "usage"})


def test_tool_declaration_validates_contract_example() -> None:
    declaration = ToolDeclaration.model_validate(
        {
            "name": "filesystem.write",
            "owner_skill": "filesystem",
            "description": "Write a text file inside an allowed scope.",
            "input_schema": {},
            "permission": {
                "class": "fs.write",
                "scope": {"paths": ["$workspace/**"]},
            },
            "trust": {"input": "local_mutable", "output": "local_mutable"},
            "executor": "tools.write",
        }
    )

    assert declaration.name == "filesystem.write"
    assert declaration.permission.permission_class == "fs.write"


def test_tool_declaration_rejects_non_canonical_name() -> None:
    with pytest.raises(ValidationError):
        ToolDeclaration.model_validate(
            {
                "name": "write",
                "owner_skill": "filesystem",
                "description": "Write.",
                "permission": {"class": "fs.write", "scope": {}},
                "trust": {"input": "trusted", "output": "trusted"},
                "executor": "tools.write",
            }
        )


def test_tool_result_success_and_failure_share_envelope() -> None:
    success = ToolResult.model_validate(
        {
            "ok": True,
            "summary": "File written.",
            "data": {"path": "$workspace/notes/today.md"},
            "trust": "local_mutable",
            "artifacts": [{"type": "file", "path": "$workspace/notes/today.md"}],
            "events": [
                {
                    "name": "filesystem.file_written",
                    "payload": {"path": "$workspace/notes/today.md"},
                }
            ],
            "error": None,
        }
    )
    failure = ToolResult.model_validate(
        {
            "ok": False,
            "summary": "Write denied.",
            "data": None,
            "trust": "trusted",
            "artifacts": [],
            "events": [],
            "error": {
                "code": "permission_denied",
                "message": "fs.write outside allowed scope",
                "retryable": False,
            },
        }
    )

    assert set(success.model_dump().keys()) == set(failure.model_dump().keys())


def test_failed_tool_result_requires_error() -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate(
            {
                "ok": False,
                "summary": "Nope.",
                "data": None,
                "trust": "trusted",
                "artifacts": [],
                "events": [],
                "error": None,
            }
        )


def test_invalid_permission_class_fails_validation() -> None:
    with pytest.raises(ValidationError):
        PermissionRule.model_validate(
            {
                "class": "fs.delete",
                "decision": "ask",
                "scope": {},
                "ttl": "turn",
                "rememberable": False,
                "reason": "Unknown class.",
            }
        )


def test_event_and_approval_contracts_validate_examples() -> None:
    event = Event.model_validate(
        {
            "id": "evt_1",
            "time": "2026-04-26T00:00:00Z",
            "kind": "fact",
            "name": "tool.completed",
            "origin": "kernel",
            "agent_id": "main",
            "session_id": "sess_1",
            "correlation_id": "turn_1",
            "payload": {},
        }
    )
    approval = PendingApproval.model_validate(
        {
            "id": "approval_1",
            "agent_id": "main",
            "session_id": "sess_1",
            "correlation_id": "turn_1",
            "tool_name": "filesystem.write",
            "permission_class": "fs.write",
            "scope": {},
            "arguments_hash": "sha256:abc",
            "summary": "Write file notes/today.md",
            "reason": "The tool requests fs.write.",
            "created_at": "2026-04-26T00:00:00Z",
            "expires_at": "2026-04-26T00:30:00Z",
            "rememberable": False,
            "status": "pending",
        }
    )

    assert event.name == "tool.completed"
    assert approval.status == "pending"


def test_skill_manifest_agent_subagent_and_dream_contracts_validate() -> None:
    manifest = SkillManifest.model_validate(
        {
            "name": "memory",
            "version": "0.1.0",
            "origin": "system",
            "mutable": False,
            "description": "Durable memory storage and retrieval.",
            "config_namespace": "skills.memory",
            "requires": {"binaries": [], "credentials": []},
            "permissions": [
                {
                    "class": "fs.read",
                    "scope": {
                        "paths": [
                            "$agent_workspace/memory/**",
                            "$workspace/.maurice/memory.sqlite",
                        ]
                    },
                }
            ],
            "tools": [
                {
                    "name": "memory.remember",
                    "input_schema": {},
                    "permission_class": "fs.write",
                }
            ],
            "backend": None,
            "storage": {
                "engine": "sqlite",
                "path": "$agent_workspace/memory/memory.sqlite",
                "schema_version": 1,
                "migrations": ["migrations/001_init.sql"],
            },
            "dreams": {
                "attachment": "dreams.md",
                "input_builder": "dreams.build_inputs",
            },
            "daily": {"attachment": "daily.md"},
            "events": {"state_publisher": "state.publish"},
        }
    )
    agent = AgentConfig.model_validate(
        {
            "id": "main",
            "default": True,
            "workspace": "$workspace/agents/main",
            "skills": ["filesystem", "memory"],
            "permission_profile": "safe",
            "channels": ["telegram"],
            "event_stream": "$workspace/agents/main/events.jsonl",
        }
    )
    run = SubagentRun.model_validate(
        {
            "id": "run_1",
            "parent_agent_id": "main",
            "task": "Run tests.",
            "workspace": "$workspace/runs/run_1",
            "write_scope": {"paths": ["$workspace/runs/run_1/**"]},
            "permission_scope": {"classes": ["fs.read"]},
        }
    )
    dream_input = DreamInput.model_validate(
        {
            "skill": "memory",
            "trust": "local_mutable",
            "freshness": {
                "generated_at": "2026-04-26T00:00:00Z",
                "expires_at": None,
            },
            "signals": [
                {
                    "id": "sig_1",
                    "type": "open_loop",
                    "summary": "Project X has not been revisited recently.",
                    "data": {},
                }
            ],
            "limits": ["Only includes non-archived memories."],
        }
    )
    report = DreamReport.model_validate(
        {
            "id": "dream_1",
            "generated_at": "2026-04-26T00:00:00Z",
            "status": "completed",
            "summary": "No action needed.",
            "inputs": [dream_input.model_dump(mode="json")],
            "proposed_actions": [],
            "errors": [],
        }
    )

    assert manifest.name == "memory"
    assert manifest.daily.attachment == "daily.md"
    assert agent.id == "main"
    assert agent.credentials == []
    assert run.state == "created"
    assert report.inputs[0].skill == "memory"

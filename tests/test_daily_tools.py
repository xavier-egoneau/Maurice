from __future__ import annotations

import json
from datetime import UTC, datetime

from maurice.kernel.permissions import PermissionContext
from maurice.kernel.skills import SkillLoader, SkillRoot
from maurice.system_skills.daily.tools import digest


def context(tmp_path) -> PermissionContext:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    return PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))


def registry(enabled_skills):
    return SkillLoader(
        [SkillRoot(path="maurice/system_skills", origin="system", mutable=False)],
        enabled_skills=enabled_skills,
    ).load()


def test_daily_digest_uses_agent_dream_report_and_daily_fragments(tmp_path) -> None:
    permission_context = context(tmp_path)
    dreams_dir = tmp_path / "workspace" / "agents" / "main" / "dreams"
    dreams_dir.mkdir(parents=True)
    (dreams_dir / "dream_1.json").write_text(
        json.dumps(
            {
                "id": "dream_1",
                "generated_at": datetime.now(UTC).isoformat(),
                "status": "completed",
                "summary": "Dream reviewed one signal.",
                "inputs": [
                    {
                        "skill": "memory",
                        "trust": "skill_generated",
                        "freshness": {"generated_at": datetime.now(UTC).isoformat()},
                        "signals": [
                            {
                                "id": "signal_1",
                                "type": "memory",
                                "summary": "Review the project roadmap.",
                                "data": {},
                            }
                        ],
                        "limits": [],
                    }
                ],
                "proposed_actions": [],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    result = digest({}, permission_context, registry(["daily", "memory"]), agent_id="main")

    assert result.ok
    assert "Review the project roadmap." in result.summary
    assert "memory" in result.data["daily_attachments"]
    assert result.data["report"]["id"] == "dream_1"

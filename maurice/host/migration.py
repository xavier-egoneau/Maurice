"""Conservative Jarvis-to-Maurice migration helpers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field

from maurice.kernel.config import load_workspace_config
from maurice.kernel.contracts import MauriceModel
from maurice.kernel.permissions import PermissionContext
from maurice.system_skills.memory.tools import remember


class MigrationItem(MauriceModel):
    kind: str
    source: str
    destination: str | None = None
    status: str
    reason: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class MigrationReport(MauriceModel):
    jarvis_root: str
    workspace_root: str | None = None
    dry_run: bool = True
    items: list[MigrationItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def migrated_count(self) -> int:
        return len([item for item in self.items if item.status == "migrated"])


def inspect_jarvis_workspace(jarvis_root: str | Path) -> MigrationReport:
    root = Path(jarvis_root).expanduser().resolve()
    report = MigrationReport(jarvis_root=str(root))
    if not root.exists():
        report.warnings.append(f"Jarvis workspace does not exist: {root}")
        return report
    _inspect_skills(root, report)
    _inspect_memory_exports(root, report)
    _inspect_artifacts(root, report)
    _inspect_credentials(root, report)
    _inspect_excluded_state(root, report)
    return report


def migrate_jarvis_workspace(
    jarvis_root: str | Path,
    workspace_root: str | Path,
    *,
    dry_run: bool = True,
    include_artifacts: bool = False,
) -> MigrationReport:
    source_root = Path(jarvis_root).expanduser().resolve()
    bundle = load_workspace_config(workspace_root)
    workspace = Path(bundle.host.workspace_root)
    report = MigrationReport(
        jarvis_root=str(source_root),
        workspace_root=str(workspace),
        dry_run=dry_run,
    )
    if not source_root.exists():
        report.warnings.append(f"Jarvis workspace does not exist: {source_root}")
        return report

    _migrate_skills(source_root, workspace, report, dry_run=dry_run)
    _migrate_memory(source_root, workspace, Path(bundle.host.runtime_root), report, dry_run=dry_run)
    if include_artifacts:
        _migrate_artifacts(source_root, workspace, report, dry_run=dry_run)
    else:
        _inspect_artifacts(source_root, report)
        for item in report.items:
            if item.kind == "artifact":
                item.status = "skipped"
                item.reason = "artifact copy requires include_artifacts"
    _inspect_credentials(source_root, report)
    _inspect_excluded_state(source_root, report)
    _write_report(workspace, report, dry_run=dry_run)
    return report


def _inspect_skills(root: Path, report: MigrationReport) -> None:
    for skill_dir in _skill_dirs(root):
        status = "candidate" if (skill_dir / "skill.yaml").is_file() else "skipped"
        reason = "" if status == "candidate" else "missing skill.yaml"
        report.items.append(
            MigrationItem(
                kind="skill",
                source=str(skill_dir),
                status=status,
                reason=reason,
                provenance={"origin": "jarvis", "relative_path": str(skill_dir.relative_to(root))},
            )
        )


def _migrate_skills(root: Path, workspace: Path, report: MigrationReport, *, dry_run: bool) -> None:
    for skill_dir in _skill_dirs(root):
        destination = workspace / "skills" / skill_dir.name
        if not (skill_dir / "skill.yaml").is_file():
            report.items.append(
                MigrationItem(
                    kind="skill",
                    source=str(skill_dir),
                    destination=str(destination),
                    status="skipped",
                    reason="missing skill.yaml",
                    provenance={"origin": "jarvis"},
                )
            )
            continue
        if destination.exists():
            report.items.append(
                MigrationItem(
                    kind="skill",
                    source=str(skill_dir),
                    destination=str(destination),
                    status="skipped",
                    reason="destination already exists",
                    provenance={"origin": "jarvis"},
                )
            )
            continue
        if not dry_run:
            shutil.copytree(skill_dir, destination)
            _write_provenance(destination / ".maurice_migration.json", skill_dir, root)
        report.items.append(
            MigrationItem(
                kind="skill",
                source=str(skill_dir),
                destination=str(destination),
                status="dry_run" if dry_run else "migrated",
                provenance={"origin": "jarvis", "relative_path": str(skill_dir.relative_to(root))},
            )
        )


def _inspect_memory_exports(root: Path, report: MigrationReport) -> None:
    for path in _memory_export_paths(root):
        count = len(_load_memory_export(path))
        report.items.append(
            MigrationItem(
                kind="memory",
                source=str(path),
                status="candidate",
                reason=f"{count} exported memories",
                provenance={"origin": "jarvis", "format": "explicit_export"},
            )
        )


def _migrate_memory(
    root: Path,
    workspace: Path,
    runtime: Path,
    report: MigrationReport,
    *,
    dry_run: bool,
) -> None:
    context = PermissionContext(workspace_root=str(workspace), runtime_root=str(runtime))
    for path in _memory_export_paths(root):
        memories = _load_memory_export(path)
        imported = 0
        for memory in memories:
            content = memory.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if not dry_run:
                tags = memory.get("tags", [])
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    tags = []
                remember(
                    {
                        "content": content,
                        "tags": sorted(set([*tags, "jarvis_migration"])),
                        "source": f"jarvis:{path.name}",
                    },
                    context,
                )
            imported += 1
        report.items.append(
            MigrationItem(
                kind="memory",
                source=str(path),
                destination=str(workspace / "skills" / "memory" / "memory.sqlite"),
                status="dry_run" if dry_run else "migrated",
                reason=f"{imported} memories imported through explicit export",
                provenance={"origin": "jarvis", "format": "explicit_export"},
            )
        )


def _inspect_artifacts(root: Path, report: MigrationReport) -> None:
    artifacts = root / "artifacts"
    if not artifacts.is_dir():
        return
    for path in sorted(item for item in artifacts.iterdir() if item.is_file()):
        report.items.append(
            MigrationItem(
                kind="artifact",
                source=str(path),
                status="candidate",
                provenance={"origin": "jarvis", "relative_path": str(path.relative_to(root))},
            )
        )


def _migrate_artifacts(root: Path, workspace: Path, report: MigrationReport, *, dry_run: bool) -> None:
    artifacts = root / "artifacts"
    if not artifacts.is_dir():
        return
    destination_root = workspace / "content" / "jarvis"
    for path in sorted(item for item in artifacts.iterdir() if item.is_file()):
        destination = destination_root / path.name
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            _write_provenance(destination.with_suffix(destination.suffix + ".provenance.json"), path, root)
        report.items.append(
            MigrationItem(
                kind="artifact",
                source=str(path),
                destination=str(destination),
                status="dry_run" if dry_run else "migrated",
                provenance={"origin": "jarvis", "relative_path": str(path.relative_to(root))},
            )
        )


def _inspect_credentials(root: Path, report: MigrationReport) -> None:
    for name in ("credentials.yaml", "credentials.yml", ".env"):
        path = root / name
        if path.exists():
            report.items.append(
                MigrationItem(
                    kind="credential",
                    source=str(path),
                    status="skipped",
                    reason="credential migration requires explicit provider-specific review",
                    provenance={"origin": "jarvis"},
                )
            )


def _inspect_excluded_state(root: Path, report: MigrationReport) -> None:
    for name in ("config", "sessions"):
        path = root / name
        if path.exists():
            report.items.append(
                MigrationItem(
                    kind=name.rstrip("s"),
                    source=str(path),
                    status="excluded",
                    reason="raw Jarvis config and sessions are not imported",
                    provenance={"origin": "jarvis"},
                )
            )


def _skill_dirs(root: Path) -> list[Path]:
    skills = root / "skills"
    if not skills.is_dir():
        return []
    return sorted(item for item in skills.iterdir() if item.is_dir())


def _memory_export_paths(root: Path) -> list[Path]:
    candidates = [
        root / "memory_export.json",
        root / "memories.json",
        root / "memory" / "export.json",
    ]
    return [path for path in candidates if path.is_file()]


def _load_memory_export(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("memories", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_report(workspace: Path, report: MigrationReport, *, dry_run: bool) -> None:
    if dry_run:
        return
    path = workspace / "content" / "migrations" / "jarvis_migration_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def _write_provenance(path: Path, source: Path, root: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "origin": "jarvis",
                "source": str(source),
                "relative_path": str(source.relative_to(root)),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

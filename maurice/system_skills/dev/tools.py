"""Development worker orchestration tools."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from maurice.host.credentials import load_workspace_credentials
from maurice.host.runtime import _agent_system_prompt, _provider_and_model_for_config
from maurice.kernel.approvals import ApprovalStore
from maurice.kernel.config import load_workspace_config
from maurice.kernel.contracts import ToolResult
from maurice.kernel.events import EventStore
from maurice.kernel.loop import AgentLoop
from maurice.kernel.permissions import PermissionContext
from maurice.kernel.session import SessionStore
from maurice.kernel.skills import SkillContext, SkillHooks, SkillRegistry

MAX_WORKERS_PER_CALL = 5
MAX_ACTIVE_WORKERS = 10
DEFAULT_MAX_TOOL_ITERATIONS = 12
MAX_TOOL_ITERATIONS = 20
DEFAULT_MAX_SECONDS_PER_WORKER = 300
MAX_SECONDS_PER_WORKER = 900
MAX_FILE_CONTEXT_CHARS = 18_000
STALE_WORKER_SECONDS = 7200
WORKER_REPORT_STATUSES = {"completed", "blocked", "needs_arbitration", "obsolete"}


def build_executors(ctx: Any) -> dict[str, Any]:
    return {"dev.spawn_workers": lambda arguments: spawn_workers(arguments, ctx)}


def spawn_workers(arguments: dict[str, Any], ctx: SkillContext) -> ToolResult:
    raw_workers = arguments.get("workers")
    if not isinstance(raw_workers, list) or not raw_workers:
        return _error("invalid_arguments", "dev.spawn_workers requires a non-empty workers list.")
    max_workers = _bounded_int(arguments.get("max_workers"), len(raw_workers), 1, MAX_WORKERS_PER_CALL)
    workers = raw_workers[:max_workers]
    skipped_workers = max(0, len(raw_workers) - len(workers))

    permission_context = PermissionContext.model_validate(ctx.permission_context)
    variables = permission_context.variables()
    workspace = Path(variables["$workspace"]).expanduser().resolve()
    project = Path(variables["$project"]).expanduser().resolve()
    agent_workspace = Path(variables["$agent_workspace"]).expanduser().resolve()
    active_runs = _active_worker_count(agent_workspace)
    if active_runs + len(workers) > MAX_ACTIVE_WORKERS:
        return _error(
            "too_many_active_workers",
            f"Too many active dev workers ({active_runs}); max active workers is {MAX_ACTIVE_WORKERS}.",
        )

    try:
        bundle = load_workspace_config(workspace)
        parent_agent = bundle.agents.agents[ctx.agent_id]
    except Exception as exc:
        return _error("config_error", f"Could not load parent agent config: {exc}")

    worker_model_chain = list(parent_agent.worker_model_chain or parent_agent.model_chain or [])
    worker_agent = parent_agent.model_copy(update={"model_chain": worker_model_chain})
    credentials = load_workspace_credentials(workspace).visible_to(parent_agent.credentials)
    max_tool_iterations = _bounded_int(
        arguments.get("max_tool_iterations"),
        DEFAULT_MAX_TOOL_ITERATIONS,
        1,
        MAX_TOOL_ITERATIONS,
    )
    max_seconds_per_worker = _bounded_int(
        arguments.get("max_seconds_per_worker"),
        DEFAULT_MAX_SECONDS_PER_WORKER,
        30,
        MAX_SECONDS_PER_WORKER,
    )

    results = []
    started_at = time.monotonic()
    prepared = []
    for index, raw_worker in enumerate(workers, start=1):
        prepared.append(
            _prepare_worker_run(
                index=index,
                raw_worker=raw_worker,
                agent_workspace=agent_workspace,
            )
        )

    valid_runs = [item for item in prepared if item["status"] == "ready"]
    results.extend(item for item in prepared if item["status"] != "ready")
    if valid_runs:
        with ThreadPoolExecutor(max_workers=len(valid_runs)) as executor:
            future_map = {
                executor.submit(
                    _run_worker_with_status,
                    run=item,
                    ctx=ctx,
                    bundle=bundle,
                    parent_agent=parent_agent,
                    worker_agent=worker_agent,
                    credentials=credentials,
                    permission_context=permission_context,
                    workspace=workspace,
                    project=project,
                    max_tool_iterations=max_tool_iterations,
                    max_seconds_per_worker=max_seconds_per_worker,
                ): item
                for item in valid_runs
            }
            completed_by_index = {}
            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    completed_by_index[item["index"]] = future.result()
                except Exception as exc:
                    _write_status(item["run_root"], "failed", task=item["task"], error=str(exc))
                    completed_by_index[item["index"]] = {
                        "id": item["run_id"],
                        "index": item["index"],
                        "task": item["task"],
                        "status": "failed",
                        "error": str(exc),
                    }
            results.extend(completed_by_index[index] for index in sorted(completed_by_index))

    completed = sum(1 for item in results if item.get("status") == "completed")
    return ToolResult(
        ok=True,
        summary=f"{completed}/{len(results)} dev worker(s) completed.",
        data={
            "workers": results,
            "skipped_workers": skipped_workers,
            "worker_model_chain": worker_model_chain,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "limits": {
                "max_workers_per_call": MAX_WORKERS_PER_CALL,
                "max_active_workers": MAX_ACTIVE_WORKERS,
                "max_tool_iterations": max_tool_iterations,
                "max_seconds_per_worker": max_seconds_per_worker,
            },
        },
        trust="skill_generated",
        artifacts=[],
        events=[{"name": "dev.workers.spawned", "payload": {"count": len(results), "completed": completed}}],
        error=None,
    )


def _prepare_worker_run(
    *,
    index: int,
    raw_worker: Any,
    agent_workspace: Path,
) -> dict[str, Any]:
    if not isinstance(raw_worker, dict):
        return {"index": index, "status": "failed", "error": "worker must be an object"}
    task = str(raw_worker.get("task") or "").strip()
    if not task:
        return {"index": index, "status": "failed", "error": "worker task is required"}
    run_id = f"devw_{int(time.time() * 1000)}_{index}_{uuid4().hex[:8]}"
    run_root = agent_workspace / "runs" / "dev_workers" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    return {
        "index": index,
        "status": "ready",
        "run_id": run_id,
        "run_root": run_root,
        "raw_worker": raw_worker,
        "task": task,
    }


def _run_worker_with_status(
    *,
    run: dict[str, Any],
    ctx: SkillContext,
    bundle: Any,
    parent_agent: Any,
    worker_agent: Any,
    credentials: Any,
    permission_context: PermissionContext,
    workspace: Path,
    project: Path,
    max_tool_iterations: int,
    max_seconds_per_worker: int,
) -> dict[str, Any]:
    run_root = run["run_root"]
    task = run["task"]
    _write_status(run_root, "running", task=task)
    started_at = time.monotonic()
    try:
        result = _run_worker(
            run_id=run["run_id"],
            run_root=run_root,
            task=task,
            raw_worker=run["raw_worker"],
            ctx=ctx,
            bundle=bundle,
            parent_agent=parent_agent,
            worker_agent=worker_agent,
            credentials=credentials,
            permission_context=permission_context,
            workspace=workspace,
            project=project,
            max_tool_iterations=max_tool_iterations,
            max_seconds_per_worker=max_seconds_per_worker,
        )
        elapsed = time.monotonic() - started_at
        result["elapsed_seconds"] = round(elapsed, 3)
        result["over_time_budget"] = elapsed > max_seconds_per_worker
        _write_status(run_root, "completed", task=task)
        return result
    except Exception as exc:
        _write_status(run_root, "failed", task=task, error=str(exc))
        return {
            "id": run["run_id"],
            "index": run["index"],
            "task": task,
            "status": "failed",
            "error": str(exc),
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }


def _run_worker(
    *,
    run_id: str,
    run_root: Path,
    task: str,
    raw_worker: dict[str, Any],
    ctx: SkillContext,
    bundle: Any,
    parent_agent: Any,
    worker_agent: Any,
    credentials: Any,
    permission_context: PermissionContext,
    workspace: Path,
    project: Path,
    max_tool_iterations: int,
    max_seconds_per_worker: int,
) -> dict[str, Any]:
    prompt = _worker_prompt(task, raw_worker, project, max_seconds_per_worker=max_seconds_per_worker)
    provider, model_config = _provider_and_model_for_config(
        bundle,
        prompt,
        credentials,
        agent=worker_agent,
    )
    event_store = EventStore(run_root / "events.jsonl")
    registry = _worker_registry(ctx.registry)
    worker_ctx = SkillContext(
        permission_context=permission_context,
        registry=registry,
        event_store=event_store,
        all_skill_configs=ctx.all_skill_configs,
        skill_roots=ctx.skill_roots,
        enabled_skills=ctx.enabled_skills,
        agent_id=parent_agent.id,
        session_id=f"worker:{run_id}",
        hooks=SkillHooks(
            context_root=ctx.hooks.context_root,
            content_root=ctx.hooks.content_root,
            state_root=ctx.hooks.state_root,
            memory_path=ctx.hooks.memory_path,
            scope=ctx.hooks.scope,
            lifecycle="dev_worker",
            vision_backend=ctx.hooks.vision_backend,
            agents=ctx.hooks.agents,
        ),
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        session_store=SessionStore(run_root / "sessions"),
        event_store=event_store,
        permission_context=permission_context,
        permission_profile=parent_agent.permission_profile,
        tool_executors=_worker_executors(registry.build_executor_map(worker_ctx)),
        approval_store=ApprovalStore(run_root / "approvals.json", event_store=event_store),
        model=str(model_config.get("name") or "mock"),
        system_prompt=_agent_system_prompt(workspace, agent=parent_agent, active_project=project)
        + "\n\nYou are a bounded development worker. The parent agent is the orchestrator. "
        "Do not spawn other workers, do not revise the global plan, and do not continue around a blocker. "
        "Use tools only for this mission. If you hit a blocker, conflict, missing information, permission need, "
        "or plan inconsistency, stop and report it. Return only the structured report requested by the worker mission.",
    )
    cancel_event = threading.Event()
    timer = threading.Timer(max_seconds_per_worker, cancel_event.set)
    timer.daemon = True
    timer.start()
    try:
        turn = loop.run_turn(
            agent_id=parent_agent.id,
            session_id=f"worker:{run_id}",
            message=prompt,
            limits={
                "max_tool_iterations": max_tool_iterations,
                "allow_text_tool_calls": True,
                "max_seconds": max_seconds_per_worker,
            },
            message_metadata={"internal": True, "worker_id": run_id},
            cancel_event=cancel_event,
        )
    finally:
        timer.cancel()
    execution_status = "completed" if turn.status == "completed" else turn.status
    worker_status = _reported_worker_status(turn.assistant_text, execution_status=execution_status)
    return {
        "id": run_id,
        "task": task,
        "status": worker_status,
        "execution_status": execution_status,
        "worker_status": worker_status,
        "model": str(model_config.get("name") or "mock"),
        "summary": turn.assistant_text.strip(),
        "tool_activity": turn.tool_activity,
        "error": turn.error,
        "artifacts": [
            artifact.model_dump(mode="json")
            for result in turn.tool_results
            for artifact in result.artifacts
        ],
    }


def _worker_prompt(
    task: str,
    raw_worker: dict[str, Any],
    project: Path,
    *,
    max_seconds_per_worker: int,
) -> str:
    context_summary = str(raw_worker.get("context_summary") or "").strip()
    expected_output = str(raw_worker.get("expected_output") or "").strip()
    relevant_files = _string_list(raw_worker.get("relevant_files"))
    write_paths = _string_list(raw_worker.get("write_paths"))
    excerpts = _file_excerpts(project, relevant_files)
    return (
        "Mission worker de developpement.\n\n"
        f"Projet actif : {project}\n"
        f"Tache : {task}\n\n"
        f"Contexte minimal :\n{context_summary or '- Aucun contexte supplementaire fourni.'}\n\n"
        f"Fichiers pertinents : {', '.join(relevant_files) if relevant_files else '-'}\n"
        f"Chemins d'ecriture attendus : {', '.join(write_paths) if write_paths else '-'}\n"
        f"Sortie attendue : {expected_output or 'Resume concis, fichiers touches, verification, blocages.'}\n\n"
        "Contraintes :\n"
        "- Reste strictement sur cette mission.\n"
        "- L'agent parent est l'orchestrateur : ne change pas le plan global toi-meme.\n"
        "- Ne spawn pas d'autres workers.\n"
        "- Ne modifie pas de secrets ni de fichiers hors projet.\n"
        "- Si tu rencontres un conflit, un blocage, une information manquante, une permission requise, ou un impact probable sur d'autres workers, stoppe et remonte-le au parent.\n"
        "- Ne contourne pas un blocage en changeant de strategie majeure sans consigne du parent.\n"
        f"- Budget temps indicatif : {max_seconds_per_worker}s. Termine proprement des qu'une action longue est finie.\n"
        "- Si une autorisation est requise, stoppe et explique le blocage.\n\n"
        "Rapport obligatoire, sans prose autour :\n"
        "```text\n"
        "status: completed | blocked | needs_arbitration | obsolete\n"
        "summary: ...\n"
        "changed_files: path1, path2 | none\n"
        "verification: ... | not_run: reason\n"
        "blocker: ... | none\n"
        "impact_on_other_tasks: ... | none\n"
        "suggested_next_worker_task: ... | none\n"
        "```\n\n"
        f"Extraits fournis :\n{excerpts}"
    )


def _reported_worker_status(text: str, *, execution_status: str) -> str:
    if execution_status != "completed":
        return execution_status
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "status":
            status = value.strip().lower().replace("-", "_").replace(" ", "_")
            if status in WORKER_REPORT_STATUSES:
                return status
    lowered = text.lower()
    if any(marker in lowered for marker in ("blocked", "bloque", "permission", "autorisation requise")):
        return "blocked"
    return "completed"


def _worker_registry(registry: Any | None) -> SkillRegistry:
    if registry is None:
        return SkillRegistry(skills={}, tools={})
    return SkillRegistry(
        skills=registry.skills,
        tools={
            name: declaration
            for name, declaration in registry.tools.items()
            if name != "dev.spawn_workers"
        },
        commands=registry.commands,
    )


def _worker_executors(executors: dict[str, Any]) -> dict[str, Any]:
    return {
        name: executor
        for name, executor in executors.items()
        if name != "dev.spawn_workers" and not name.endswith(".dev.tools.spawn_workers")
    }


def _active_worker_count(agent_workspace: Path) -> int:
    root = agent_workspace / "runs" / "dev_workers"
    if not root.exists():
        return 0
    now = time.time()
    count = 0
    for status_path in root.glob("*/status.json"):
        try:
            import json

            data = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if now - status_path.stat().st_mtime > STALE_WORKER_SECONDS:
            continue
        if data.get("status") == "running":
            count += 1
    return count


def _write_status(run_root: Path, status: str, **extra: Any) -> None:
    import json

    payload = {"status": status, "updated_at": datetime_iso(), **extra}
    (run_root / "status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def datetime_iso() -> str:
    return datetime.now(UTC).isoformat()


def _file_excerpts(project: Path, paths: list[str]) -> str:
    if not paths:
        return "- Aucun extrait fourni.\n"
    remaining = MAX_FILE_CONTEXT_CHARS
    chunks = []
    for raw_path in paths:
        if remaining <= 0:
            break
        try:
            path = _resolve_project_path(project, raw_path)
        except ValueError:
            chunks.append(f"\n## {raw_path}\nPath refused: outside project.\n")
            continue
        if not path.is_file():
            chunks.append(f"\n## {raw_path}\nNot a file or missing.\n")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        excerpt = text[:remaining]
        remaining -= len(excerpt)
        suffix = "\n... truncated ...\n" if len(text) > len(excerpt) else "\n"
        chunks.append(f"\n## {raw_path}\n```text\n{excerpt}{suffix}```\n")
    return "".join(chunks) or "- Aucun extrait lisible.\n"


def _resolve_project_path(project: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project.resolve())
    except ValueError as exc:
        raise ValueError("outside project") from exc
    return resolved


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        data=None,
        trust="skill_generated",
        artifacts=[],
        events=[],
        error={"code": code, "message": message},
    )

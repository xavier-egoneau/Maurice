"""Automatic Docker Compose service management for skill dependencies.

When a loaded skill declares a `docker` section in its manifest, the host
ensures the service is running before the skill's tools are used.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import request
from urllib.error import URLError

if TYPE_CHECKING:
    from maurice.kernel.skills import SkillRegistry

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def ensure_skill_services(registry: "SkillRegistry") -> None:
    """Start any Docker services declared by loaded skills that aren't healthy yet."""
    for skill in registry.loaded().values():
        if skill.manifest and skill.manifest.docker:
            _ensure(skill.manifest.docker, skill.name)


def _ensure(docker_cfg: object, skill_name: str) -> None:
    health_url: str = getattr(docker_cfg, "health_url", "")
    service: str = getattr(docker_cfg, "service", "")
    compose_file: str = getattr(docker_cfg, "compose_file", "docker-compose.yml")
    startup_timeout: int = getattr(docker_cfg, "startup_timeout", 15)

    if not service:
        return
    if _is_healthy(health_url):
        return

    compose_path = _PROJECT_ROOT / compose_file
    if not compose_path.exists():
        return

    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d", service],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return

    if health_url:
        _wait_healthy(health_url, startup_timeout)


def _is_healthy(url: str) -> bool:
    if not url:
        return False
    try:
        with request.urlopen(url, timeout=2) as r:
            return r.status < 400
    except (URLError, OSError, Exception):
        return False


def _wait_healthy(url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_healthy(url):
            return
        time.sleep(1)

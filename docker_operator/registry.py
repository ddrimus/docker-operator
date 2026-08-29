# Logs in to a private registry once at startup so `docker compose pull` can authenticate
from __future__ import annotations
import logging
import subprocess

from .config import Settings

log = logging.getLogger("docker_operator.registry")


# Best-effort `docker login`; no-op if REGISTRY_HOST is unset. Password goes over stdin, never as an
# argv/env value a process listing could catch, and is never logged. Failure isn't fatal: stacks
# pulling public images are unaffected, and a private-image pull failure is already reported via
# reconcile's own retry/notify path
def login(settings: Settings) -> None:
    if not settings.registry_host:
        return
    try:
        proc = subprocess.run(
            ["docker", "login", settings.registry_host, "-u", settings.registry_username, "--password-stdin"],
            input=settings.registry_password, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        log.error("docker login to %s timed out", settings.registry_host)
        return
    if proc.returncode != 0:
        log.error("docker login to %s failed: %s", settings.registry_host, proc.stderr.strip())
        return
    log.info("docker login to %s succeeded", settings.registry_host)

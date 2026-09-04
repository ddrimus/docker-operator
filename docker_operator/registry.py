# Logs in to a private registry once at startup so `docker compose pull` can authenticate
from __future__ import annotations
import logging
import subprocess

from .config import Settings

log = logging.getLogger("docker_operator.registry")


# Best-effort `docker login`; no-op if REGISTRY_HOST unset, password only via stdin (never logged), failure isn't fatal since pull failures are already reported via reconcile's retry path
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

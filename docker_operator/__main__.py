# CLI entry point: runs a single reconcile pass, validates stacks, or starts the webhook server
from __future__ import annotations
import argparse
import logging
import os
import sys
import urllib.request
from logging.handlers import RotatingFileHandler

from .config import load_settings
from .notify import flush as flush_notifications
from .reconcile import reconcile, validate
from .registry import login as registry_login
from .server import run
from .util import chown_recursive


# Container HEALTHCHECK probe; skips load_settings() so an unrelated config error doesn't make Docker think the server is down
def _healthcheck() -> None:
    port = os.environ.get("LISTEN_PORT", "8080")
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
    except Exception:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="docker-operator")
    parser.add_argument("--once", action="store_true",
                         help="run a single reconcile pass and exit (no HTTP server)")
    parser.add_argument("--force", metavar="STACK", action="append", default=[],
                         help="redeploy STACK even if unchanged, e.g. after a manual "
                              "`docker compose down -v --rmi all` (repeatable; pass 'all' "
                              "to force every stack). Requires --once.")
    parser.add_argument("--validate", action="store_true",
                         help="validate every stack's compose config and exit, without deploying (for CI use)")
    parser.add_argument("--healthcheck", action="store_true",
                         help="internal: used by the container HEALTHCHECK")
    args = parser.parse_args()

    if args.healthcheck:
        _healthcheck()
        return

    if args.force and not args.once:
        parser.error("--force requires --once (it forces a single manual reconcile pass)")

    settings = load_settings()
    # mkdir's mode= only applies on creation, so chmod explicitly in case Docker already created these as looser bind-mount points
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.data_dir.chmod(0o700)
    settings.deploy_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.deploy_dir.chmod(0o700)
    settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.log_dir.chmod(0o700)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        # 10MB x 5 backups on top of stdout; `docker compose logs -f` keeps working either way
        handlers=[logging.StreamHandler(sys.stdout),
                  RotatingFileHandler(settings.log_dir / "docker-operator.log",
                                       maxBytes=10 * 1024 * 1024, backupCount=5)],
    )

    if args.validate:
        sys.exit(0 if validate(settings) else 1)

    registry_login(settings)
    # DEPLOY_UID/DEPLOY_GID let a host user own DEPLOY_DIR outright, so it's `cd`/`docker compose`-able as themselves; unset leaves it root-owned
    chown_recursive(settings.deploy_dir, settings.deploy_uid, settings.deploy_gid)

    if args.once:
        reconcile(settings, force=set(args.force) or None)
        # Notifications are sent off a background queue so they never block reconcile itself (see notify.flush), but this process exits right after and daemon threads die outright on interpreter exit, so anything still queued needs an explicit wait here or it would simply never go out; a one-shot run has no shutdown-grace-period deadline forcing a short wait, so it can afford longer than flush()'s own conservative default
        flush_notifications(timeout=30)
    else:
        run(settings)


if __name__ == "__main__":
    main()

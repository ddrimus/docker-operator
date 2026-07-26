# CLI entry point: runs a single reconcile pass or starts the webhook server
from __future__ import annotations
import argparse
import logging
import os
import sys
import urllib.request

from .config import load_settings
from .reconcile import reconcile
from .server import run


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
    parser.add_argument("--healthcheck", action="store_true",
                         help="internal: used by the container HEALTHCHECK")
    args = parser.parse_args()

    if args.healthcheck:
        _healthcheck()
        return

    if args.force and not args.once:
        parser.error("--force requires --once (it forces a single manual reconcile pass)")

    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # mkdir's mode= only applies on creation, so chmod explicitly in case Docker already created these as looser bind-mount points
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.data_dir.chmod(0o700)
    settings.deploy_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.deploy_dir.chmod(0o700)

    if args.once:
        reconcile(settings, force=set(args.force) or None)
    else:
        run(settings)


if __name__ == "__main__":
    main()

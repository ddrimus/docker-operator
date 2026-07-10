# CLI entry point: runs a single reconcile pass
from __future__ import annotations
import argparse
import logging
import sys

from .config import load_settings
from .reconcile import reconcile


def main() -> None:
    parser = argparse.ArgumentParser(prog="docker-operator")
    parser.add_argument("--force", metavar="STACK", action="append", default=[],
                         help="redeploy STACK even if unchanged (repeatable; pass 'all' to force every stack)")
    args = parser.parse_args()

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

    reconcile(settings, force=set(args.force) or None)


if __name__ == "__main__":
    main()

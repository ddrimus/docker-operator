# Reads and atomically writes the operator's persisted stack state
from __future__ import annotations
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("docker_operator.state")


def load(state_file: Path) -> dict:
    if not state_file.is_file():
        return {"stacks": {}}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("failed to read state file %s: %s, starting fresh", state_file, exc)
        return {"stacks": {}}


# Atomically write state to disk; not internally locked since every caller already holds reconcile.py's cross-process flock
def save(state_file: Path, data: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=state_file.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, state_file)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

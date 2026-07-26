from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


# Proves the actual subprocess-level shutdown behavior end to end, since Python installs no default SIGTERM handler for `docker stop`
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_triggers_clean_exit_with_log_message(git_repo: Path, tmp_path: Path, sig):
    env = os.environ.copy()
    env.update(
        WEBHOOK_SECRET="x", GIT_REPO_URL=str(git_repo), GIT_BRANCH="main",
        DATA_DIR=str(tmp_path / "data"), DEPLOY_DIR=str(tmp_path / "deploy"),
        LISTEN_PORT="0", POLL_INTERVAL_SECONDS="0",
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    p = subprocess.Popen([sys.executable, "-m", "docker_operator"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=repo_root)
    try:
        time.sleep(1.0)
        assert p.poll() is None, "server exited before the signal was even sent"

        p.send_signal(sig)
        p.wait(timeout=5)

        assert p.returncode == 0, f"expected a clean exit (0), got {p.returncode}"
        out = p.stdout.read()
        assert "shutdown complete" in out
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


# No test for SIGKILL (uncatchable by definition): an unclean kill is made safe by state.py's atomic rename plus reconcile.py's stage-then-promote design

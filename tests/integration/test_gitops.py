from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

from docker_operator.gitops import sync_repo
from conftest import add_stack


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                           text=True, check=True).stdout.strip()


def test_first_call_clones(git_repo: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    head, synced = sync_repo(str(git_repo), "main", dest)
    assert synced is True
    assert head == _head(git_repo)
    assert (dest / ".git").is_dir()
    assert (dest / "compose").is_dir()


def test_local_path_clone_works_without_file_scheme(git_repo: Path, tmp_path: Path):
    # The exact case the "bind-mount Forgejo's bare repo" design depends on: a plain filesystem path, no file:// needed
    dest = tmp_path / "dest"
    head, synced = sync_repo(str(git_repo), "main", dest)
    assert synced is True
    assert head


def test_second_call_fetches_new_commit_instead_of_recloning(git_repo: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    head1, _ = sync_repo(str(git_repo), "main", dest)

    add_stack(git_repo, "newstack")
    head2, synced = sync_repo(str(git_repo), "main", dest)

    assert synced is True
    assert head2 != head1
    assert (dest / "compose" / "newstack").is_dir()


def test_hard_reset_discards_local_modifications(git_repo: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    sync_repo(str(git_repo), "main", dest)

    # Simulate garbage written into the working copy between syncs: `reset --hard` must never trust disk over origin
    (dest / "compose" / "rogue-file.txt").write_text("should not survive")

    add_stack(git_repo, "another")
    sync_repo(str(git_repo), "main", dest)

    assert not (dest / "compose" / "rogue-file.txt").exists()
    assert (dest / "compose" / "another").is_dir()


def test_unreachable_remote_falls_back_to_last_known_good(git_repo: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    head1, synced1 = sync_repo(str(git_repo), "main", dest)
    assert synced1 is True

    moved_away = git_repo.parent / "moved-away"
    git_repo.rename(moved_away)
    try:
        head2, synced2 = sync_repo(str(git_repo), "main", dest)
        assert synced2 is False
        # Kept exactly what was there, didn't touch anything
        assert head2 == head1
    finally:
        # Restore for any later fixture teardown
        moved_away.rename(git_repo)


def test_first_clone_failure_raises_when_no_previous_checkout(tmp_path: Path):
    dest = tmp_path / "dest"
    with pytest.raises(subprocess.CalledProcessError):
        sync_repo(str(tmp_path / "no-such-repo"), "main", dest)


def test_wrong_branch_name_raises_on_first_clone(git_repo: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    with pytest.raises(subprocess.CalledProcessError):
        sync_repo(str(git_repo), "does-not-exist-branch", dest)

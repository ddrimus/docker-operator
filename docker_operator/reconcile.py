# Syncs the repo and brings deployed stacks in line with what's tracked in the compose root
from __future__ import annotations
import fcntl
import logging
import os
import shutil
from contextlib import contextmanager

from . import compose, secrets, state as state_mod
from .config import Settings
from .gitops import sync_repo
from .networks import parse_networks, topo_order
from .notify import notify
from .stacks import Stack, discover_stacks, stack_hash
from .util import chown_recursive, exc_detail

log = logging.getLogger("docker_operator.reconcile")


# Cross-process advisory lock so a manual `--once` run can't race the server's background worker thread
@contextmanager
def _reconcile_lock(settings: Settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.data_dir / ".reconcile.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _deploy_path(settings: Settings, name: str):
    return settings.deploy_dir / name


# Render and validate a stack into staged *.new files without touching the live compose.yaml/.env
def _stage(settings: Settings, stk: Stack, deploy_path) -> tuple:
    deploy_path.mkdir(parents=True, exist_ok=True)
    deploy_path.chmod(0o700)
    staged_env = deploy_path / ".env.new"
    staged_compose = deploy_path / "compose.yaml.new"
    try:
        secrets.render_env_file(stk, settings.sops_age_key_file, staged_env)
        staged_compose.write_bytes(stk.compose_file.read_bytes())
        config_json = compose.resolve_config(staged_compose, staged_env, stk.name, deploy_path,
                                              settings.deploy_timeout_seconds)
    except Exception:
        staged_env.unlink(missing_ok=True)
        staged_compose.unlink(missing_ok=True)
        raise
    owned, external = parse_networks(config_json)
    # Ownership transfers through the later staged_*.replace(final_*) rename, so this is the only chown needed
    chown_recursive(deploy_path, settings.deploy_uid, settings.deploy_gid)
    return staged_compose, staged_env, owned, external


# Apply the staged config via `docker compose up`, promoting it to the canonical files only once `up` succeeds
def _promote_and_up(settings: Settings, name: str, deploy_path, staged_compose, staged_env) -> None:
    # If `up` fails, leave the canonical files and state hash untouched so the next reconcile retries cleanly from the same input
    try:
        compose.up(staged_compose, staged_env, name, deploy_path,
                   pull=settings.pull_images, timeout=settings.deploy_timeout_seconds)
    except Exception:
        staged_compose.unlink(missing_ok=True)
        staged_env.unlink(missing_ok=True)
        raise

    final_compose = deploy_path / "compose.yaml"
    final_env = deploy_path / ".env"
    staged_compose.replace(final_compose)
    staged_env.replace(final_env)
    # Belt-and-suspenders; render_env_file already writes 0600
    final_env.chmod(0o600)


def reconcile(settings: Settings, force: set[str] | None = None) -> None:
    with _reconcile_lock(settings):
        _reconcile_locked(settings, force)


def _reconcile_locked(settings: Settings, force: set[str] | None) -> None:
    try:
        head, synced = sync_repo(settings.git_repo_url, settings.git_branch, settings.repo_dir)
    except Exception as exc:
        detail = exc_detail(exc)
        log.error("cannot reach %s and no previous checkout exists yet: %s", settings.git_repo_url, detail)
        notify(settings.notify_webhook_url,
               f"docker-operator: cannot reach {settings.git_repo_url} and no cached checkout exists: {detail}")
        return
    if not synced:
        log.info("forgejo unreachable, reconciling against last known-good checkout (%s)", head[:12])

    st = state_mod.load(settings.state_file)
    known: dict = st.setdefault("stacks", {})
    if force:
        force_all = "all" in force
        for name in list(known):
            if force_all or name in force:
                known.pop(name, None)
        log.warning("forced redeploy of all stacks (state cleared)" if force_all
                    else f"forced redeploy of: {', '.join(sorted(force))}")
    current = discover_stacks(settings.compose_root)

    changed = [(stk, h) for name, stk in current.items()
               if (h := stack_hash(stk)) != known.get(name, {}).get("hash")]
    removed = [name for name in known if name not in current]

    if not changed and not removed:
        log.info("reconcile: no changes (%d stacks up to date)", len(current))
        return
    log.info("reconcile: %d changed, %d removed", len(changed), len(removed))

    # Phase A: stage + validate every changed stack, discover network roles
    staged: dict[str, tuple] = {}
    owners: dict[str, str] = {}
    needs: dict[str, set[str]] = {}
    for stk, new_hash in changed:
        deploy_path = _deploy_path(settings, stk.name)
        try:
            staged_compose, staged_env, owned, external = _stage(settings, stk, deploy_path)
        except Exception as exc:
            detail = exc_detail(exc)
            log.error("stack '%s' failed validation: %s", stk.name, detail)
            notify(settings.notify_webhook_url, f"docker-operator: `{stk.name}` failed to validate: {detail}")
            continue
        for n in owned:
            owners.setdefault(n, stk.name)
        needs[stk.name] = external
        staged[stk.name] = (stk, staged_compose, staged_env, new_hash)

    # Phase B: order deploys so a stack that owns a network goes before any other changed stack that depends on it as external
    depends_on = {
        name: {owners[n] for n in ext if n in owners and owners[n] != name}
        for name, ext in needs.items()
    }
    order = topo_order(list(staged.keys()), depends_on)

    # Phase C: apply each staged stack and promote it only once `up` succeeds
    for name in order:
        stk, staged_compose, staged_env, new_hash = staged[name]
        deploy_path = _deploy_path(settings, name)
        try:
            log.info("deploying stack '%s'", name)
            _promote_and_up(settings, name, deploy_path, staged_compose, staged_env)
            known[name] = {"hash": new_hash}
            state_mod.save(settings.state_file, st)
            log.info("stack '%s' deployed OK -> %s", name, deploy_path)
        except Exception as exc:
            detail = exc_detail(exc)
            log.error("stack '%s' failed to deploy: %s", name, detail)
            notify(settings.notify_webhook_url, f"docker-operator: `{name}` failed to deploy: {detail}")

    if removed:
        if settings.prune_removed_stacks:
            for name in removed:
                deploy_path = _deploy_path(settings, name)
                compose_file = deploy_path / "compose.yaml"
                env_file = deploy_path / ".env"
                if not compose_file.is_file():
                    # Nothing on disk to tear down; stop tracking it so this doesn't get re-logged on every future reconcile
                    log.warning("stack '%s' removed from repo, nothing on disk to tear down, dropping from state",
                                name)
                    known.pop(name, None)
                    state_mod.save(settings.state_file, st)
                    continue
                try:
                    log.warning("stack '%s' removed from repo, tearing down", name)
                    compose.down(compose_file, env_file, name, deploy_path, settings.deploy_timeout_seconds)
                    del known[name]
                    state_mod.save(settings.state_file, st)
                    shutil.rmtree(deploy_path, ignore_errors=True)
                except Exception as exc:
                    detail = exc_detail(exc)
                    log.error("failed to tear down '%s': %s", name, detail)
                    notify(settings.notify_webhook_url, f"docker-operator: failed to tear down `{name}`: {detail}")
        else:
            log.warning("stack(s) removed from repo, PRUNE_REMOVED_STACKS=false, left on disk: %s", removed)

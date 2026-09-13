# Syncs the repo and brings deployed stacks in line with what's tracked in the compose root, or just validates it
from __future__ import annotations
import fcntl
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import compose, secrets, state as state_mod
from .config import Settings
from .gitops import sync_repo
from .networks import parse_networks, priority_sorted, topo_order
from .notify import COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING, notify
from .stacks import Stack, discover_stacks, stack_hash
from .util import chown_recursive, exc_detail

log = logging.getLogger("docker_operator.reconcile")

# Outcome lines accumulate as (status, line) pairs through a reconcile pass; status is "ok", "warn", or "error"
Outcome = tuple[str, str]

# Keeps a many-stacks-at-once recap comfortably under Discord's embed description cap, cut at a line boundary rather than mid-word
_MAX_RECAP_LINES = 15


# True if this attempt at new_hash should run now: never attempted, past the backoff delay, or a different hash than what was last failing (a real change resets it)
def _should_attempt(retries: dict, name: str, new_hash: str, max_retries: int, retry_delay_seconds: int) -> bool:
    retry = retries.get(name)
    if retry is None or retry["hash"] != new_hash:
        return True
    if retry["attempts"] >= max_retries:
        return False
    return (time.time() - retry["last_attempt"]) >= retry_delay_seconds


# Record a failed attempt at new_hash, persisted immediately, kept separate from known/st["stacks"] so "no entry" there keeps meaning "never successfully deployed"
def _record_failure(settings: Settings, st: dict, retries: dict, name: str, new_hash: str) -> int:
    retry = retries.get(name)
    attempts = retry["attempts"] + 1 if retry and retry["hash"] == new_hash else 1
    retries[name] = {"hash": new_hash, "attempts": attempts, "last_attempt": time.time()}
    state_mod.save(settings.state_file, st)
    return attempts


# Record a stack as successfully deployed at new_hash, clearing any retry history, and persist immediately
def _remember_stack(settings: Settings, st: dict, known: dict, retries: dict, name: str, new_hash: str) -> None:
    known[name] = {"hash": new_hash}
    retries.pop(name, None)
    state_mod.save(settings.state_file, st)


# Drop a stack from tracked/retry state entirely (nothing left to reconcile against), and persist immediately
def _forget_stack(settings: Settings, st: dict, known: dict, retries: dict, name: str) -> None:
    known.pop(name, None)
    retries.pop(name, None)
    state_mod.save(settings.state_file, st)


# US-style timestamp used across every notification, so they read consistently regardless of what triggered them
def _timestamp() -> str:
    return datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")


# Sync the repo, logging (and optionally notifying) on total failure; returns False if the caller should bail
def _sync_or_bail(settings: Settings, action: str, notify_on_failure: bool) -> bool:
    try:
        head, synced = sync_repo(settings.git_repo_url, settings.git_branch, settings.repo_dir)
    except Exception as exc:
        detail = exc_detail(exc)
        log.error("cannot reach %s and no previous checkout exists yet: %s", settings.git_repo_url, detail)
        if notify_on_failure:
            notify(settings.notify_webhook_url, "🚀 Docker Operator — Git Unreachable",
                   f"Cannot reach the repository and no cached checkout exists to fall back on.\n\n"
                   f"**Extra Informations**\n"
                   f"🔗 - Repo: `{settings.git_repo_url}`\n"
                   f"❗ - Error: `{detail}`\n"
                   f"🕒 - Time: `{_timestamp()}`",
                   COLOR_ERROR)
        return False
    if not synced:
        log.info("forgejo unreachable, %s against last known-good checkout (%s)", action, head[:12])
    return True


# Record a validate/deploy failure for one stack: log it, and add a recap line. Final give-ups read as errors, everything still within budget reads as a warning
def _fail_stack(settings: Settings, st: dict, retries: dict, name: str, new_hash: str, stage: str, exc: Exception, outcomes: list[Outcome]) -> None:
    detail = exc_detail(exc)
    attempts = _record_failure(settings, st, retries, name, new_hash)
    gave_up = attempts >= settings.deploy_max_retries
    if gave_up:
        log.error("stack '%s' failed to %s: %s (attempt %d/%d, giving up until it changes)",
                   name, stage, detail, attempts, settings.deploy_max_retries)
        outcomes.append(("error", f"❌ **{name}** failed to {stage} after {attempts} attempt(s), giving up until it changes:\n> `{detail}`"))
    else:
        log.error("stack '%s' failed to %s: %s (attempt %d/%d)", name, stage, detail, attempts, settings.deploy_max_retries)
        outcomes.append(("warn", f"⚠️ **{name}** failed to {stage} (attempt {attempts}/{settings.deploy_max_retries}):\n> `{detail}`"))


# Build and send one end-of-pass recap from every outcome line collected, skipped entirely if nothing happened
def _send_recap(settings: Settings, outcomes: list[Outcome], elapsed: float) -> None:
    if not outcomes:
        return
    statuses = {status for status, _ in outcomes}
    color = COLOR_ERROR if "error" in statuses else COLOR_WARNING if "warn" in statuses else COLOR_SUCCESS
    title = ("🚀 Docker Operator — Reconcile Complete" if color == COLOR_SUCCESS
             else "🚀 Docker Operator — Reconcile Finished with Errors")

    counts = {status: sum(1 for s, _ in outcomes if s == status) for status in ("ok", "warn", "error")}
    result = ", ".join(f"{n} {label}" for label, n in
                        (("succeeded", counts["ok"]), ("warning(s)", counts["warn"]), ("error(s)", counts["error"])) if n)

    # Failures sorted first so a truncated recap never hides the one thing that actually needs attention
    rank = {"error": 0, "warn": 1, "ok": 2}
    ordered = sorted(outcomes, key=lambda o: rank[o[0]])
    shown = [line for _, line in ordered[:_MAX_RECAP_LINES]]
    if len(ordered) > _MAX_RECAP_LINES:
        shown.append(f"… and {len(ordered) - _MAX_RECAP_LINES} more, see logs for the full list")
    body = "\n\n".join(shown)
    description = (f"{body}\n\n"
                    f"**Extra Informations**\n"
                    f"📦 - Result: `{result}`\n"
                    f"⏱️ - Duration: `{elapsed:.1f}s`\n"
                    f"🕒 - Time: `{_timestamp()}`")
    notify(settings.notify_webhook_url, title, description, color)


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
    start = time.time()
    if not _sync_or_bail(settings, "reconciling", notify_on_failure=True):
        return

    st = state_mod.load(settings.state_file)
    known: dict = st.setdefault("stacks", {})
    retries: dict = st.setdefault("retries", {})
    force_all = bool(force) and "all" in force
    if force:
        cleared = [name for name in list(known) + list(retries) if force_all or name in force]
        for name in cleared:
            known.pop(name, None)
            retries.pop(name, None)
        if cleared:
            # Persisted now: a force on a stack with no other changes this pass would otherwise never reach disk
            state_mod.save(settings.state_file, st)
        log.warning("forced redeploy of all stacks (state cleared)" if force_all
                    else f"forced redeploy of: {', '.join(sorted(force))}")
    current = discover_stacks(settings.compose_root)

    # A stack forced by name still deploys even while paused: an explicit --force is a stronger signal than a possibly-stale marker
    paused = {name for name, stk in current.items()
              if stk.paused and not (force_all or (force and name in force))}
    candidates = [(stk, h) for name, stk in current.items()
                  if name not in paused and (h := stack_hash(stk)) != known.get(name, {}).get("hash")]
    changed = [(stk, h) for stk, h in candidates
               if _should_attempt(retries, stk.name, h, settings.deploy_max_retries,
                                   settings.deploy_retry_delay_seconds)]
    deferred = len(candidates) - len(changed)
    removed = [name for name in known if name not in current]

    if not changed and not removed:
        suffix = f", {deferred} deferred by retry backoff/limit" if deferred else ""
        suffix += f", {len(paused)} paused" if paused else ""
        log.info("reconcile: no changes (%d stacks up to date%s) in %.1fs", len(current), suffix, time.time() - start)
        return
    changed_names = f" [{', '.join(sorted(stk.name for stk, _ in changed))}]" if changed else ""
    removed_names = f" [{', '.join(sorted(removed))}]" if removed else ""
    log.info("reconcile: %d changed%s, %d removed%s%s%s",
              len(changed), changed_names, len(removed), removed_names,
              f", {deferred} deferred" if deferred else "",
              f", {len(paused)} paused" if paused else "")

    # Every notable per-stack outcome this pass, folded into one recap notification at the very end instead of one per event
    outcomes: list[Outcome] = []

    # Phase A: stage + validate every changed stack, discover network roles
    staged: dict[str, tuple] = {}
    owners: dict[str, str] = {}
    needs: dict[str, set[str]] = {}
    for stk, new_hash in changed:
        deploy_path = _deploy_path(settings, stk.name)
        try:
            staged_compose, staged_env, owned, external = _stage(settings, stk, deploy_path)
        except Exception as exc:
            _fail_stack(settings, st, retries, stk.name, new_hash, "validate", exc, outcomes)
            continue
        for n in owned:
            owners.setdefault(n, stk.name)
        needs[stk.name] = external
        staged[stk.name] = (stk, staged_compose, staged_env, new_hash)

    # Phase B: order deploys so a stack that owns a network, or explicitly lists another via .depends_on, goes after it
    depends_on = {
        name: {owners[n] for n in ext if n in owners and owners[n] != name} | staged[name][0].depends_on
        for name, ext in needs.items()
    }
    order = topo_order(priority_sorted(list(staged.keys()), settings.deploy_priority), depends_on)

    # Phase C: apply each staged stack and promote it only once `up` succeeds
    for name in order:
        stk, staged_compose, staged_env, new_hash = staged[name]
        deploy_path = _deploy_path(settings, name)
        was_failing = name in retries
        stack_start = time.time()
        try:
            log.info("deploying stack '%s'", name)
            _promote_and_up(settings, name, deploy_path, staged_compose, staged_env)
            _remember_stack(settings, st, known, retries, name, new_hash)
            log.info("stack '%s' deployed OK in %.1fs -> %s", name, time.time() - stack_start, deploy_path)
            outcomes.append(("ok", f"🔁 **{name}** recovered and deployed OK" if was_failing
                              else f"✅ **{name}** deployed OK"))
        except Exception as exc:
            _fail_stack(settings, st, retries, name, new_hash, "deploy", exc, outcomes)

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
                    _forget_stack(settings, st, known, retries, name)
                    outcomes.append(("ok", f"🗑️ **{name}** was already gone on disk, dropped from tracking"))
                    continue
                try:
                    log.warning("stack '%s' removed from repo, tearing down", name)
                    compose.down(compose_file, env_file, name, deploy_path, settings.deploy_timeout_seconds)
                    _forget_stack(settings, st, known, retries, name)
                    shutil.rmtree(deploy_path, ignore_errors=True)
                    outcomes.append(("ok", f"🗑️ **{name}** removed from repo, torn down"))
                except Exception as exc:
                    detail = exc_detail(exc)
                    log.error("failed to tear down '%s': %s", name, detail)
                    outcomes.append(("error", f"❌ **{name}** removed from repo but failed to tear down:\n> `{detail}`"))
        else:
            # Log-only, deliberately: nothing here is ever cleared from `known`, so unlike everything else in outcomes this would re-notify every single pass forever, not just once
            log.warning("stack(s) removed from repo, PRUNE_REMOVED_STACKS=false, left on disk: %s", removed)

    elapsed = time.time() - start
    _send_recap(settings, outcomes, elapsed)
    log.info("reconcile finished in %.1fs", elapsed)


# Render and validate one stack's compose config in an isolated scratch dir, touching nothing persistent
def _validate_stack(settings: Settings, stk: Stack) -> str | None:
    scratch = Path(tempfile.mkdtemp(prefix=f".validate-{stk.name}-"))
    try:
        env_file = scratch / ".env"
        secrets.render_env_file(stk, settings.sops_age_key_file, env_file)
        compose.resolve_config(stk.compose_file, env_file, stk.name, scratch, settings.deploy_timeout_seconds)
        return None
    except Exception as exc:
        return exc_detail(exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# Sync the repo and validate every discovered stack's compose config without deploying anything, for CI use ahead of a real reconcile
def validate(settings: Settings) -> bool:
    start = time.time()
    with _reconcile_lock(settings):
        if not _sync_or_bail(settings, "validating", notify_on_failure=False):
            return False

        current = discover_stacks(settings.compose_root)
        ok = True
        for name, stk in sorted(current.items()):
            detail = _validate_stack(settings, stk)
            if detail is None:
                log.info("stack '%s' is valid", name)
            else:
                log.error("stack '%s' failed validation: %s", name, detail)
                ok = False
        log.info("validate: %d stack(s) checked in %.1fs", len(current), time.time() - start)
        return ok

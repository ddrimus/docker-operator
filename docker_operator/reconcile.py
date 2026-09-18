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

from . import compose, registry, secrets, state as state_mod
from .config import Settings
from .gitops import sync_repo
from .networks import parse_networks, priority_sorted, topo_order
from .notify import COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING, notify
from .stacks import Stack, discover_stacks, stack_hash
from .util import chown_recursive, exc_detail

log = logging.getLogger("docker_operator.reconcile")

# Outcomes accumulate as (status, block, total_containers, started_containers) through a pass: counts are 0/0 when there's no container yet (validate failure, removal)
Outcome = tuple[str, str, int, int]

# Keeps a many-stacks-at-once bucket notification comfortably under Discord's embed description cap
_MAX_RECAP_STACKS = 15

# `ps` is a quick read-only listing, not a real deploy; cap its wait well below deploy_timeout_seconds so a stuck daemon can't double the hang time
_STATUS_TIMEOUT_SECONDS = 15

# Named once here instead of scattered as raw glyphs through the f-strings below; one place to change an icon, and a name search actually finds every use
_ICON_APP = "🐙"       # every notification title, regardless of status; the embed color is what actually signals status
_ICON_RESULT = "📦"
_ICON_DURATION = "⏳"
_ICON_TIME = "🕒"
_ICON_REPO = "🔗"      # git-unreachable's own alert only

# Title/color for each bucket's own standalone notification
_BUCKET_STYLE = {
    "error": (f"{_ICON_APP} Docker Operator - Failed", COLOR_ERROR),
    "warn": (f"{_ICON_APP} Docker Operator - Retrying", COLOR_WARNING),
    "ok": (f"{_ICON_APP} Docker Operator - Success", COLOR_SUCCESS),
}


# The bucket's Result line: containers started vs total, plus what's wrong with the rest; falls back to a stack count when total is 0 (nothing in the bucket ever reached a container)
def _result_summary(status: str, started: int, total: int, stack_count: int) -> str:
    if total == 0:
        label = {"ok": "succeeded", "warn": "warning(s)", "error": "hard error(s)"}[status]
        return f"{stack_count} {label}"
    if status == "ok":
        return f"{started}/{total} containers started"
    return f"{started}/{total} containers started, {total - started} {'retrying' if status == 'warn' else 'failed'}"


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


# True if a `docker compose ps` row is a healthy container: running, or a one-shot job (init/migration, restart: "no") that exited 0 on purpose
def _row_ok(row: dict | None) -> bool:
    if not row:
        return False
    state = (row.get("State") or "").lower()
    return state == "running" or (state == "exited" and row.get("ExitCode") == 0)


# A ```diff fenced block, one line per service: "+ name" when running, "- name (note)" for every other service in `failed`
def _diff_block(services: list[str], failed: dict[str, str]) -> str:
    lines = [f"- {svc} ({failed[svc]})" if svc in failed else f"+ {svc}" for svc in services]
    return "```diff\n" + "\n".join(lines) + "\n```"


# Sync the repo, logging (and optionally notifying) on total failure; returns False if the caller should bail
def _sync_or_bail(settings: Settings, action: str, notify_on_failure: bool) -> bool:
    try:
        head, synced = sync_repo(settings.git_repo_url, settings.git_branch, settings.repo_dir)
    except Exception as exc:
        detail = exc_detail(exc)
        log.error("cannot reach %s and no previous checkout exists yet: %s", settings.git_repo_url, detail)
        if notify_on_failure:
            notify(settings.notify_webhook_url, f"{_ICON_APP} Docker Operator - Git Unreachable",
                   f"An alert for **the repository** has failed to sync, and no cached checkout exists to fall back on:\n"
                   f"> `{detail}`\n\n"
                   f"**Extra Informations**\n"
                   f"{_ICON_REPO} - Repo: `{settings.git_repo_url}`\n"
                   f"{_ICON_TIME} - Time: `{_timestamp()}`",
                   COLOR_ERROR)
        return False
    if not synced:
        log.info("forgejo unreachable, %s against last known-good checkout (%s)", action, head[:12])
    return True


# Record a validate/deploy failure for one stack. Final give-ups read as errors, still-within-budget reads as a warning, and no `services` (never reached a container) falls back to plain-detail wording
def _fail_stack(settings: Settings, st: dict, retries: dict, name: str, new_hash: str, stage: str, exc: Exception,
                 outcomes: list[Outcome], services: list[str] | None = None) -> None:
    detail = exc_detail(exc)
    attempts = _record_failure(settings, st, retries, name, new_hash)
    gave_up = attempts >= settings.deploy_max_retries
    log.error("stack '%s' failed to %s: %s (attempt %d/%d%s)", name, stage, detail, attempts,
              settings.deploy_max_retries, ", giving up until it changes" if gave_up else "")

    if not services:
        if gave_up:
            outcomes.append(("error", f"Stack **{name}** failed to {stage} after {attempts} attempt(s), "
                                       f"giving up until it changes:\n> `{detail}`", 0, 0))
        else:
            outcomes.append(("warn", f"Stack **{name}** failed to {stage} "
                                      f"(attempt {attempts}/{settings.deploy_max_retries}):\n> `{detail}`", 0, 0))
        return

    rows = getattr(exc, "containers", None) or {}
    if gave_up:
        note, intro, status = "hard error", f"Stack **{name}** failed to start and the deploy was aborted.", "error"
    else:
        note = f"retry {attempts}/{settings.deploy_max_retries}, {settings.deploy_retry_delay_seconds}s"
        intro, status = f"Stack **{name}** hit issues during deploy, retrying automatically.", "warn"
    failed = {svc: note for svc in services if not _row_ok(rows.get(svc))}
    outcomes.append((status, f"{intro}\n{_diff_block(services, failed)}", len(services), len(services) - len(failed)))


# Send up to one notification per non-empty status bucket this pass (error/warn/ok), instead of one mixed recap; skipped entirely for an empty bucket
def _send_recap(settings: Settings, outcomes: list[Outcome], elapsed: float) -> None:
    for status in ("error", "warn", "ok"):
        entries = [o for o in outcomes if o[0] == status]
        if not entries:
            continue
        title, color = _BUCKET_STYLE[status]
        blocks = [block for _, block, _, _ in entries]
        total = sum(t for _, _, t, _ in entries)
        started = sum(s for _, _, _, s in entries)

        shown = blocks[:_MAX_RECAP_STACKS]
        if len(blocks) > _MAX_RECAP_STACKS:
            shown = shown + [f"… and {len(blocks) - _MAX_RECAP_STACKS} more, see logs for the full list"]
        body = "\n\n".join(shown)
        description = (f"{body}\n"
                        f"**Extra Informations**\n"
                        f"{_ICON_RESULT} Result: `{_result_summary(status, started, total, len(entries))}`\n"
                        f"{_ICON_DURATION} - Duration: `{elapsed:.1f}s`\n"
                        f"{_ICON_TIME} - Time: `{_timestamp()}`")
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
    services = compose.service_names(config_json)
    # Ownership transfers through the later staged_*.replace(final_*) rename, so this is the only chown needed
    chown_recursive(deploy_path, settings.deploy_uid, settings.deploy_gid)
    return staged_compose, staged_env, owned, external, services


# Apply the staged config via `docker compose up`, promoting it to the canonical files only once `up` succeeds
def _promote_and_up(settings: Settings, name: str, deploy_path, staged_compose, staged_env) -> None:
    # If `up` fails, leave the canonical files and state hash untouched so the next reconcile retries cleanly from the same input
    try:
        compose.up(staged_compose, staged_env, name, deploy_path,
                   pull=settings.pull_images, timeout=settings.deploy_timeout_seconds,
                   retry_login=lambda: registry.login(settings))
    except Exception as exc:
        # `up` failing doesn't say which service caused it; snapshot container state now, before the staged files `ps` needs are deleted below (skipped with no webhook configured, nothing would read it)
        if settings.notify_webhook_url:
            try:
                exc.containers = compose.ps(staged_compose, staged_env, name, deploy_path, _STATUS_TIMEOUT_SECONDS)
            except Exception:
                exc.containers = {}
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

    # Every notable per-stack outcome this pass, grouped into up to 3 bucket notifications at the very end instead of one per event
    outcomes: list[Outcome] = []

    # Phase A: stage + validate every changed stack, discover network roles
    staged: dict[str, tuple] = {}
    owners: dict[str, str] = {}
    needs: dict[str, set[str]] = {}
    for stk, new_hash in changed:
        deploy_path = _deploy_path(settings, stk.name)
        try:
            staged_compose, staged_env, owned, external, services = _stage(settings, stk, deploy_path)
        except Exception as exc:
            _fail_stack(settings, st, retries, stk.name, new_hash, "validate", exc, outcomes)
            continue
        for n in owned:
            owners.setdefault(n, stk.name)
        needs[stk.name] = external
        staged[stk.name] = (stk, staged_compose, staged_env, new_hash, services)

    # Phase B: order deploys so a stack that owns a network, or explicitly lists another via .depends_on, goes after it
    depends_on = {
        name: {owners[n] for n in ext if n in owners and owners[n] != name} | staged[name][0].depends_on
        for name, ext in needs.items()
    }
    order = topo_order(priority_sorted(list(staged.keys()), settings.deploy_priority), depends_on)

    # Phase C: apply each staged stack and promote it only once `up` succeeds
    for name in order:
        stk, staged_compose, staged_env, new_hash, services = staged[name]
        deploy_path = _deploy_path(settings, name)
        was_failing = name in retries
        stack_start = time.time()
        try:
            log.info("deploying stack '%s'", name)
            _promote_and_up(settings, name, deploy_path, staged_compose, staged_env)
            _remember_stack(settings, st, known, retries, name, new_hash)
            log.info("stack '%s' deployed OK in %.1fs -> %s", name, time.time() - stack_start, deploy_path)
            verb = "recovered and deployed successfully" if was_failing else "deployed successfully"
            if services:
                intro = f"Stack **{name}** {verb} ({len(services)}/{len(services)} containers)."
                outcomes.append(("ok", f"{intro}\n{_diff_block(services, {})}", len(services), len(services)))
            else:
                outcomes.append(("ok", f"Stack **{name}** {verb}.", 0, 0))
        except Exception as exc:
            _fail_stack(settings, st, retries, name, new_hash, "deploy", exc, outcomes, services=services)

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
                    outcomes.append(("ok", f"Stack **{name}** was already gone on disk, dropped from tracking.", 0, 0))
                    continue
                try:
                    log.warning("stack '%s' removed from repo, tearing down", name)
                    compose.down(compose_file, env_file, name, deploy_path, settings.deploy_timeout_seconds)
                    _forget_stack(settings, st, known, retries, name)
                    shutil.rmtree(deploy_path, ignore_errors=True)
                    outcomes.append(("ok", f"Stack **{name}** was removed from the repo and torn down.", 0, 0))
                except Exception as exc:
                    detail = exc_detail(exc)
                    log.error("failed to tear down '%s': %s", name, detail)
                    outcomes.append(("error", f"Stack **{name}** was removed from the repo but failed to tear down:\n"
                                               f"> `{detail}`", 0, 0))
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

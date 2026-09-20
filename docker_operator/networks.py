# Derives docker network ownership and stack deploy ordering from compose config
from __future__ import annotations
import json
import logging

log = logging.getLogger("docker_operator.networks")


# Return (owned, external) docker network names from a `docker compose config --format json` result
def parse_networks(config_json: str) -> tuple[set[str], set[str]]:
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError as exc:
        log.warning("could not parse compose config json: %s", exc)
        return set(), set()

    # Defends against any unexpected shape, not just malformed JSON; `docker compose config` output is trusted but not guaranteed
    networks = data.get("networks") if isinstance(data, dict) else None
    if not isinstance(networks, dict):
        networks = {}

    owned: set[str] = set()
    external: set[str] = set()
    for key, val in networks.items():
        if not isinstance(val, dict):
            continue
        actual = val.get("name") or key
        is_external = val.get("external")
        if isinstance(is_external, dict):
            is_external = True
        (external if is_external else owned).add(actual)
    return owned, external


# Move priority names to the front, keeping relative order otherwise, so topo_order's tie-break prefers them without ever overriding a real dependency
def priority_sorted(names: list[str], priority: tuple[str, ...]) -> list[str]:
    rank = {name: i for i, name in enumerate(priority)}
    # Precomputed once instead of names.index(n) per key; that would be an O(n) scan per element, O(n^2) overall for no reason
    order = {name: i for i, name in enumerate(names)}
    return sorted(names, key=lambda n: (rank.get(n, len(priority)), order[n]))


# Order stacks via Kahn's algorithm, falling back to the given order on a dependency cycle. Picks one ready stack at a time rather than appending a whole ready-batch per round, so a stack that only just became ready (e.g. a lower-priority stack waiting on a still-pending higher-priority one) is compared against priority immediately instead of an unrelated, already-ready stack claiming the slot first
def topo_order(names: list[str], depends_on: dict[str, set[str]]) -> list[str]:
    # Same reasoning as priority_sorted: a precomputed position beats names.index(n) inside the loop below
    order = {name: i for i, name in enumerate(names)}
    remaining = {n: set(depends_on.get(n, ())) & set(names) for n in names}
    ordered: list[str] = []
    pending = list(names)
    while pending:
        ready = [n for n in pending if not remaining[n]]
        if not ready:
            log.warning("stack dependency cycle detected among %s, using original order", pending)
            ordered.extend(pending)
            break
        n = min(ready, key=order.__getitem__)
        ordered.append(n)
        pending.remove(n)
        for other in pending:
            remaining[other].discard(n)
    return ordered

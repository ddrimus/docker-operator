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

    owned: set[str] = set()
    external: set[str] = set()
    for key, val in (data.get("networks") or {}).items():
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
    return sorted(names, key=lambda n: (rank.get(n, len(priority)), names.index(n)))


# Order stacks via Kahn's algorithm, falling back to the given order on a dependency cycle
def topo_order(names: list[str], depends_on: dict[str, set[str]]) -> list[str]:
    remaining = {n: set(depends_on.get(n, ())) & set(names) for n in names}
    ordered: list[str] = []
    pending = list(names)
    while pending:
        ready = [n for n in pending if not remaining[n]]
        if not ready:
            log.warning("stack dependency cycle detected among %s, using original order", pending)
            ordered.extend(pending)
            break
        ready.sort(key=names.index)
        for n in ready:
            ordered.append(n)
            pending.remove(n)
            for other in pending:
                remaining[other].discard(n)
    return ordered

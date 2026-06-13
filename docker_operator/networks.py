# Derives docker network ownership from compose config
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

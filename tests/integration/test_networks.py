from __future__ import annotations
import json

import pytest

from docker_operator.networks import parse_networks


def _cfg(networks: dict) -> str:
    return json.dumps({"networks": networks})


def test_owned_vs_external_split():
    owned, external = parse_networks(_cfg({
        "traefik": {"name": "traefik", "driver": "bridge"},
        "proxy": {"name": "proxy", "external": True},
    }))
    assert owned == {"traefik"}
    assert external == {"proxy"}


def test_external_as_object_form_counts_as_external():
    # compose's long-form `external: {name: ...}` still means "expects to already exist", same as the boolean `true` form
    owned, external = parse_networks(_cfg({
        "proxy": {"name": "proxy", "external": {"name": "proxy"}},
    }))
    assert owned == set()
    assert external == {"proxy"}


def test_uses_declared_name_not_yaml_key():
    owned, _ = parse_networks(_cfg({
        "internal-key": {"name": "actual-docker-name", "driver": "bridge"},
    }))
    assert owned == {"actual-docker-name"}


def test_falls_back_to_key_when_no_name_field():
    owned, _ = parse_networks(_cfg({"mynet": {"driver": "bridge"}}))
    assert owned == {"mynet"}


def test_no_networks_key_returns_empty_sets():
    owned, external = parse_networks(json.dumps({}))
    assert owned == set()
    assert external == set()


def test_non_dict_network_value_is_skipped_not_crashed():
    owned, external = parse_networks(_cfg({"weird": None}))
    assert owned == set()
    assert external == set()


def test_malformed_json_returns_empty_sets_not_raises():
    owned, external = parse_networks("{not json")
    assert owned == set()
    assert external == set()

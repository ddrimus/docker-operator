from __future__ import annotations
import json

import pytest

from docker_operator.networks import parse_networks, topo_order


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


def test_topo_order_respects_simple_dependency():
    order = topo_order(["b", "a"], {"b": {"a"}})
    assert order.index("a") < order.index("b")


def test_topo_order_no_dependencies_keeps_stable_order():
    order = topo_order(["c", "a", "b"], {})
    assert order == ["c", "a", "b"]


def test_topo_order_chain_of_three():
    order = topo_order(["c", "a", "b"], {"c": {"b"}, "b": {"a"}})
    assert order == ["a", "b", "c"]


def test_topo_order_ignores_dependency_outside_the_given_set():
    # A dependency on something not in `names` (e.g. a network owned by an unchanged, already-running stack) shouldn't block anything
    order = topo_order(["a", "b"], {"a": {"not-in-this-batch"}})
    assert set(order) == {"a", "b"}


def test_topo_order_cycle_falls_back_to_original_order_without_raising():
    order = topo_order(["x", "y"], {"x": {"y"}, "y": {"x"}})
    assert set(order) == {"x", "y"}
    assert len(order) == 2


@pytest.mark.parametrize("names", [[], ["solo"]])
def test_topo_order_trivial_inputs(names):
    assert topo_order(names, {}) == names

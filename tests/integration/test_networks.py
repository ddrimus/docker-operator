from __future__ import annotations
import json

import pytest

from docker_operator.networks import parse_networks, priority_sorted, topo_order


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


def test_non_dict_networks_key_is_skipped_not_crashed():
    owned, external = parse_networks(json.dumps({"networks": ["not", "a", "dict"]}))
    assert owned == set()
    assert external == set()


def test_non_dict_top_level_json_is_skipped_not_crashed():
    owned, external = parse_networks(json.dumps(["not", "an", "object"]))
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


# --- priority_sorted ---

def test_priority_sorted_moves_priority_names_to_front_in_given_order():
    assert priority_sorted(["a", "b", "c"], ("c", "a")) == ["c", "a", "b"]


def test_priority_sorted_no_priority_keeps_original_order():
    assert priority_sorted(["c", "a", "b"], ()) == ["c", "a", "b"]


def test_priority_sorted_ignores_priority_name_not_present():
    assert priority_sorted(["a", "b"], ("nginx", "a")) == ["a", "b"]


def test_priority_sorted_preserves_relative_order_of_non_priority_names():
    assert priority_sorted(["a", "b", "c", "d"], ("c",)) == ["c", "a", "b", "d"]


def test_priority_sorted_feeds_into_topo_order_without_overriding_real_dependencies():
    # "b" is priority but depends on "a"; the network dependency still wins over priority
    order = topo_order(priority_sorted(["a", "b"], ("b",)), {"b": {"a"}})
    assert order.index("a") < order.index("b")


def test_priority_sorted_wins_ties_left_to_topo_order():
    order = topo_order(priority_sorted(["a", "b", "c"], ("c",)), {})
    assert order == ["c", "a", "b"]


def test_priority_stack_waiting_on_its_own_dependency_is_not_overtaken_by_an_unrelated_stack():
    # Regression: "b" is priority and depends on "a" (also priority), so it isn't ready until round 2; an
    # unrelated, dependency-free "independent" must not slip in ahead of it just because it was ready sooner
    order = topo_order(priority_sorted(["independent", "a", "b"], ("a", "b")), {"b": {"a"}})
    assert order == ["a", "b", "independent"]

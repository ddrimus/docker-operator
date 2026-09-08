from __future__ import annotations
from pathlib import Path

from docker_operator import state
import pytest


def test_load_missing_file_returns_empty_stacks(tmp_path: Path):
    assert state.load(tmp_path / "state.json") == {"stacks": {}}


def test_save_then_load_round_trips(tmp_path: Path):
    sf = tmp_path / "sub" / "state.json"
    data = {"stacks": {"traefik": {"hash": "abc123"}}}
    state.save(sf, data)
    assert state.load(sf) == data


def test_save_creates_parent_directories(tmp_path: Path):
    sf = tmp_path / "does" / "not" / "exist" / "state.json"
    state.save(sf, {"stacks": {}})
    assert sf.is_file()


def test_load_corrupted_json_returns_fresh_state_not_raise(tmp_path: Path):
    sf = tmp_path / "state.json"
    sf.write_text("{not valid json at all")
    assert state.load(sf) == {"stacks": {}}


def test_load_non_object_json_returns_fresh_state_not_raise(tmp_path: Path):
    sf = tmp_path / "state.json"
    sf.write_text("[1, 2, 3]")
    assert state.load(sf) == {"stacks": {}}


def test_load_stacks_key_wrong_type_returns_fresh_state_not_raise(tmp_path: Path):
    sf = tmp_path / "state.json"
    sf.write_text('{"stacks": ["not", "a", "dict"]}')
    assert state.load(sf) == {"stacks": {}}


def test_load_retries_key_wrong_type_returns_fresh_state_not_raise(tmp_path: Path):
    sf = tmp_path / "state.json"
    sf.write_text('{"stacks": {}, "retries": "not a dict"}')
    assert state.load(sf) == {"stacks": {}}


def test_save_is_atomic_no_tmp_file_left_behind(tmp_path: Path):
    sf = tmp_path / "state.json"
    state.save(sf, {"stacks": {"a": {"hash": "1"}}})
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_save_overwrites_previous_content_fully(tmp_path: Path):
    sf = tmp_path / "state.json"
    state.save(sf, {"stacks": {"a": {"hash": "1"}, "b": {"hash": "2"}}})
    state.save(sf, {"stacks": {"a": {"hash": "1"}}})
    assert state.load(sf) == {"stacks": {"a": {"hash": "1"}}}


def test_save_cleans_up_tmp_file_on_write_failure(tmp_path: Path):
    from unittest.mock import patch
    sf = tmp_path / "state.json"

    with patch("json.dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            state.save(sf, {"stacks": {"a": {"hash": "1"}}})

    assert not sf.exists()
    leftovers = list(tmp_path.iterdir())
    assert leftovers == []

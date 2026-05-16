"""Tests for the SQLite state manager."""

import tempfile
from pathlib import Path

import pytest

from g_code_mode.state import StateManager


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(path=tmp_path / "test.db")


def test_create_and_retrieve_operation(state: StateManager):
    op_id = state.create_operation("deploy_agent_engine", {"project": "test"})
    op = state.get_operation(op_id)
    assert op is not None
    assert op["type"] == "deploy_agent_engine"
    assert op["status"] == "in_flight"
    assert op["params"]["project"] == "test"


def test_update_status(state: StateManager):
    op_id = state.create_operation("delete_agent_engine", {})
    state.update_status(op_id, "completed", {"deleted": "rn/123"})
    op = state.get_operation(op_id)
    assert op is not None
    assert op["status"] == "completed"
    assert op["result"]["deleted"] == "rn/123"


def test_set_snapshot(state: StateManager):
    op_id = state.create_operation("delete_agent_engine", {})
    state.set_snapshot(op_id, {"resource_name": "projects/1/locations/us/reasoningEngines/2"})
    op = state.get_operation(op_id)
    assert op is not None
    assert op["snapshot"]["resource_name"] == "projects/1/locations/us/reasoningEngines/2"


def test_set_undo_recipe(state: StateManager):
    op_id = state.create_operation("deploy_agent_engine", {})
    state.set_undo_recipe(op_id, {"description": "delete it", "call": "delete(...)"})
    op = state.get_operation(op_id)
    assert op is not None
    assert op["undo_recipe"]["description"] == "delete it"


def test_get_in_flight(state: StateManager):
    state.create_operation("deploy_agent_engine", {})
    state.create_operation("deploy_agent_engine", {})
    in_flight = state.get_in_flight()
    assert len(in_flight) == 2


def test_in_flight_excludes_completed(state: StateManager):
    op_id = state.create_operation("deploy_agent_engine", {})
    state.update_status(op_id, "completed")
    assert state.get_in_flight() == []

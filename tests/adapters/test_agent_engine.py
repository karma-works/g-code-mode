"""Unit tests for Vertex AI Agent Engine adapter — trap coverage."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from g_code_mode.adapters.vertex_ai.agent_engine import (
    AgentEngineAdapter,
    _validate_resource_name,
    _warn_secret_env_vars,
)
from g_code_mode.state import StateManager


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(path=tmp_path / "test.db")


@pytest.fixture
def adapter(state: StateManager) -> AgentEngineAdapter:
    return AgentEngineAdapter(state=state)


# ── Trap-3: resource name validation ──────────────────────────────────────


def test_valid_resource_name():
    _validate_resource_name("projects/123/locations/us-central1/reasoningEngines/456")


def test_invalid_resource_name_raises():
    with pytest.raises(ValueError, match="Invalid Agent Engine resource name"):
        _validate_resource_name("not-a-valid-name")


def test_resource_name_missing_project_number():
    with pytest.raises(ValueError):
        _validate_resource_name("projects/abc/locations/us-central1/reasoningEngines/456")


# ── Trap-7: secret env var warnings ───────────────────────────────────────


def test_secret_key_triggers_warning():
    warnings = _warn_secret_env_vars({"BRAVE_SEARCH_API_KEY": "sk-abc"})
    assert len(warnings) == 1
    assert "Secret Manager" in warnings[0]


def test_non_secret_key_no_warning():
    warnings = _warn_secret_env_vars({"PROJECT_ID": "my-project", "REGION": "us-central1"})
    assert warnings == []


def test_multiple_secret_keys():
    warnings = _warn_secret_env_vars({"API_KEY": "x", "DB_PASSWORD": "y", "NORMAL": "z"})
    assert len(warnings) == 2


# ── Trap-1: ADC check on every mutating operation ─────────────────────────


@pytest.mark.asyncio
async def test_list_requires_adc(adapter: AgentEngineAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.list_agent_engines("proj", "us-central1")


@pytest.mark.asyncio
async def test_deploy_requires_adc(adapter: AgentEngineAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.deploy_agent_engine(
            project="proj",
            location="us-central1",
            display_name="test",
            package_path=".",
            requirements=[],
        )


# ── Trap-3: get_agent_engine validates resource name before API call ───────


@pytest.mark.asyncio
async def test_get_validates_resource_name(adapter: AgentEngineAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))
    with pytest.raises(ValueError, match="Invalid Agent Engine resource name"):
        await adapter.get_agent_engine("bad-name")


# ── delete records snapshot ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_records_snapshot(adapter: AgentEngineAdapter, monkeypatch):
    rn = "projects/123/locations/us-central1/reasoningEngines/456"

    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    fake_engine = MagicMock()
    fake_engine.resource_name = rn
    fake_engine.display_name = "my-agent"
    fake_engine.create_time = "2026-01-01"
    fake_engine.update_time = "2026-01-01"
    fake_engine._gca_resource = ""
    fake_engine.delete = MagicMock()

    fake_aiplatform = MagicMock()
    fake_aiplatform.agent_engines.get.return_value = fake_engine
    fake_aiplatform.init = MagicMock()

    with patch.dict("sys.modules", {"google.cloud.aiplatform": fake_aiplatform}):
        result = await adapter.delete_agent_engine(rn)

    assert result["success"] is True
    assert result["snapshot"] is not None
    assert result["undo_recipe"]["description"] != ""
    op = adapter._state.get_operation(result["op_id"])
    assert op is not None
    assert op["snapshot"] is not None

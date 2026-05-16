"""Unit tests for Cloud Run adapter — all 6 trap coverage + undo recipes."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from g_code_mode.adapters.cloud_run.service import (
    CloudRunAdapter,
    _build_traffic_targets,
    _service_to_dict,
    _warn_secret_keys,
)
from g_code_mode.state import StateManager


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(path=tmp_path / "test.db")


@pytest.fixture
def adapter(state: StateManager) -> CloudRunAdapter:
    return CloudRunAdapter(state=state)


# ── _warn_secret_keys ─────────────────────────────────────────────────────────


def test_secret_key_triggers_warning():
    warnings = _warn_secret_keys({"API_KEY": "sk-abc"})
    assert len(warnings) == 1
    assert "Secret Manager" in warnings[0]


def test_secret_token_triggers_warning():
    warnings = _warn_secret_keys({"GITHUB_TOKEN": "ghp-xyz"})
    assert len(warnings) == 1


def test_secret_password_triggers_warning():
    warnings = _warn_secret_keys({"DB_PASSWORD": "hunter2"})
    assert len(warnings) == 1


def test_non_secret_no_warning():
    warnings = _warn_secret_keys({"PROJECT_ID": "my-proj", "REGION": "us-central1"})
    assert warnings == []


def test_multiple_secret_keys_multiple_warnings():
    warnings = _warn_secret_keys({"API_KEY": "a", "DB_SECRET": "b", "NORMAL": "c"})
    assert len(warnings) == 2


# ── _service_to_dict — secret values never exposed ───────────────────────────


def _make_mock_service(
    name: str = "my-service",
    region: str = "europe-west1",
    project: str = "my-project",
    secret_env_vars: list[tuple[str, str]] | None = None,
    plain_env_vars: list[tuple[str, str]] | None = None,
) -> MagicMock:
    svc = MagicMock()
    svc.name = f"projects/{project}/locations/{region}/services/{name}"
    svc.uri = f"https://{name}-abc.a.run.app"
    svc.latest_ready_revision = f"projects/{project}/locations/{region}/services/{name}/revisions/{name}-00001-abc"
    svc.ingress = MagicMock()
    svc.ingress.name = "INGRESS_TRAFFIC_ALL"

    # Terminal condition: ready
    svc.terminal_condition = MagicMock()
    svc.terminal_condition.type_ = "Ready"
    svc.terminal_condition.state = MagicMock()
    svc.terminal_condition.state.name = "STATE_TRUE"

    # Traffic
    t = MagicMock()
    t.revision = ""
    t.percent = 100
    t.type_ = MagicMock()
    t.type_.name = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    svc.traffic = [t]

    # Scaling
    svc.template = MagicMock()
    svc.template.scaling = MagicMock()
    svc.template.scaling.min_instance_count = 0
    svc.template.scaling.max_instance_count = 100
    svc.template.service_account = "svc@project.iam.gserviceaccount.com"
    svc.template.execution_environment = None

    # Containers with env vars
    container = MagicMock()
    container.image = "europe-west1-docker.pkg.dev/my-project/repo/app:latest"
    container.env = []

    for k, v in (plain_env_vars or []):
        ev = MagicMock()
        ev.name = k
        ev.value = v
        ev.value_source = None
        container.env.append(ev)

    for k, _secret_name in (secret_env_vars or []):
        ev = MagicMock()
        ev.name = k
        ev.value_source = MagicMock()
        ev.value_source.secret_key_ref = MagicMock()
        ev.value_source.secret_key_ref.secret = f"projects/my-project/secrets/{_secret_name}"
        container.env.append(ev)

    svc.template.containers = [container]
    return svc


def test_service_to_dict_excludes_secret_values():
    svc = _make_mock_service(
        secret_env_vars=[("API_KEY", "my-api-key-secret")],
        plain_env_vars=[("PROJECT_ID", "my-proj")],
    )
    result = _service_to_dict(svc, "my-project", "europe-west1")

    assert "API_KEY" in result["secret_env_var_keys"]
    assert "PROJECT_ID" in result["env_var_keys"]
    # No raw values exposed anywhere
    assert "my-api-key-secret" not in str(result)


def test_service_to_dict_plain_secret_like_key_flagged():
    """Trap CR-2: plain env var with secret-like name goes into secret_env_var_keys."""
    svc = _make_mock_service(plain_env_vars=[("DB_PASSWORD", "hunter2")])
    result = _service_to_dict(svc, "my-project", "europe-west1")

    assert "DB_PASSWORD" in result["secret_env_var_keys"]
    # Value must not appear
    assert "hunter2" not in str(result)


def test_service_to_dict_normal_fields():
    svc = _make_mock_service(plain_env_vars=[("REGION", "us-central1")])
    result = _service_to_dict(svc, "my-project", "europe-west1")

    assert result["name"] == "my-service"
    assert result["region"] == "europe-west1"
    assert result["ingress"] == "INGRESS_TRAFFIC_ALL"
    assert result["ready"] is True
    assert result["url"].startswith("https://")


# ── _build_traffic_targets ────────────────────────────────────────────────────


def test_build_traffic_targets_latest():
    from google.cloud import run_v2  # type: ignore[import-untyped]

    targets = _build_traffic_targets({"LATEST": 100})
    assert len(targets) == 1
    assert targets[0].type_ == run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
    assert targets[0].percent == 100


def test_build_traffic_targets_named_revision():
    from google.cloud import run_v2  # type: ignore[import-untyped]

    targets = _build_traffic_targets({"my-service-00041-abc": 100})
    assert len(targets) == 1
    assert targets[0].type_ == run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
    assert targets[0].revision == "my-service-00041-abc"


def test_build_traffic_targets_split():
    from google.cloud import run_v2  # type: ignore[import-untyped]

    targets = _build_traffic_targets({"LATEST": 90, "my-service-00041-abc": 10})
    assert len(targets) == 2
    total_pct = sum(t.percent for t in targets)
    assert total_pct == 100


# ── Trap CR-6: set_traffic sum validation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_set_traffic_sum_not_100_raises(adapter: CloudRunAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))
    with pytest.raises(ValueError, match="must sum to 100"):
        await adapter.set_traffic(
            project="proj",
            region="europe-west1",
            service_id="my-service",
            splits={"LATEST": 50, "my-service-00041-abc": 40},  # sums to 90
        )


@pytest.mark.asyncio
async def test_set_traffic_sum_zero_raises(adapter: CloudRunAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))
    with pytest.raises(ValueError, match="must sum to 100"):
        await adapter.set_traffic(
            project="proj",
            region="europe-west1",
            service_id="my-service",
            splits={"LATEST": 60},  # sums to 60
        )


# ── Trap CR-1: background thread warning ─────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_revision_bg_thread_warning(adapter: CloudRunAdapter, monkeypatch):
    """min_instances=0 + cpu_throttling=True triggers Trap CR-1 warning."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_svc = _make_mock_service()
    mock_get = AsyncMock(return_value=mock_svc)
    mock_updated = MagicMock()
    mock_updated.latest_ready_revision = "projects/proj/locations/r/services/svc/revisions/svc-00002-abc"
    mock_updated.uri = "https://svc.run.app"
    mock_op = AsyncMock()
    mock_op.result = AsyncMock(return_value=mock_updated)
    mock_client = AsyncMock()
    mock_client.get_service = AsyncMock(return_value=mock_svc)
    mock_client.update_service = AsyncMock(return_value=mock_op)

    mock_svc.template.containers[0].env = []

    with patch("g_code_mode.adapters.cloud_run.service.CloudRunAdapter.get_service", new_callable=AsyncMock) as mock_gs, \
         patch("g_code_mode.adapters.cloud_run.service.CloudRunAdapter.list_services", new_callable=AsyncMock), \
         patch("google.cloud.run_v2.ServicesAsyncClient", return_value=mock_client):
        mock_gs.return_value = {
            "name": "my-service",
            "traffic": [{"revision": "LATEST", "percent": 100}],
            "latest_revision": "my-service-00001-abc",
            "ingress": "INGRESS_TRAFFIC_ALL",
        }
        result = await adapter.deploy_revision(
            project="proj",
            region="europe-west1",
            service_id="my-service",
            image="gcr.io/proj/app:latest",
            min_instances=0,
            cpu_throttling=True,
        )

    assert any("Background" in w or "min_instances" in w for w in result["warnings"])


# ── Trap CR-3: AGENT_ENGINE_RESOURCE_NAME warning ────────────────────────────


@pytest.mark.asyncio
async def test_deploy_revision_agent_engine_warning(adapter: CloudRunAdapter, monkeypatch):
    """Deploying with AGENT_ENGINE_RESOURCE_NAME triggers Trap CR-3 warning."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_svc = _make_mock_service()
    mock_svc.template.containers[0].env = []
    mock_updated = MagicMock()
    mock_updated.latest_ready_revision = "projects/proj/locations/r/services/svc/revisions/svc-00002-abc"
    mock_updated.uri = "https://svc.run.app"
    mock_op = AsyncMock()
    mock_op.result = AsyncMock(return_value=mock_updated)
    mock_client = AsyncMock()
    mock_client.get_service = AsyncMock(return_value=mock_svc)
    mock_client.update_service = AsyncMock(return_value=mock_op)

    with patch("g_code_mode.adapters.cloud_run.service.CloudRunAdapter.get_service", new_callable=AsyncMock) as mock_gs, \
         patch("google.cloud.run_v2.ServicesAsyncClient", return_value=mock_client):
        mock_gs.return_value = {
            "name": "my-service",
            "traffic": [{"revision": "LATEST", "percent": 100}],
            "latest_revision": "my-service-00001-abc",
            "ingress": "INGRESS_TRAFFIC_ALL",
        }
        result = await adapter.deploy_revision(
            project="proj",
            region="europe-west1",
            service_id="my-service",
            image="gcr.io/proj/app:latest",
            env_vars={"AGENT_ENGINE_RESOURCE_NAME": "projects/123/locations/us-central1/reasoningEngines/456"},
        )

    assert any("AGENT_ENGINE_RESOURCE_NAME" in w for w in result["warnings"])


# ── Trap CR-4: region mismatch / service not found ───────────────────────────


@pytest.mark.asyncio
async def test_deploy_revision_service_not_found(adapter: CloudRunAdapter, monkeypatch):
    """deploy_revision raises clear error when service doesn't exist (Trap CR-4)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    with patch(
        "g_code_mode.adapters.cloud_run.service.CloudRunAdapter.get_service",
        new_callable=AsyncMock,
        side_effect=Exception("404 Not Found"),
    ):
        with pytest.raises(ValueError, match="not found"):
            await adapter.deploy_revision(
                project="proj",
                region="wrong-region",
                service_id="my-service",
                image="gcr.io/proj/app:latest",
            )


# ── rollback_revision: revision must exist ───────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_unknown_revision_raises(adapter: CloudRunAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    with patch(
        "g_code_mode.adapters.cloud_run.service.CloudRunAdapter.list_revisions",
        new_callable=AsyncMock,
        return_value=[{"name": "my-service-00041-abc", "image": "x", "create_time": "t", "ready": True}],
    ):
        with pytest.raises(ValueError, match="not found"):
            await adapter.rollback_revision(
                project="proj",
                region="europe-west1",
                service_id="my-service",
                revision_name="my-service-99999-xyz",  # doesn't exist
            )


# ── undo recipe is always present on successful execute ──────────────────────


@pytest.mark.asyncio
async def test_set_traffic_undo_recipe(adapter: CloudRunAdapter, monkeypatch):
    """set_traffic returns an undo_recipe capturing the prior splits."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    prior_traffic = [{"revision": "my-service-00041-abc", "percent": 100}]
    snapshot = {
        "name": "my-service",
        "traffic": prior_traffic,
        "latest_revision": "my-service-00041-abc",
    }
    mock_updated = MagicMock()
    mock_op = AsyncMock()
    mock_op.result = AsyncMock(return_value=mock_updated)
    mock_client = AsyncMock()
    mock_client.get_service = AsyncMock(return_value=_make_mock_service())
    mock_client.update_service = AsyncMock(return_value=mock_op)

    with patch("g_code_mode.adapters.cloud_run.service.CloudRunAdapter.get_service", new_callable=AsyncMock, return_value=snapshot), \
         patch("g_code_mode.adapters.cloud_run.service.CloudRunAdapter.list_revisions", new_callable=AsyncMock, return_value=[{"name": "LATEST", "image": "x", "create_time": "t", "ready": True}]), \
         patch("google.cloud.run_v2.ServicesAsyncClient", return_value=mock_client):
        result = await adapter.set_traffic(
            project="proj",
            region="europe-west1",
            service_id="my-service",
            splits={"LATEST": 100},
        )

    assert result["success"] is True
    undo = result["undo_recipe"]
    assert "set_traffic" in undo["call"]
    assert "my-service-00041-abc" in undo["call"]
    assert undo["description"] != ""


# ── ADC required on every operation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_services_requires_adc(adapter: CloudRunAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.list_services("proj", "europe-west1")


@pytest.mark.asyncio
async def test_get_service_requires_adc(adapter: CloudRunAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.get_service("proj", "europe-west1", "svc")


@pytest.mark.asyncio
async def test_set_traffic_requires_adc(adapter: CloudRunAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.set_traffic("proj", "europe-west1", "svc", {"LATEST": 100})

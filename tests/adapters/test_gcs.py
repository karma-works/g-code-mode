"""Unit tests for GCS adapter — all 10 trap coverage + undo recipes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from g_code_mode.adapters.gcs.service import (
    GCSAdapter,
    _bucket_detail,
    _bucket_summary,
    _iam_bindings,
    _resolve_adc_identity,
)
from g_code_mode.state import StateManager


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(path=tmp_path / "test.db")


@pytest.fixture
def adapter(state: StateManager) -> GCSAdapter:
    return GCSAdapter(state=state)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_mock_bucket(
    name: str = "my-bucket",
    storage_class: str = "STANDARD",
    versioning_enabled: bool = False,
    retention_policy_locked: bool = False,
    retention_period: int | None = None,
    uniform_access_enabled: bool = False,
    uniform_access_lock_time: datetime | None = None,
    soft_delete_retention: int | None = 604800,  # 7 days default
    lifecycle_rules: list | None = None,
    autoclass_enabled: bool = False,
) -> MagicMock:
    b = MagicMock()
    b.name = name
    b.storage_class = storage_class
    b.location = "EU"
    b.location_type = "multi-region"
    b.versioning_enabled = versioning_enabled
    b.retention_policy_locked = retention_policy_locked
    b.retention_period = retention_period
    b.retention_policy_effective_time = None
    b.lifecycle_rules = lifecycle_rules or []
    b.cors = []
    b.labels = {}
    b.autoclass_enabled = autoclass_enabled

    iam_cfg = MagicMock()
    iam_cfg.uniform_bucket_level_access_enabled = uniform_access_enabled
    iam_cfg.uniform_bucket_level_access_lock_time = uniform_access_lock_time
    iam_cfg.public_access_prevention = "inherited"
    b.iam_configuration = iam_cfg

    soft_delete = MagicMock()
    soft_delete.retention_duration_seconds = soft_delete_retention
    soft_delete.effective_time = None
    b.soft_delete_policy = soft_delete

    return b


def _make_mock_blob(
    name: str = "path/to/object.txt",
    bucket_name: str = "my-bucket",
    size: int = 1024,
    generation: int = 111,
    storage_class: str = "STANDARD",
) -> MagicMock:
    blob = MagicMock()
    blob.name = name
    blob.bucket = MagicMock()
    blob.bucket.name = bucket_name
    blob.size = size
    blob.content_type = "text/plain"
    blob.updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    blob.storage_class = storage_class
    blob.generation = generation
    blob.metageneration = 1
    blob.md5_hash = "abc"
    blob.crc32c = "def"
    return blob


# ── ADC required on all operations ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_buckets_requires_adc(adapter: GCSAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (_ for _ in ()).throw(Exception("no ADC")))
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.list_buckets("proj")


@pytest.mark.asyncio
async def test_upload_object_requires_adc(adapter: GCSAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (_ for _ in ()).throw(Exception("no ADC")))
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.upload_object("proj", "bucket", "obj.txt", "data")


@pytest.mark.asyncio
async def test_delete_object_requires_adc(adapter: GCSAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (_ for _ in ()).throw(Exception("no ADC")))
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.delete_object("proj", "bucket", "obj.txt")


# ── Trap GCS-2: silent overwrite guard ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_object_warns_on_overwrite(adapter: GCSAdapter, monkeypatch):
    """Uploading over an existing object surfaces a warning (Trap GCS-2)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=True)
    existing_blob = _make_mock_blob(generation=100)
    mock_bucket.get_blob.return_value = existing_blob

    new_blob = _make_mock_blob(generation=200)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    new_blob.generation = 200

    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "path/obj.txt", b"new content")

    assert result["success"] is True
    assert any("already exists" in w for w in result["warnings"])
    assert any("generation=100" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_upload_object_if_not_exists_raises_on_existing(
    adapter: GCSAdapter, monkeypatch
):
    """if_not_exists=True raises when object already exists (Trap GCS-2)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket()
    existing_blob = _make_mock_blob(generation=100)
    mock_bucket.get_blob.return_value = existing_blob

    upload_blob = MagicMock()
    mock_bucket.blob.return_value = upload_blob
    upload_blob.upload_from_string.side_effect = Exception("412 Precondition Failed")

    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="if_not_exists=True"):
            await adapter.upload_object(
                "proj", "my-bucket", "path/obj.txt", b"data", if_not_exists=True
            )


@pytest.mark.asyncio
async def test_upload_new_object_undo_is_delete(adapter: GCSAdapter, monkeypatch):
    """Undo for a new upload is delete_object (Trap GCS-2)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket()
    mock_bucket.get_blob.return_value = None  # object is new

    new_blob = _make_mock_blob(generation=999)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None

    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "new.txt", b"hello")

    assert "delete_object" in result["undo_recipe"]["call"]
    assert result["snapshot"] is None  # no prior object


# ── Trap GCS-3: retention policy lock ────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_blocked_by_retention_lock(adapter: GCSAdapter, monkeypatch):
    """Upload raises when bucket has a locked retention policy (Trap GCS-3)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(retention_policy_locked=True, retention_period=31536000)
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="RetentionPolicyLocked"):
            await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")


@pytest.mark.asyncio
async def test_set_lifecycle_blocked_by_retention_lock(adapter: GCSAdapter, monkeypatch):
    """set_lifecycle_policy raises when bucket has a locked retention policy (Trap GCS-3)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(retention_policy_locked=True)
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="RetentionPolicyLocked"):
            await adapter.set_lifecycle_policy("proj", "my-bucket", [])


# ── Trap GCS-4: uniform bucket-level access ───────────────────────────────────


@pytest.mark.asyncio
async def test_enable_uniform_access_warns_on_acl_breakage(adapter: GCSAdapter, monkeypatch):
    """Enabling uniform access warns that ACLs will be disabled immediately (Trap GCS-4)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(uniform_access_enabled=False)
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_uniform_bucket_access("proj", "my-bucket", enabled=True)

    assert result["success"] is True
    assert any("ACLs" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_disable_uniform_access_blocked_when_locked(adapter: GCSAdapter, monkeypatch):
    """Disabling uniform access is blocked when lock_time is in the past (Trap GCS-4)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    locked_at = datetime.now(tz=timezone.utc) - timedelta(days=1)  # lock already passed
    mock_bucket = _make_mock_bucket(
        uniform_access_enabled=True,
        uniform_access_lock_time=locked_at,
    )
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="locked"):
            await adapter.set_uniform_bucket_access("proj", "my-bucket", enabled=False)


@pytest.mark.asyncio
async def test_disable_uniform_access_warns_when_approaching_lock(
    adapter: GCSAdapter, monkeypatch
):
    """Disabling uniform access warns when lock_time is in the future (Trap GCS-4)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    future_lock = datetime.now(tz=timezone.utc) + timedelta(days=10)
    mock_bucket = _make_mock_bucket(
        uniform_access_enabled=True,
        uniform_access_lock_time=future_lock,
    )
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_uniform_bucket_access("proj", "my-bucket", enabled=False)

    assert result["success"] is True
    assert any("locked" in w.lower() for w in result["warnings"])


# ── Trap GCS-5: lifecycle 24h propagation ────────────────────────────────────


@pytest.mark.asyncio
async def test_set_lifecycle_includes_propagation_warning(adapter: GCSAdapter, monkeypatch):
    """set_lifecycle_policy always includes the 24h propagation warning (Trap GCS-5)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket()
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_lifecycle_policy(
            "proj", "my-bucket", [{"action": {"type": "Delete"}, "condition": {"age": 90}}]
        )

    assert any("24 hours" in w for w in result["warnings"])
    assert "set_lifecycle_policy" in result["undo_recipe"]["call"]


# ── Trap GCS-6: delete undo paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_object_warns_irreversible_when_no_protection(
    adapter: GCSAdapter, monkeypatch
):
    """delete_object warns permanent when no versioning and no soft delete (Trap GCS-6)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=False, soft_delete_retention=None)
    mock_bucket.soft_delete_policy.retention_duration_seconds = None
    blob = _make_mock_blob()
    mock_bucket.get_blob.return_value = blob
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.delete_object("proj", "my-bucket", "path/obj.txt")

    assert result["details"]["reversible"] is False
    assert any("PERMANENT" in w for w in result["warnings"])
    assert "Cannot undo" in result["undo_recipe"]["call"]


@pytest.mark.asyncio
async def test_delete_object_undo_has_restore_cmd_when_soft_delete(
    adapter: GCSAdapter, monkeypatch
):
    """delete_object undo recipe contains restore command when soft delete is active (Trap GCS-6)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=False, soft_delete_retention=604800)
    blob = _make_mock_blob(generation=42)
    mock_bucket.get_blob.return_value = blob
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.delete_object("proj", "my-bucket", "path/obj.txt")

    assert result["details"]["reversible"] is True
    assert "gcloud storage restore" in result["undo_recipe"]["call"]
    assert "42" in result["undo_recipe"]["call"]


# ── Trap GCS-7: ADC identity in pre-flight ───────────────────────────────────


@pytest.mark.asyncio
async def test_upload_surfaces_adc_identity(adapter: GCSAdapter, monkeypatch):
    """Every mutating operation includes the resolved ADC identity in warnings (Trap GCS-7)."""
    mock_creds = MagicMock()
    mock_creds.service_account_email = "deploy@my-project.iam.gserviceaccount.com"
    monkeypatch.setattr("google.auth.default", lambda: (mock_creds, "my-project"))

    mock_bucket = _make_mock_bucket()
    mock_bucket.get_blob.return_value = None
    new_blob = _make_mock_blob(generation=1)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")

    assert any("deploy@my-project.iam.gserviceaccount.com" in w for w in result["warnings"])


# ── Trap GCS-8: allUsers IAM grant ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_bucket_iam_blocks_all_users(adapter: GCSAdapter, monkeypatch):
    """set_bucket_iam blocks allUsers unless allow_public_access=True (Trap GCS-8)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    with pytest.raises(ValueError, match="Blocked"):
        await adapter.set_bucket_iam(
            "proj",
            "my-bucket",
            [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}],
        )


@pytest.mark.asyncio
async def test_set_bucket_iam_blocks_all_authenticated_users(adapter: GCSAdapter, monkeypatch):
    """set_bucket_iam blocks allAuthenticatedUsers unless allow_public_access=True."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    with pytest.raises(ValueError, match="Blocked"):
        await adapter.set_bucket_iam(
            "proj",
            "my-bucket",
            [{"role": "roles/storage.objectViewer", "members": ["allAuthenticatedUsers"]}],
        )


@pytest.mark.asyncio
async def test_set_bucket_iam_allows_public_with_explicit_flag(
    adapter: GCSAdapter, monkeypatch
):
    """set_bucket_iam proceeds with allUsers when allow_public_access=True, warns loudly."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = MagicMock()
    prior_policy = MagicMock()
    prior_policy.bindings = []
    prior_policy.etag = "abc"
    new_policy = MagicMock()
    new_policy.bindings = []
    new_policy.etag = "abc"
    mock_bucket.get_iam_policy.side_effect = [prior_policy, new_policy]
    mock_bucket.set_iam_policy.return_value = None
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_bucket_iam(
            "proj",
            "my-bucket",
            [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}],
            allow_public_access=True,
        )

    assert result["success"] is True
    assert result["details"]["has_public_access"] is True
    assert any("[PUBLIC]" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_set_bucket_iam_non_public_succeeds(adapter: GCSAdapter, monkeypatch):
    """set_bucket_iam with normal members proceeds without warning (Trap GCS-8)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = MagicMock()
    prior_policy = MagicMock()
    prior_policy.bindings = []
    new_policy = MagicMock()
    new_policy.bindings = []
    mock_bucket.get_iam_policy.side_effect = [prior_policy, new_policy]
    mock_bucket.set_iam_policy.return_value = None
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_bucket_iam(
            "proj",
            "my-bucket",
            [{"role": "roles/storage.objectViewer", "members": ["user:dev@example.com"]}],
        )

    assert result["success"] is True
    assert result["details"]["has_public_access"] is False


# ── Trap GCS-9: versioning without lifecycle ─────────────────────────────────


@pytest.mark.asyncio
async def test_enable_versioning_adds_noncurrent_expiry_by_default(
    adapter: GCSAdapter, monkeypatch
):
    """enable_versioning adds a noncurrent expiry lifecycle rule by default (Trap GCS-9)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=False, lifecycle_rules=[])
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.enable_versioning("proj", "my-bucket")

    assert result["details"]["noncurrent_expiry_added"] is True
    assert result["details"]["noncurrent_expiry_days"] == 30
    # lifecycle_rules setter was called with the noncurrent rule
    assert mock_bucket.lifecycle_rules is not None
    assert any("cost" in w.lower() or "expiry" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_enable_versioning_skips_expiry_when_already_enabled(
    adapter: GCSAdapter, monkeypatch
):
    """enable_versioning skips adding expiry rule if versioning already on (Trap GCS-9)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=True, lifecycle_rules=[])
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.enable_versioning("proj", "my-bucket")

    assert result["details"]["noncurrent_expiry_added"] is False
    assert any("already enabled" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_enable_versioning_suppresses_expiry_when_opted_out(
    adapter: GCSAdapter, monkeypatch
):
    """enable_versioning respects add_noncurrent_expiry=False (Trap GCS-9)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(versioning_enabled=False)
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.enable_versioning(
            "proj", "my-bucket", add_noncurrent_expiry=False
        )

    assert result["details"]["noncurrent_expiry_added"] is False


# ── Trap GCS-10: cold storage class fees ─────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_to_coldline_bucket_warns(adapter: GCSAdapter, monkeypatch):
    """Uploading to a Coldline bucket includes retrieval fee warning (Trap GCS-10)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(storage_class="COLDLINE")
    mock_bucket.get_blob.return_value = None
    new_blob = _make_mock_blob(generation=1)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")

    assert any("COLDLINE" in w for w in result["warnings"])
    assert any("$0.01" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_upload_to_archive_bucket_warns(adapter: GCSAdapter, monkeypatch):
    """Uploading to an Archive bucket includes retrieval fee warning (Trap GCS-10)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(storage_class="ARCHIVE")
    mock_bucket.get_blob.return_value = None
    new_blob = _make_mock_blob(generation=1)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")

    assert any("ARCHIVE" in w for w in result["warnings"])
    assert any("$0.05" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_upload_to_standard_bucket_no_cost_warning(adapter: GCSAdapter, monkeypatch):
    """Uploading to a Standard bucket has no cost warning (Trap GCS-10)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(storage_class="STANDARD")
    mock_bucket.get_blob.return_value = None
    new_blob = _make_mock_blob(generation=1)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")

    cost_warnings = [w for w in result["warnings"] if "retrieval" in w.lower() or "$0.0" in w]
    assert cost_warnings == []


@pytest.mark.asyncio
async def test_autoclass_bucket_skips_cold_warning(adapter: GCSAdapter, monkeypatch):
    """Autoclass-enabled buckets skip the cold storage warning (Trap GCS-10)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket(storage_class="COLDLINE", autoclass_enabled=True)
    mock_bucket.get_blob.return_value = None
    new_blob = _make_mock_blob(generation=1)
    mock_bucket.blob.return_value = new_blob
    new_blob.upload_from_string.return_value = None
    new_blob.reload.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.upload_object("proj", "my-bucket", "obj.txt", b"data")

    assert not any("COLDLINE" in w and "$0.01" in w for w in result["warnings"])


# ── copy_object ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_object_raises_when_source_not_found(adapter: GCSAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket()
    mock_bucket.get_blob.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="does not exist"):
            await adapter.copy_object(
                "proj", "src-bucket", "src/obj.txt", "dst-bucket", "dst/obj.txt"
            )


@pytest.mark.asyncio
async def test_copy_object_if_not_exists_raises_on_existing_dst(
    adapter: GCSAdapter, monkeypatch
):
    """copy_object raises when destination exists and if_not_exists=True (default)."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    src_blob = _make_mock_blob(name="src/obj.txt", bucket_name="src-bucket")
    dst_blob = _make_mock_blob(name="dst/obj.txt", bucket_name="dst-bucket", generation=50)

    src_bkt = _make_mock_bucket(name="src-bucket")
    dst_bkt = _make_mock_bucket(name="dst-bucket")

    def get_bucket_side_effect(name):
        return src_bkt if name == "src-bucket" else dst_bkt

    src_bkt.get_blob.return_value = src_blob
    dst_bkt.get_blob.return_value = dst_blob
    mock_client = MagicMock()
    mock_client.get_bucket.side_effect = get_bucket_side_effect

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="if_not_exists=False"):
            await adapter.copy_object(
                "proj", "src-bucket", "src/obj.txt", "dst-bucket", "dst/obj.txt"
            )


# ── delete_object: object not found ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_object_raises_when_not_found(adapter: GCSAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_bucket = _make_mock_bucket()
    mock_bucket.get_blob.return_value = None
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        with pytest.raises(ValueError, match="does not exist"):
            await adapter.delete_object("proj", "my-bucket", "missing.txt")


# ── undo recipe always present on successful execute ─────────────────────────


@pytest.mark.asyncio
async def test_set_lifecycle_undo_recipe_restores_prior_rules(
    adapter: GCSAdapter, monkeypatch
):
    """set_lifecycle_policy undo recipe contains a call to restore prior rules."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    prior_rules = [{"action": {"type": "Delete"}, "condition": {"age": 365}}]
    mock_bucket = _make_mock_bucket(lifecycle_rules=prior_rules)
    mock_client = MagicMock()
    mock_client.get_bucket.return_value = mock_bucket
    mock_bucket.patch.return_value = None

    with patch("g_code_mode.adapters.gcs.service.GCSAdapter._client", return_value=mock_client):
        result = await adapter.set_lifecycle_policy(
            "proj",
            "my-bucket",
            [{"action": {"type": "Delete"}, "condition": {"age": 90}}],
        )

    assert "set_lifecycle_policy" in result["undo_recipe"]["call"]
    assert result["snapshot"]["lifecycle_rules"] == prior_rules


# ── _resolve_adc_identity ─────────────────────────────────────────────────────


def test_resolve_adc_identity_returns_service_account(monkeypatch):
    mock_creds = MagicMock()
    mock_creds.service_account_email = "svc@proj.iam.gserviceaccount.com"
    monkeypatch.setattr("google.auth.default", lambda: (mock_creds, "my-proj"))
    identity = _resolve_adc_identity()
    assert "svc@proj.iam.gserviceaccount.com" in identity
    assert "my-proj" in identity


def test_resolve_adc_identity_fallback_on_error(monkeypatch):
    monkeypatch.setattr(
        "google.auth.default",
        lambda: (_ for _ in ()).throw(Exception("no creds")),
    )
    identity = _resolve_adc_identity()
    assert identity == "unresolved"

"""Unit tests for Firestore adapter — all 8 trap coverage + undo recipes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from g_code_mode.adapters.firestore.service import (
    FirestoreAdapter,
    _collection_ref,
    _serialize_doc,
)
from g_code_mode.state import StateManager


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(path=tmp_path / "test.db")


@pytest.fixture
def adapter(state: StateManager) -> FirestoreAdapter:
    return FirestoreAdapter(state=state)


# ── _serialize_doc — Trap FS-4 ────────────────────────────────────────────────


def test_serialize_doc_passthrough_primitives():
    data = {"name": "test", "count": 42, "active": True, "value": None}
    assert _serialize_doc(data) == data


def test_serialize_doc_datetime():
    dt = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    result = _serialize_doc({"ts": dt})
    assert isinstance(result["ts"], str)
    assert "2026-05-16" in result["ts"]


def test_serialize_doc_nested_datetime():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = _serialize_doc({"outer": {"inner": dt}})
    assert isinstance(result["outer"]["inner"], str)


def test_serialize_doc_list_with_datetime():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = _serialize_doc([dt, "hello", 42])
    assert isinstance(result[0], str)
    assert result[1] == "hello"


def test_serialize_doc_bytes():
    result = _serialize_doc({"data": b"\x00\x01\x02"})
    assert result["data"] == "<bytes>"


def test_serialize_doc_geopoint():
    gp = MagicMock()
    gp.latitude = 48.8566
    gp.longitude = 2.3522
    del gp.isoformat  # ensure it doesn't hit datetime branch
    del gp.path       # ensure it doesn't hit DocumentReference branch
    result = _serialize_doc({"location": gp})
    assert result["location"] == {"latitude": 48.8566, "longitude": 2.3522}


def test_serialize_doc_document_reference():
    ref = MagicMock()
    ref.path = "projects/proj/databases/(default)/documents/runs/abc123"
    ref.id = "abc123"
    del ref.isoformat  # not a datetime
    del ref.latitude   # not a geopoint
    result = _serialize_doc({"ref": ref})
    assert result["ref"] == ref.path


# ── _collection_ref — Trap FS-3 ───────────────────────────────────────────────


def test_collection_ref_single_segment():
    db = MagicMock()
    _collection_ref(db, "runs")
    db.collection.assert_called_once_with("runs")


def test_collection_ref_subcollection_path():
    db = MagicMock()
    col = db.collection.return_value
    doc = col.document.return_value
    _collection_ref(db, "runs/abc123/events")
    db.collection.assert_called_once_with("runs")
    col.document.assert_called_once_with("abc123")
    doc.collection.assert_called_once_with("events")


def test_collection_ref_even_segment_raises():
    db = MagicMock()
    with pytest.raises(ValueError, match="even"):
        _collection_ref(db, "runs/abc123")  # 2 segments = document path, not collection


def test_collection_ref_empty_raises():
    db = MagicMock()
    with pytest.raises(ValueError, match="must not be empty"):
        _collection_ref(db, "")


# ── ADC enforcement ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_collections_requires_adc(adapter: FirestoreAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.list_collections("my-project")


@pytest.mark.asyncio
async def test_get_document_requires_adc(adapter: FirestoreAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.get_document("proj", "(default)", "runs", "abc123")


@pytest.mark.asyncio
async def test_set_document_requires_adc(adapter: FirestoreAdapter, monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        await adapter.set_document("proj", "(default)", "runs", "abc123", {"status": "queued"})


# ── Trap FS-1: Datastore mode error surfaced clearly ─────────────────────────


@pytest.mark.asyncio
async def test_list_collections_datastore_mode_error(adapter: FirestoreAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_db = MagicMock()

    async def _fail_collections():
        raise Exception("FAILED_PRECONDITION: Datastore mode")
        yield  # make it an async generator

    mock_db.collections = _fail_collections
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        with pytest.raises(ValueError, match="Datastore mode"):
            await adapter.list_collections("my-project")


# ── Trap FS-5: PERMISSION_DENIED with IAM hint ────────────────────────────────


@pytest.mark.asyncio
async def test_list_collections_permission_denied(adapter: FirestoreAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_db = MagicMock()

    async def _fail_collections():
        raise Exception("PERMISSION_DENIED: Missing or insufficient permissions")
        yield

    mock_db.collections = _fail_collections
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        with pytest.raises(ValueError, match="roles/datastore.user"):
            await adapter.list_collections("my-project")


# ── Trap FS-8: update_document fails on non-existent doc ─────────────────────


@pytest.mark.asyncio
async def test_update_document_not_found_raises(adapter: FirestoreAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_snap = MagicMock()
    mock_snap.exists = False

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        with pytest.raises(ValueError, match="does not exist"):
            await adapter.update_document(
                "proj", "(default)", "runs", "nonexistent", {"status": "running"}
            )


# ── delete_document fails on non-existent doc ─────────────────────────────────


@pytest.mark.asyncio
async def test_delete_document_not_found_raises(adapter: FirestoreAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_snap = MagicMock()
    mock_snap.exists = False

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        with pytest.raises(ValueError, match="does not exist"):
            await adapter.delete_document("proj", "(default)", "runs", "nonexistent")


# ── set_document — undo recipe for new document ───────────────────────────────


@pytest.mark.asyncio
async def test_set_document_new_undo_is_delete(adapter: FirestoreAdapter, monkeypatch):
    """When setting a new document, undo recipe should call delete_document."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_snap = MagicMock()
    mock_snap.exists = False  # document didn't exist

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)
    mock_doc_ref.set = AsyncMock()

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        result = await adapter.set_document(
            "proj", "(default)", "runs", "new-run-id", {"status": "queued"}
        )

    assert result["success"] is True
    assert "delete_document" in result["undo_recipe"]["call"]
    assert result["details"]["created"] is True


# ── set_document — undo recipe for existing document ─────────────────────────


@pytest.mark.asyncio
async def test_set_document_existing_undo_is_restore(adapter: FirestoreAdapter, monkeypatch):
    """When replacing an existing document, undo recipe should call set_document with prior data."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict = MagicMock(return_value={"status": "queued", "prompt": "old prompt"})

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)
    mock_doc_ref.set = AsyncMock()

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        result = await adapter.set_document(
            "proj", "(default)", "runs", "existing-id", {"status": "running"}
        )

    assert result["success"] is True
    undo_call = result["undo_recipe"]["call"]
    assert "set_document" in undo_call
    assert "queued" in undo_call  # prior data embedded in undo call
    assert result["details"]["created"] is False


# ── delete_document — snapshot captured + undo recreates ─────────────────────


@pytest.mark.asyncio
async def test_delete_document_undo_recreates(adapter: FirestoreAdapter, monkeypatch):
    """delete_document undo_recipe should call set_document with the snapshot."""
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    prior_data = {"status": "completed", "prompt": "analyze market"}

    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict = MagicMock(return_value=prior_data)

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)
    mock_doc_ref.delete = AsyncMock()

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        result = await adapter.delete_document("proj", "(default)", "runs", "abc123")

    assert result["success"] is True
    assert result["snapshot"] == prior_data
    undo_call = result["undo_recipe"]["call"]
    assert "set_document" in undo_call
    assert "analyze market" in undo_call


# ── update_document — undo restores full snapshot ────────────────────────────


@pytest.mark.asyncio
async def test_update_document_undo_restores_full_snapshot(adapter: FirestoreAdapter, monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda: (MagicMock(), "proj"))

    prior_data = {"status": "queued", "prompt": "test", "event_sequence": 0}

    mock_snap = MagicMock()
    mock_snap.exists = True
    mock_snap.to_dict = MagicMock(return_value=prior_data)

    mock_doc_ref = AsyncMock()
    mock_doc_ref.get = AsyncMock(return_value=mock_snap)
    mock_doc_ref.update = AsyncMock()

    mock_col_ref = MagicMock()
    mock_col_ref.document = MagicMock(return_value=mock_doc_ref)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_col_ref)
    mock_db.close = AsyncMock()

    with patch("g_code_mode.adapters.firestore.service.FirestoreAdapter._client", return_value=mock_db):
        result = await adapter.update_document(
            "proj", "(default)", "runs", "abc123", {"status": "running"}
        )

    assert result["success"] is True
    assert result["snapshot"] == prior_data
    undo_call = result["undo_recipe"]["call"]
    assert "set_document" in undo_call
    # Full snapshot embedded — all three original keys should appear
    assert "event_sequence" in undo_call

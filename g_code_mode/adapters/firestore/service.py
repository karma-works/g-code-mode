"""
Firestore adapter — collections, documents, queries, subcollections.

Covers the production patterns from GapHunter's RunStore (app/storage.py):
collection-per-entity, subcollection events, transactional status updates.

Traps absorbed:
  FS-1  Native vs Datastore mode
  FS-2  (default) database ID
  FS-3  Even-segment document paths
  FS-4  DatetimeWithNanoseconds JSON serialization
  FS-5  Agent Engine service account needs roles/datastore.user
  FS-6  stream() with limit instead of get() for large collections
  FS-7  Subcollections invisible from list_collections
  FS-8  update() fails on non-existent documents
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from g_code_mode.adapters.firestore.types import FirestoreExecuteResult
from g_code_mode.preflight import require_adc
from g_code_mode.undo_registry import UndoRecipe

if TYPE_CHECKING:
    from g_code_mode.state import StateManager

_DEFAULT_DATABASE = "(default)"

# Trap FS-5: Agent Engine service account remediation template
_AGENT_ENGINE_IAM_HINT = (
    "If Agent Engine cannot write run state to Firestore, the Reasoning Engine "
    "service agent needs roles/datastore.user:\n"
    "  gcloud projects add-iam-policy-binding PROJECT \\\n"
    "    --member=serviceAccount:service-PROJECT_NUMBER"
    "@gcp-sa-aiplatform-re.iam.gserviceaccount.com \\\n"
    "    --role=roles/datastore.user"
)


def _serialize_doc(data: Any) -> Any:
    """Recursively convert Firestore-specific types to JSON-safe equivalents.

    Trap FS-4: DatetimeWithNanoseconds and other SDK types break json.dumps.
    """
    if isinstance(data, dict):
        return {k: _serialize_doc(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize_doc(v) for v in data]
    if isinstance(data, bytes):
        return "<bytes>"
    if hasattr(data, "isoformat"):
        return data.isoformat()
    if hasattr(data, "latitude") and hasattr(data, "longitude"):
        return {"latitude": data.latitude, "longitude": data.longitude}
    if hasattr(data, "path") and hasattr(data, "id"):
        # DocumentReference — has both .path and .id
        return data.path
    return data


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any] | None:
    """Convert a DocumentSnapshot to a serializable dict, or None if not found."""
    if not snapshot.exists:
        return None
    raw = snapshot.to_dict() or {}
    return _serialize_doc(raw)


class FirestoreAdapter:
    """Firestore operations with full g-code-mode safety stack."""

    def __init__(self, state: StateManager) -> None:
        self._state = state

    def _client(self, project: str, database: str = _DEFAULT_DATABASE) -> Any:
        from google.cloud import firestore  # type: ignore[import-untyped]

        return firestore.AsyncClient(project=project, database=database)

    # ── inquire (read-only) ────────────────────────────────────────────────

    async def list_collections(
        self, project: str, database: str = _DEFAULT_DATABASE
    ) -> list[str]:
        """
        List root-level collection IDs in a Firestore database.

        Trap FS-2: database defaults to "(default)". Pass database name explicitly
        for non-default databases.

        Trap FS-7: Only root-level collections are returned. Subcollections like
        runs/{id}/events are NOT listed here — use list_subcollections instead.

        Trap FS-5: If Agent Engine writes fail with PERMISSION_DENIED, the
        Reasoning Engine service agent needs roles/datastore.user.
        """
        require_adc()
        db = self._client(project, database)
        try:
            collections = [col.id async for col in db.collections()]
            return collections
        except Exception as exc:
            err_str = str(exc)
            if "FAILED_PRECONDITION" in err_str or "Datastore" in err_str:
                raise ValueError(
                    f"Firestore database '{database}' in project '{project}' may be in "
                    "Datastore mode. This adapter requires Native mode.\n"
                    f"Check: gcloud firestore databases describe --database={database} "
                    f"--project={project}"
                ) from exc
            if "PERMISSION_DENIED" in err_str:
                raise ValueError(
                    f"Permission denied accessing Firestore in project '{project}'.\n"
                    f"{_AGENT_ENGINE_IAM_HINT}"
                ) from exc
            raise
        finally:
            await db.close()

    async def list_documents(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        limit: int = 50,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        List documents in a collection (newest first by __name__ is not guaranteed;
        use query_documents with order_by for sorted results).

        Trap FS-6: uses stream() with limit to avoid loading entire collection into memory.
        Trap FS-7: collection is a root collection path. For subcollections, pass
        the full path like "runs/abc123/events".
        """
        require_adc()
        db = self._client(project, database)
        try:
            col_ref = _collection_ref(db, collection)
            query = col_ref.limit(limit)
            docs = []
            async for snap in query.stream():
                raw = _snapshot_to_dict(snap) or {}
                if fields:
                    raw = {k: v for k, v in raw.items() if k in fields}
                raw["_id"] = snap.id
                docs.append(raw)
            return docs
        finally:
            await db.close()

    async def get_document(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        document_id: str = "",
    ) -> dict[str, Any] | None:
        """
        Get a single document by collection path and document ID.

        Trap FS-3: collection and document_id are separate parameters to prevent
        odd-segment path confusion. Collection can be a subcollection path like
        "runs/abc123/events".

        Returns None if document does not exist.
        """
        require_adc()
        db = self._client(project, database)
        try:
            doc_ref = _collection_ref(db, collection).document(document_id)
            snap = await doc_ref.get()
            return _snapshot_to_dict(snap)
        finally:
            await db.close()

    async def query_documents(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Query documents using equality and comparison filters.

        filters: list of (field, operator, value) tuples
          Operators: "==", "!=", "<", "<=", ">", ">=", "in", "array_contains"
          Example: [("status", "==", "completed"), ("created_at", ">", "2026-01-01")]

        Trap FS-6: always uses stream() with limit.
        """
        require_adc()
        db = self._client(project, database)
        try:
            query: Any = _collection_ref(db, collection)
            for field_name, op, value in (filters or []):
                query = query.where(field=field_name, op_string=op, value=value)
            if order_by:
                query = query.order_by(order_by)
            query = query.limit(limit)
            docs = []
            async for snap in query.stream():
                raw = _snapshot_to_dict(snap) or {}
                raw["_id"] = snap.id
                docs.append(raw)
            return docs
        finally:
            await db.close()

    async def list_subcollections(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        document_id: str = "",
    ) -> list[str]:
        """
        List subcollection IDs under a specific document.

        Trap FS-7: subcollections are not discoverable from list_collections.
        This operation exposes them for a single document.

        Example: list_subcollections(project, database, "runs", "abc123")
        returns ["events", "sources"]
        """
        require_adc()
        db = self._client(project, database)
        try:
            doc_ref = _collection_ref(db, collection).document(document_id)
            return [col.id async for col in doc_ref.collections()]
        finally:
            await db.close()

    # ── execute (mutating) ─────────────────────────────────────────────────

    async def set_document(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        document_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create or replace a document (Firestore .set() semantics).

        Safety stack:
          1. Pre-flight: ADC, snapshot prior state
          2. Snapshot: prior document data (or None if document is new)
          3. Execute: doc_ref.set(data)
          4. Undo: set_document(prior_data) if existed; delete_document if new
        """
        require_adc()
        data = data or {}
        db = self._client(project, database)
        try:
            doc_ref = _collection_ref(db, collection).document(document_id)
            prior_snap = await doc_ref.get()
            prior_data = _snapshot_to_dict(prior_snap)
            doc_existed = prior_data is not None

            op_id = self._state.create_operation(
                "set_document",
                {
                    "project": project,
                    "database": database,
                    "collection": collection,
                    "document_id": document_id,
                },
            )
            self._state.set_snapshot(op_id, prior_data)

            try:
                await doc_ref.set(data)
            except Exception as exc:
                self._state.update_status(op_id, "failed")
                raise RuntimeError(f"set_document failed: {exc}") from exc

            if doc_existed:
                undo_call = (
                    f"await set_document("
                    f"project={project!r}, database={database!r}, "
                    f"collection={collection!r}, document_id={document_id!r}, "
                    f"data={prior_data!r})"
                )
                undo_desc = f"Restore document {collection}/{document_id} to prior state"
            else:
                undo_call = (
                    f"await delete_document("
                    f"project={project!r}, database={database!r}, "
                    f"collection={collection!r}, document_id={document_id!r})"
                )
                undo_desc = f"Delete new document {collection}/{document_id} (it didn't exist before)"

            undo = UndoRecipe(description=undo_desc, call=undo_call)
            self._state.set_undo_recipe(op_id, undo.to_dict())
            self._state.update_status(op_id, "completed", {"document_id": document_id})

            return FirestoreExecuteResult(
                success=True,
                project=project,
                database=database,
                collection=collection,
                document_id=document_id,
                undo_recipe=undo.to_dict(),
                snapshot=prior_data,
                details={"created": not doc_existed, "fields_written": list(data.keys())},
                op_id=op_id,
            ).to_dict()
        finally:
            await db.close()

    async def update_document(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        document_id: str = "",
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Partially update an existing document (Firestore .update() semantics).

        Trap FS-8: fails with a clear error if document doesn't exist — use
        set_document to create. The full pre-update document is snapshotted for undo.

        Safety stack:
          1. Pre-flight: ADC; get_document — raises if not found (Trap FS-8)
          2. Snapshot: full prior document
          3. Execute: doc_ref.update(updates)
          4. Undo: set_document(snapshot) to restore all fields
        """
        require_adc()
        updates = updates or {}
        db = self._client(project, database)
        try:
            doc_ref = _collection_ref(db, collection).document(document_id)
            prior_snap = await doc_ref.get()
            prior_data = _snapshot_to_dict(prior_snap)

            # Trap FS-8: update() on a missing doc raises NotFound
            if prior_data is None:
                raise ValueError(
                    f"Document '{collection}/{document_id}' does not exist. "
                    "Use set_document to create a new document."
                )

            op_id = self._state.create_operation(
                "update_document",
                {
                    "project": project,
                    "database": database,
                    "collection": collection,
                    "document_id": document_id,
                    "updated_fields": list(updates.keys()),
                },
            )
            self._state.set_snapshot(op_id, prior_data)

            try:
                await doc_ref.update(updates)
            except Exception as exc:
                self._state.update_status(op_id, "failed")
                raise RuntimeError(f"update_document failed: {exc}") from exc

            undo = UndoRecipe(
                description=f"Restore document {collection}/{document_id} to pre-update state",
                call=(
                    f"await set_document("
                    f"project={project!r}, database={database!r}, "
                    f"collection={collection!r}, document_id={document_id!r}, "
                    f"data={prior_data!r})"
                ),
            )
            self._state.set_undo_recipe(op_id, undo.to_dict())
            self._state.update_status(op_id, "completed", {"updated_fields": list(updates.keys())})

            return FirestoreExecuteResult(
                success=True,
                project=project,
                database=database,
                collection=collection,
                document_id=document_id,
                undo_recipe=undo.to_dict(),
                snapshot=prior_data,
                details={"updated_fields": list(updates.keys())},
                op_id=op_id,
            ).to_dict()
        finally:
            await db.close()

    async def delete_document(
        self,
        project: str,
        database: str = _DEFAULT_DATABASE,
        collection: str = "",
        document_id: str = "",
    ) -> dict[str, Any]:
        """
        Delete a document. Snapshots full content before deletion for undo.

        Undo: set_document with the snapshot data recreates the document.
        Raises ValueError if the document doesn't exist.
        """
        require_adc()
        db = self._client(project, database)
        try:
            doc_ref = _collection_ref(db, collection).document(document_id)
            prior_snap = await doc_ref.get()
            prior_data = _snapshot_to_dict(prior_snap)

            if prior_data is None:
                raise ValueError(
                    f"Document '{collection}/{document_id}' does not exist — nothing to delete."
                )

            op_id = self._state.create_operation(
                "delete_document",
                {
                    "project": project,
                    "database": database,
                    "collection": collection,
                    "document_id": document_id,
                },
            )
            self._state.set_snapshot(op_id, prior_data)

            try:
                await doc_ref.delete()
            except Exception as exc:
                self._state.update_status(op_id, "failed")
                raise RuntimeError(f"delete_document failed: {exc}") from exc

            undo = UndoRecipe(
                description=f"Recreate deleted document {collection}/{document_id}",
                call=(
                    f"await set_document("
                    f"project={project!r}, database={database!r}, "
                    f"collection={collection!r}, document_id={document_id!r}, "
                    f"data={prior_data!r})"
                ),
            )
            self._state.set_undo_recipe(op_id, undo.to_dict())
            self._state.update_status(op_id, "completed", {"document_id": document_id})

            return FirestoreExecuteResult(
                success=True,
                project=project,
                database=database,
                collection=collection,
                document_id=document_id,
                undo_recipe=undo.to_dict(),
                snapshot=prior_data,
                details={"deleted": True},
                op_id=op_id,
            ).to_dict()
        finally:
            await db.close()


def _collection_ref(db: Any, collection_path: str) -> Any:
    """
    Build a CollectionReference from a potentially multi-segment path.

    Trap FS-3: Firestore paths alternate collection/document segments.
    "runs" → db.collection("runs")
    "runs/abc123/events" → db.collection("runs").document("abc123").collection("events")
    Raises ValueError for even-segment paths (those are document paths, not collections).
    """
    parts = [p for p in collection_path.split("/") if p]
    if not parts:
        raise ValueError("collection path must not be empty")
    if len(parts) % 2 == 0:
        raise ValueError(
            f"Collection path '{collection_path}' has {len(parts)} segments (even). "
            "Collection paths must have an odd number of segments. "
            "Did you mean to pass a document path? Use document_id for the final segment."
        )
    ref = db.collection(parts[0])
    i = 1
    while i < len(parts):
        ref = ref.document(parts[i])
        i += 1
        if i < len(parts):
            ref = ref.collection(parts[i])
            i += 1
    return ref

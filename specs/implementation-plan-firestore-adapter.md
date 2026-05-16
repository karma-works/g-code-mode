# Implementation Plan: Firestore Adapter

**Status:** Draft — 2026-05-16  
**Scope:** Firestore adapter for g-code-mode — list collections, list/get/query documents, set/update/delete documents. Full five-layer safety stack.

---

## GapHunter Firestore usage (primary reference)

GapHunter's `RunStore` (`app/storage.py`) reveals the real production patterns:

- **Synchronous `firestore.Client`** for all operations (not Async)
- **Collection structure**: root collection `runs/{run_id}`, subcollections `runs/{run_id}/events` and `runs/{run_id}/sources`
- **Write patterns**: `.set()` for create/replace, `.update()` for partial patch, `.collection(...).document(...).set()` for subcollection writes
- **Read patterns**: `.get()` for single doc, `.stream()` for collection iteration, `.where().order_by().stream()` for queries
- **Transactions**: `@firestore.transactional` decorator with explicit transaction object for conditional updates
- **`to_dict()`**: DocumentSnapshot deserialization; returns Python objects including `DatetimeWithNanoseconds`, `GeoPoint`, `DocumentReference`

---

## Firestore Python SDK

Package: `google-cloud-firestore` (`google.cloud.firestore`)

```python
from google.cloud import firestore

# Async client — used by this adapter for consistency with Cloud Run adapter
db = firestore.AsyncClient(project=project, database=database)

# Common patterns
col_ref = db.collection("runs")
doc_ref = col_ref.document("abc123")
snapshot = await doc_ref.get()           # DocumentSnapshot
data = snapshot.to_dict()                # dict | None
async for snap in col_ref.stream():      # iterate documents
    data = snap.to_dict()
async for subcol in doc_ref.collections():  # subcollections
    print(subcol.id)
```

**Key serialization types returned by `to_dict()`:**
- `DatetimeWithNanoseconds` — serialize with `.isoformat()`
- `google.cloud.firestore_v1.base_document.DocumentReference` — serialize as `.path`
- `google.type.latlng_pb2.LatLng` / `GeoPoint` — serialize as `{"latitude": ..., "longitude": ...}`
- `bytes` — serialize as `base64` or skip with note

---

## GapHunter Traps to absorb

**Trap FS-1 — Native mode vs Datastore mode**  
Firestore has two modes: Native (supports subcollections, real-time listeners) and Datastore (legacy). Using `firestore.Client` against a Datastore-mode database fails with confusing errors. Native mode is required for subcollection operations.  
Adapter response: emit an informational note in `list_collections` output about the database mode; check for `google.api_core.exceptions.FailedPrecondition` and surface the fix: `gcloud firestore databases describe --database=(default)`.

**Trap FS-2 — `(default)` database ID is not obvious**  
Most Firestore examples omit the `database` parameter, defaulting to `(default)`. Projects using named databases (supported since 2023) must pass the database ID explicitly or all operations hit the wrong database silently.  
Adapter response: `database` parameter defaults to `"(default)"` with a note in the docstring; `list_collections` prints which database is being queried.

**Trap FS-3 — Document paths must be even-segment**  
Firestore paths alternate collection/document segments. `runs` is a collection (1 segment); `runs/abc` is a document (2 segments); `runs/abc/events` is a subcollection (3 segments); `runs/abc/events/000001` is a document (4 segments). Using an odd-segment path where a document is expected raises a cryptic "path must point to a document" error.  
Adapter response: `get_document` and mutating operations accept explicit `collection` and `document_id` parameters (never a raw path string) to prevent segment confusion.

**Trap FS-4 — `DatetimeWithNanoseconds` breaks JSON serialization**  
`snapshot.to_dict()` returns `DatetimeWithNanoseconds` objects. These are not JSON-serializable. Any code that passes Firestore output to `json.dumps()` without a custom serializer raises `TypeError: Object of type DatetimeWithNanoseconds is not JSON serializable`.  
Adapter response: `_serialize_doc(data)` recursively converts all non-JSON-safe Firestore types before returning.

**Trap FS-5 — Agent Engine IAM for Firestore writes**  
When Agent Engine (`service-{project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com`) writes run state to Firestore, it needs `roles/datastore.user`. Without it: `PERMISSION_DENIED: Missing or insufficient permissions`. The error gives no hint about which service account is the culprit.  
Adapter response: `list_collections` and `get_service_account_hint` surface the expected service account format. The `PERMISSION_DENIED` error handler prints the remediation:
```
gcloud projects add-iam-policy-binding PROJECT \
  --member=serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com \
  --role=roles/datastore.user
```

**Trap FS-6 — `stream()` vs `get()` on collections**  
`collection.get()` returns all documents as a list (loads all into memory, blocks until done). `collection.stream()` is an async generator (streams, memory-safe). LLMs and many examples use `.get()` on large collections, causing OOM or timeouts.  
Adapter response: always use `.stream()` with a `limit` parameter via `.limit(n)` on queries.

**Trap FS-7 — Subcollections are invisible from the root**  
`db.collections()` only returns root-level collections. It does NOT list `runs/{id}/events` or `runs/{id}/sources`. LLMs frequently try to discover the full schema via `list_collections` and miss all subcollections.  
Adapter response: `list_subcollections(project, database, collection, document_id)` exposes subcollection discovery on a specific document. `list_collections` docstring warns explicitly that subcollections are not returned.

**Trap FS-8 — `update()` fails on non-existent documents**  
Firestore's `.update()` raises `NotFound` if the document doesn't exist. `.set()` creates or replaces. LLMs often use `update_document` to create documents and get a confusing 404.  
Adapter response: `update_document` pre-flight checks document existence; if not found, suggests using `set_document` instead.

---

## Operations in scope

| Operation | Type | Undo |
|---|---|---|
| `list_collections(project, database)` | inquire | — |
| `list_documents(project, database, collection, limit, fields)` | inquire | — |
| `get_document(project, database, collection, document_id)` | inquire | — |
| `query_documents(project, database, collection, filters, order_by, limit)` | inquire | — |
| `list_subcollections(project, database, collection, document_id)` | inquire | — |
| `set_document(project, database, collection, document_id, data)` | execute | `set_document` with prior data; `delete_document` if didn't exist |
| `update_document(project, database, collection, document_id, updates)` | execute | `set_document` with full snapshot |
| `delete_document(project, database, collection, document_id)` | execute | `set_document` with snapshot |

---

## Interface exposed to the code tool

```python
# ── inquire ────────────────────────────────────────────────────────────────

async def list_collections(project: str, database: str = "(default)") -> list[str]:
    """List root-level collections. Does NOT return subcollections (use list_subcollections)."""

async def list_documents(
    project: str,
    database: str = "(default)",
    collection: str = "",
    limit: int = 50,
    fields: list[str] | None = None,
) -> list[dict]:
    """List documents in a collection. Returns id + selected fields (all if fields=None)."""

async def get_document(
    project: str, database: str, collection: str, document_id: str
) -> dict | None:
    """Get a single document. Returns None if not found."""

async def query_documents(
    project: str,
    database: str = "(default)",
    collection: str = "",
    filters: list[tuple[str, str, Any]] | None = None,
    order_by: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query documents with equality/comparison filters.
    filters: [("field", "==", value), ("field", ">", value), ...]
    """

async def list_subcollections(
    project: str, database: str, collection: str, document_id: str
) -> list[str]:
    """List subcollection IDs under a specific document."""

# ── execute ────────────────────────────────────────────────────────────────

async def set_document(
    project: str,
    database: str = "(default)",
    collection: str = "",
    document_id: str = "",
    data: dict = {},
) -> dict:
    """Create or replace a document. Snapshots prior state for undo.
    If document doesn't exist, undo = delete_document.
    If document exists, undo = set_document with prior data.
    """

async def update_document(
    project: str,
    database: str = "(default)",
    collection: str = "",
    document_id: str = "",
    updates: dict = {},
) -> dict:
    """Partially update an existing document. Fails if document doesn't exist (use set_document).
    Undo = set_document with full pre-update snapshot.
    """

async def delete_document(
    project: str,
    database: str = "(default)",
    collection: str = "",
    document_id: str = "",
) -> dict:
    """Delete a document. Snapshots for undo. Undo = set_document with snapshot."""
```

---

## Serialization helper

```python
def _serialize_doc(data: Any) -> Any:
    """Recursively convert Firestore-specific types to JSON-safe equivalents."""
    if isinstance(data, dict):
        return {k: _serialize_doc(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize_doc(v) for v in data]
    if hasattr(data, "isoformat"):          # datetime / DatetimeWithNanoseconds
        return data.isoformat()
    if hasattr(data, "latitude"):           # GeoPoint
        return {"latitude": data.latitude, "longitude": data.longitude}
    if hasattr(data, "path"):               # DocumentReference
        return data.path
    if isinstance(data, bytes):
        return "<bytes>"
    return data
```

---

## Safety stack per operation

### `set_document`
1. **Pre-flight**: ADC check; attempt `get_document` to capture prior state
2. **Snapshot**: prior document data (or `None` if new)
3. **Execute**: `doc_ref.set(data)`
4. **Undo**: if prior state was `None` → `delete_document`; else → `set_document(prior_data)`

### `update_document`
1. **Pre-flight**: ADC check; `get_document` — if `None`, raise with suggestion to use `set_document` (Trap FS-8)
2. **Snapshot**: full prior document
3. **Execute**: `doc_ref.update(updates)`
4. **Undo**: `set_document(snapshot)`

### `delete_document`
1. **Pre-flight**: ADC check; `get_document` — if `None`, raise (nothing to delete)
2. **Snapshot**: full document data
3. **Execute**: `doc_ref.delete()`
4. **Undo**: `set_document(snapshot)` (recreate)

---

## Files to create

```
g_code_mode/
  adapters/
    firestore/
      __init__.py
      service.py      # all operations
      types.py        # FirestoreExecuteResult
tests/
  adapters/
    test_firestore.py
```

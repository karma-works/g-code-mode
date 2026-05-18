# Implementation Plan: Cloud Storage (GCS) Adapter

**Status:** Draft — 2026-05-18  
**Scope:** GCS adapter for g-code-mode — bucket inspection, object operations, IAM, lifecycle, versioning. Full five-layer safety stack on all mutating operations.

---

## Pain points driving this adapter

Sourced from developer forums, gsutil GitHub issues, and GCP documentation footguns. Ordered by blast radius.

| ID | Name | Reversible? |
|---|---|---|
| GCS-1 | `gsutil rsync -d` wipes destination on source error | Only with versioning/soft delete pre-enabled |
| GCS-2 | `blob.upload_from_filename()` silently overwrites — no guard | Only with versioning pre-enabled |
| GCS-3 | Retention policy lock — permanent, no grace period | No |
| GCS-4 | Uniform bucket-level access breaks legacy IAM silently; permanent after 90 days | Partial (within 90 days) |
| GCS-5 | Lifecycle policy changes take up to 24h to propagate — old rule fires in the gap | Objects already deleted: no |
| GCS-6 | Soft delete doesn't protect against project deletion; bucket restore ≠ object restore | Project delete: no |
| GCS-7 | ADC silently resolves to wrong project/credentials | Depends on ops executed |
| GCS-8 | `allUsers` IAM grant exposes entire bucket publicly; bots scan within minutes | Grant: removable. Exfiltration: irreversible |
| GCS-9 | Versioning without lifecycle policy = unbounded silent cost growth | Cost: no. Noncurrent objects: deletable |
| GCS-10 | Coldline/Archive early deletion penalty + per-read retrieval fee invisible at write time | Fees incurred: no |

---

## Python SDK

Package: `google-cloud-storage` (`google.cloud.storage`)

```python
from google.cloud import storage

client = storage.Client(project=project)
bucket = client.bucket(bucket_name)
blob = bucket.blob(object_path)
```

**Key types:**
- `storage.Client` — project-scoped entry point
- `storage.Bucket` — bucket resource + IAM + lifecycle + versioning config
- `storage.Blob` — object resource; upload/download/copy/delete
- `storage.IAMConfiguration` — uniform vs fine-grained access config
- `BucketNotFound` / `NotFound` — distinct errors for bucket vs object

---

## Operations in scope

### Inquire (read-only)

| Operation | Returns |
|---|---|
| `list_buckets(project)` | name, location, storage_class, versioning, retention_policy, uniform_access, public_access_prevention |
| `get_bucket(project, bucket_name)` | full config: IAM, lifecycle rules, CORS, versioning, soft_delete_retention, labels |
| `list_objects(project, bucket_name, prefix, max_results)` | name, size, content_type, updated, storage_class, generation |
| `get_object_metadata(project, bucket_name, object_path)` | full metadata + generation + metageneration |
| `get_bucket_iam(project, bucket_name)` | bindings (principal → roles), highlights allUsers and allAuthenticatedUsers |

### Execute (mutating)

| Operation | Type | Undo action |
|---|---|---|
| `upload_object(project, bucket_name, object_path, content, content_type, if_not_exists)` | execute | `delete_object` or restore noncurrent version |
| `delete_object(project, bucket_name, object_path, generation)` | execute | restore noncurrent version (if versioning) or warn irreversible |
| `copy_object(project, src_bucket, src_path, dst_bucket, dst_path, if_not_exists)` | execute | `delete_object` on destination |
| `set_bucket_iam(project, bucket_name, bindings)` | execute | `set_bucket_iam` with snapshotted bindings |
| `set_lifecycle_policy(project, bucket_name, rules)` | execute | `set_lifecycle_policy` with prior rules + warn on 24h propagation lag |
| `enable_versioning(project, bucket_name)` | execute | `disable_versioning` |
| `set_uniform_bucket_access(project, bucket_name, enabled)` | execute | revert if <90 days; else block with explanation |

**Explicitly out of scope (too dangerous or wrong tool):**

- `lock_retention_policy` — permanent, irreversible. The adapter will refuse to execute this and explain why. Users must use `gcloud storage buckets update --lock-retention-policy` directly with full understanding.
- `gsutil rsync` — the rsync-wipes-destination bug (GCS-1) is a gsutil CLI issue; the adapter does not wrap gsutil subprocess calls. Object copies are done via SDK `blob.copy_to()` with explicit dry-run listing first.
- Project-level delete — out of scope for all adapters.

---

## Traps baked into implementation

### Trap GCS-1 — `rsync -d` wipes on source error
The adapter has no `rsync` operation. If bulk copy is needed, `copy_object` calls are explicit and individually confirmed. Never wrap `gsutil rsync`.

### Trap GCS-2 — Silent overwrite on upload
`upload_object` always checks `if_not_exists` flag:
- Default: `if_not_exists=False` — proceeds with overwrite, but **snapshots the existing object's generation** and registers an undo that restores it.
- `if_not_exists=True` — uses `if_generation_match=0` precondition; raises a clear error if object already exists.
- Pre-flight always calls `get_object_metadata` to detect an existing object and surface it: `"Warning: object already exists (generation=12345, size=4.2KB, updated=2026-05-10). This upload will overwrite it."`

### Trap GCS-3 — Retention policy lock
`set_lifecycle_policy` and any bucket config operation checks whether a retention policy lock exists. If locked, all mutating operations on that bucket return an error with an explanation:
```
RetentionPolicyLocked: This bucket has a locked retention policy (duration=7y).
Objects cannot be deleted until they are 7 years old.
Bucket deletion is blocked until all objects age out.
This lock is permanent and cannot be removed.
```
The adapter never exposes `lock_retention_policy`. Doc comment explains why.

### Trap GCS-4 — Uniform bucket-level access silently breaks legacy IAM
`set_uniform_bucket_access(enabled=True)` pre-flight:
1. Reads current `iam_configuration.bucket_policy_only.enabled` and `locked_time`.
2. If currently disabled → warn: "Enabling uniform bucket-level access will immediately disable all object-level ACLs. After 90 days it cannot be reverted. Current legacy bindings will stop being honored."
3. If already enabled for >90 days → block `set_uniform_bucket_access(enabled=False)` with: "Uniform bucket-level access was locked on {locked_time} and cannot be disabled."
4. Snapshot current IAM config before any change.

### Trap GCS-5 — Lifecycle change 24h propagation lag
`set_lifecycle_policy` appends to its response:
```
Warning: Lifecycle changes can take up to 24 hours to propagate.
During this window, the previous rules may still fire.
Objects at risk during this window: <list objects matching old rule that are NOT yet protected by new rule>
```
Pre-flight identifies objects in the danger zone by listing objects that match the old deletion rule but would be protected by the new rule.

### Trap GCS-6 — Bucket restore ≠ object restore
`get_bucket` surfaces `soft_delete_retention` status. Any delete operation response includes:
```
Undo: bucket has soft delete enabled (7-day window).
To restore: first restore the bucket, then separately restore each object:
  gcloud storage restore gs://bucket_name/object_path --generation=12345
```
The undo recipe explicitly names both steps.

### Trap GCS-7 — ADC resolves to wrong project
At startup, GCS adapter resolves and logs the active ADC credential identity and the project it maps to. Every mutating operation includes the resolved identity in the pre-flight check output:
```
Pre-flight: operating as service-account@project.iam.gserviceaccount.com on project my-project-id
```

### Trap GCS-8 — `allUsers` IAM grant
`set_bucket_iam` pre-flight scans proposed bindings:
- If `allUsers` or `allAuthenticatedUsers` appears as a principal → hard block with:
```
Blocked: proposed bindings include allUsers (roles/storage.objectViewer).
This would make every object in gs://bucket-name publicly readable on the internet.
Automated scanners index public buckets within minutes.
To serve a single public file, use generate_signed_url() instead.
To proceed, pass allow_public_access=True explicitly.
```
- `allow_public_access=True` bypass requires explicit opt-in.
- `get_bucket_iam` output always highlights `allUsers`/`allAuthenticatedUsers` bindings with a `[PUBLIC]` tag.

### Trap GCS-9 — Versioning without lifecycle = unbounded cost
`enable_versioning` response always appends:
```
Warning: versioning enabled without a noncurrent version expiry lifecycle rule.
Every overwrite accumulates permanently at full storage cost.
Recommended: add a lifecycle rule to expire noncurrent versions after N days.
Example rule added automatically unless you pass add_noncurrent_expiry=False.
```
Default: `enable_versioning` also creates a 30-day noncurrent expiry lifecycle rule unless `add_noncurrent_expiry=False` is passed.

### Trap GCS-10 — Storage class retrieval fees invisible at write time
`upload_object` and `copy_object` to a bucket with storage class Coldline or Archive emit:
```
Warning: target bucket storage class is COLDLINE.
Minimum storage duration: 90 days. Early delete/overwrite billed as 90 days.
Retrieval fee: $0.01/GB on every read.
For high-read objects, STANDARD is cheaper. Use Autoclass to let GCS decide automatically.
```

---

## Interface exposed to the code tool

```python
# ── inquire ────────────────────────────────────────────────────────────────

async def list_buckets(project: str) -> list[dict]:
    """List all GCS buckets in the project. Returns name, location, storage_class,
    versioning_enabled, has_retention_policy, uniform_access, public_access_prevention."""

async def get_bucket(project: str, bucket_name: str) -> dict:
    """Full bucket config: IAM summary, lifecycle rules, CORS, versioning, soft_delete,
    retention_policy (locked/unlocked), labels, storage_class, location."""

async def list_objects(
    project: str,
    bucket_name: str,
    prefix: str = "",
    max_results: int = 100,
) -> list[dict]:
    """List objects under prefix. Returns name, size_bytes, content_type, updated,
    storage_class, generation. Truncates at max_results with guidance."""

async def get_object_metadata(
    project: str,
    bucket_name: str,
    object_path: str,
) -> dict:
    """Full object metadata: size, content_type, generation, metageneration, updated,
    storage_class, md5_hash, crc32c."""

async def get_bucket_iam(project: str, bucket_name: str) -> dict:
    """IAM bindings for the bucket. Tags allUsers/allAuthenticatedUsers with [PUBLIC].
    Returns bindings list and a has_public_access boolean."""

# ── execute ────────────────────────────────────────────────────────────────

async def upload_object(
    project: str,
    bucket_name: str,
    object_path: str,
    content: str | bytes,
    content_type: str = "application/octet-stream",
    if_not_exists: bool = False,
) -> dict:
    """Upload content to object_path. Snapshots existing object if present.
    Returns generation, undo_recipe, warnings (storage class, overwrite)."""

async def delete_object(
    project: str,
    bucket_name: str,
    object_path: str,
    generation: int | None = None,
) -> dict:
    """Delete an object. Returns undo_recipe (restore command if soft delete active,
    warning if irreversible). Specific generation required to delete a noncurrent version."""

async def copy_object(
    project: str,
    src_bucket: str,
    src_path: str,
    dst_bucket: str,
    dst_path: str,
    if_not_exists: bool = True,
) -> dict:
    """Copy object to new location. Default: if_not_exists=True prevents silent overwrite.
    Returns undo_recipe to delete the copy."""

async def set_bucket_iam(
    project: str,
    bucket_name: str,
    bindings: list[dict],   # [{"role": "roles/storage.objectViewer", "members": ["user:x@y.com"]}]
    allow_public_access: bool = False,
) -> dict:
    """Replace bucket IAM bindings. Blocks allUsers unless allow_public_access=True.
    Returns undo_recipe to restore prior bindings."""

async def set_lifecycle_policy(
    project: str,
    bucket_name: str,
    rules: list[dict],
) -> dict:
    """Set lifecycle rules. Warns on 24h propagation lag and identifies objects at risk
    in the gap. Returns undo_recipe with prior rules."""

async def enable_versioning(
    project: str,
    bucket_name: str,
    add_noncurrent_expiry: bool = True,
    noncurrent_expiry_days: int = 30,
) -> dict:
    """Enable object versioning. By default also adds a 30-day noncurrent version
    expiry lifecycle rule to prevent unbounded cost growth."""

async def set_uniform_bucket_access(
    project: str,
    bucket_name: str,
    enabled: bool,
) -> dict:
    """Enable or disable uniform bucket-level access. Blocks disable if locked (>90 days).
    Warns on legacy ACL impact before enabling. Returns undo_recipe."""
```

---

## Safety stack per operation

### `upload_object`

1. **Pre-flight**
   - ADC check + project identity log (Trap GCS-7)
   - `get_object_metadata` → if exists, surface name/size/updated and warn overwrite (Trap GCS-2)
   - If `if_not_exists=True`, set `if_generation_match=0`
   - Check bucket storage class → warn on Coldline/Archive (Trap GCS-10)
   - Check retention policy lock → block if locked and object age < retention period (Trap GCS-3)

2. **Snapshot** — existing object generation + metadata (for undo)

3. **Execute** — `blob.upload_from_string(content, if_generation_match=...)`

4. **Undo** — if object previously existed: `blob.copy_to()` from noncurrent version or `blob.upload_from_string()` with snapshotted content (if <1MB); else `delete_object`

5. **Result** — new generation, url, undo_recipe, warnings

### `delete_object`

1. **Pre-flight**
   - ADC check
   - `get_object_metadata` to confirm object exists and capture generation
   - Check bucket versioning + soft_delete status → determine undo recipe tier
   - If no versioning and no soft delete → warn: "This deletion is permanent. No undo available."

2. **Snapshot** — generation, size, content_type, metadata

3. **Execute** — `blob.delete(if_generation_match=generation)`

4. **Undo** — if soft delete active: `gcloud storage restore` command in undo_recipe; if no recovery path: explicit "irreversible" marker

### `set_bucket_iam`

1. **Pre-flight**
   - ADC check
   - Scan proposed bindings for `allUsers`/`allAuthenticatedUsers` → block unless `allow_public_access=True` (Trap GCS-8)
   - `get_bucket_iam` → snapshot current bindings

2. **Snapshot** — current IAM policy (etag + bindings)

3. **Execute** — `bucket.set_iam_policy(policy)` with etag to prevent concurrent modification

4. **Undo** — `set_bucket_iam` with snapshotted policy

### `set_lifecycle_policy`

1. **Pre-flight**
   - ADC check
   - Check retention lock (Trap GCS-3)
   - `get_bucket` → snapshot current lifecycle rules
   - Identify objects matching old delete rule but protected by new rule → surface in warning (Trap GCS-5)

2. **Snapshot** — current lifecycle rules

3. **Execute** — `bucket.lifecycle_rules = rules; bucket.patch()`

4. **Undo** — `set_lifecycle_policy` with snapshotted rules

5. **Result** — new rules, at-risk objects list, 24h warning, undo_recipe

---

## Implementation steps

| Step | What | Files |
|---|---|---|
| 1 | Add `google-cloud-storage` to `pyproject.toml` | `pyproject.toml` |
| 2 | `list_buckets` + `get_bucket` — read-only with full config | `adapters/gcs/service.py` |
| 3 | `list_objects` + `get_object_metadata` | `adapters/gcs/service.py` |
| 4 | `get_bucket_iam` with `[PUBLIC]` tagging | `adapters/gcs/service.py` |
| 5 | `upload_object` with overwrite guard + storage class warning | `adapters/gcs/service.py` |
| 6 | `delete_object` with soft delete / versioning undo logic | `adapters/gcs/service.py` |
| 7 | `copy_object` with if_not_exists default | `adapters/gcs/service.py` |
| 8 | `set_bucket_iam` with allUsers block | `adapters/gcs/service.py` |
| 9 | `set_lifecycle_policy` with at-risk object detection | `adapters/gcs/service.py` |
| 10 | `enable_versioning` with auto-noncurrent-expiry rule | `adapters/gcs/service.py` |
| 11 | `set_uniform_bucket_access` with 90-day lock check | `adapters/gcs/service.py` |
| 12 | Register adapter in `server.py` namespace | `server.py` |
| 13 | Unit tests — all 10 traps + undo recipes | `tests/adapters/test_gcs.py` |
| 14 | Update `code` tool description with GCS functions | `server.py` |
| 15 | Update `README.md` adapter table | `README.md` |

---

## Files to create

```
g_code_mode/
  adapters/
    gcs/
      __init__.py
      service.py      # all operations
      types.py        # BucketDetail, ObjectMetadata, IAMBinding, LifecycleRule
tests/
  adapters/
    test_gcs.py
```

---

## Operations explicitly refused

| Operation | Why refused |
|---|---|
| `lock_retention_policy` | Permanent, irreversible. Users must use gcloud CLI directly with full understanding. The adapter surfaces the lock status in `get_bucket` but never sets it. |
| `delete_bucket` | Too high blast radius. Users must confirm via gcloud CLI. The adapter blocks bucket delete requests with an explanation. |
| `gsutil rsync` | GCS-1 wipe-on-source-error bug. All bulk operations use explicit per-object SDK calls. |

---

## Open questions

1. **Signed URL generation** — should `get_object_metadata` offer a time-limited signed URL as a safe alternative to `allUsers`? Recommendation: yes, add `generate_signed_url(project, bucket_name, object_path, expiration_minutes)` as an inquire-adjacent utility. Avoids the `allUsers` trap entirely for single-file sharing.

2. **Multi-region vs dual-region buckets** — should `list_buckets` expose replication config? Recommendation: yes, surface `location_type` (REGION / DUAL_REGION / MULTI_REGION) in `get_bucket`. Affects disaster recovery guarantees the LLM should know about.

3. **Autoclass** — buckets with Autoclass enabled automatically move objects between storage classes. Should `upload_object` skip the Coldline/Archive warning if Autoclass is active? Recommendation: yes — if `bucket.autoclass_enabled` is True, suppress storage class warnings and surface Autoclass status instead.

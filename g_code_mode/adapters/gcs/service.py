"""
Cloud Storage (GCS) adapter — bucket inspection, object operations, IAM, lifecycle, versioning.

The google-cloud-storage SDK is synchronous. Methods are async def for interface
compatibility with the g-code-mode adapter pattern; GCS calls run on the calling thread.

Traps absorbed:
  GCS-1  gsutil rsync -d wipes destination on source error — no rsync, SDK-only per-object calls
  GCS-2  blob.upload_from_string() silently overwrites — if_generation_match precondition
  GCS-3  Retention policy lock — permanent; lock_bucket refused; lock status always surfaced
  GCS-4  Uniform bucket-level access breaks legacy IAM; permanent after 90 days
  GCS-5  Lifecycle changes take up to 24h to propagate — old rules still fire in gap
  GCS-6  Soft delete ≠ project delete protection; bucket restore ≠ object restore
  GCS-7  ADC resolves to wrong project — identity surfaced in every mutating pre-flight
  GCS-8  allUsers IAM grant exposes entire bucket publicly — blocked by default
  GCS-9  Versioning without lifecycle = unbounded cost — auto noncurrent expiry added by default
  GCS-10 Coldline/Archive retrieval fees — warned at write time
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from g_code_mode.adapters.gcs.types import GCSExecuteResult
from g_code_mode.preflight import require_adc
from g_code_mode.undo_registry import UndoRecipe

if TYPE_CHECKING:
    from g_code_mode.state import StateManager

_COLD_CLASSES = frozenset({"COLDLINE", "ARCHIVE"})
_PUBLIC_MEMBERS = frozenset({"allUsers", "allAuthenticatedUsers"})

_RETRIEVAL_WARNINGS: dict[str, str] = {
    "COLDLINE": (
        "Target bucket storage class is COLDLINE. "
        "Minimum storage duration: 90 days (early delete billed as 90 days). "
        "Retrieval fee: $0.01/GB on every read. "
        "For high-read objects, STANDARD is cheaper."
    ),
    "ARCHIVE": (
        "Target bucket storage class is ARCHIVE. "
        "Minimum storage duration: 365 days (early delete billed as 365 days). "
        "Retrieval fee: $0.05/GB on every read. "
        "Only suitable for disaster-recovery data accessed less than once per year."
    ),
}

_LIFECYCLE_LAG_WARNING = (
    "Lifecycle changes can take up to 24 hours to propagate. "
    "During this window, the previous rules may still fire. "
    "Any objects that would be deleted by the old rule are still at risk "
    "until propagation completes."
)


def _resolve_adc_identity() -> str:
    """Return the resolved ADC identity for pre-flight logging (Trap GCS-7)."""
    try:
        import google.auth  # type: ignore[import-untyped]

        credentials, project = google.auth.default()
        sa = getattr(credentials, "service_account_email", None)
        if sa:
            return f"{sa} (project={project})"
        return f"user credentials (project={project})"
    except Exception:
        return "unresolved"


def _bucket_summary(b: Any) -> dict[str, Any]:
    return {
        "name": b.name,
        "location": b.location,
        "location_type": getattr(b, "location_type", "unknown"),
        "storage_class": b.storage_class,
        "versioning_enabled": bool(b.versioning_enabled),
        "has_retention_policy": b.retention_period is not None,
        "retention_policy_locked": bool(b.retention_policy_locked),
        "autoclass_enabled": bool(getattr(b, "autoclass_enabled", False)),
    }


def _bucket_detail(b: Any) -> dict[str, Any]:
    iam_cfg = b.iam_configuration
    soft_delete = b.soft_delete_policy
    return {
        "name": b.name,
        "location": b.location,
        "location_type": getattr(b, "location_type", "unknown"),
        "storage_class": b.storage_class,
        "versioning_enabled": bool(b.versioning_enabled),
        "retention_period_seconds": b.retention_period,
        "retention_policy_locked": bool(b.retention_policy_locked),
        "retention_policy_effective_time": (
            str(b.retention_policy_effective_time) if b.retention_policy_effective_time else None
        ),
        "uniform_bucket_level_access_enabled": bool(
            iam_cfg.uniform_bucket_level_access_enabled
        ),
        "uniform_bucket_level_access_lock_time": (
            str(iam_cfg.uniform_bucket_level_access_lock_time)
            if iam_cfg.uniform_bucket_level_access_lock_time
            else None
        ),
        "public_access_prevention": getattr(iam_cfg, "public_access_prevention", "inherited"),
        "soft_delete_retention_seconds": getattr(
            soft_delete, "retention_duration_seconds", None
        ),
        "soft_delete_effective_time": (
            str(soft_delete.effective_time)
            if getattr(soft_delete, "effective_time", None)
            else None
        ),
        "lifecycle_rules": list(b.lifecycle_rules),
        "cors": list(b.cors),
        "labels": dict(b.labels),
        "autoclass_enabled": bool(getattr(b, "autoclass_enabled", False)),
    }


def _blob_to_dict(blob: Any) -> dict[str, Any]:
    return {
        "name": blob.name,
        "bucket": blob.bucket.name if hasattr(blob.bucket, "name") else str(blob.bucket),
        "size_bytes": blob.size,
        "content_type": blob.content_type,
        "updated": str(blob.updated) if blob.updated else None,
        "storage_class": blob.storage_class,
        "generation": blob.generation,
        "metageneration": blob.metageneration,
        "md5_hash": blob.md5_hash,
        "crc32c": blob.crc32c,
    }


def _iam_bindings(policy: Any) -> list[dict[str, Any]]:
    bindings = []
    for binding in policy.bindings:
        members = sorted(binding["members"])
        has_public = any(m in _PUBLIC_MEMBERS for m in members)
        bindings.append(
            {
                "role": binding["role"],
                "members": members,
                "public": has_public,
            }
        )
    return bindings


class GCSAdapter:
    """Cloud Storage operations with full g-code-mode safety stack."""

    def __init__(self, state: StateManager) -> None:
        self._state = state

    def _client(self, project: str) -> Any:
        from google.cloud import storage  # type: ignore[import-untyped]

        return storage.Client(project=project)

    # ── inquire (read-only) ────────────────────────────────────────────────

    async def list_buckets(self, project: str) -> list[dict[str, Any]]:
        """List all GCS buckets in the project with key config summary."""
        require_adc()
        client = self._client(project)
        return [_bucket_summary(b) for b in client.list_buckets()]

    async def get_bucket(self, project: str, bucket_name: str) -> dict[str, Any]:
        """Full bucket config: IAM summary, lifecycle rules, versioning, retention, soft delete."""
        require_adc()
        client = self._client(project)
        b = client.get_bucket(bucket_name)
        return _bucket_detail(b)

    async def list_objects(
        self,
        project: str,
        bucket_name: str,
        prefix: str = "",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List objects under prefix. Truncates at max_results with a note if hit."""
        require_adc()
        client = self._client(project)
        blobs = list(
            client.list_blobs(
                bucket_name,
                prefix=prefix or None,
                max_results=max_results,
            )
        )
        result = [_blob_to_dict(b) for b in blobs]
        if len(result) == max_results:
            result.append(
                {
                    "name": "--- TRUNCATED ---",
                    "note": (
                        f"Results truncated at {max_results}. "
                        "Use a more specific prefix or increase max_results."
                    ),
                }
            )
        return result

    async def get_object_metadata(
        self,
        project: str,
        bucket_name: str,
        object_path: str,
    ) -> dict[str, Any] | None:
        """Full object metadata. Returns None if object does not exist."""
        require_adc()
        client = self._client(project)
        bucket = client.bucket(bucket_name)
        blob = bucket.get_blob(object_path)
        return _blob_to_dict(blob) if blob is not None else None

    async def get_bucket_iam(self, project: str, bucket_name: str) -> dict[str, Any]:
        """
        IAM bindings for the bucket.

        Trap GCS-8: allUsers and allAuthenticatedUsers bindings are tagged with public=True.
        """
        require_adc()
        client = self._client(project)
        bucket = client.bucket(bucket_name)
        policy = bucket.get_iam_policy(requested_policy_version=3)
        bindings = _iam_bindings(policy)
        return {
            "bucket": bucket_name,
            "bindings": bindings,
            "has_public_access": any(b["public"] for b in bindings),
            "etag": policy.etag,
        }

    # ── execute (mutating) ─────────────────────────────────────────────────

    async def upload_object(
        self,
        project: str,
        bucket_name: str,
        object_path: str,
        content: str | bytes,
        content_type: str = "application/octet-stream",
        if_not_exists: bool = False,
    ) -> dict[str, Any]:
        """
        Upload content to object_path.

        Safety stack:
          1. Pre-flight: ADC identity (GCS-7), existing object check + overwrite warning (GCS-2),
             cold storage class warning (GCS-10), retention lock check (GCS-3)
          2. Snapshot: existing object metadata and generation
          3. Execute: upload with if_generation_match=0 precondition when if_not_exists=True
          4. Undo: delete new object if new; delete new generation if versioned; warn irreversible if not
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        bucket = client.get_bucket(bucket_name)

        # Trap GCS-3: retention lock blocks all mutations
        if bucket.retention_policy_locked:
            raise ValueError(
                f"RetentionPolicyLocked: bucket '{bucket_name}' has a locked retention policy "
                f"(period={bucket.retention_period}s). Objects cannot be overwritten or deleted "
                "until they age past the retention period. This lock is permanent."
            )

        # Trap GCS-10: cold storage class fee warning
        sc = bucket.storage_class or ""
        if sc in _COLD_CLASSES and not getattr(bucket, "autoclass_enabled", False):
            warnings.append(_RETRIEVAL_WARNINGS[sc])

        # Trap GCS-2: check for existing object to warn on overwrite
        existing_blob = bucket.get_blob(object_path)
        prior_metadata: dict[str, Any] | None = None
        prior_generation: int | None = None
        if existing_blob is not None:
            prior_metadata = _blob_to_dict(existing_blob)
            prior_generation = existing_blob.generation
            size_str = f"{existing_blob.size} bytes" if existing_blob.size else "unknown size"
            warnings.append(
                f"Object already exists (generation={prior_generation}, {size_str}, "
                f"updated={existing_blob.updated}). This upload will overwrite it."
            )

        precondition: dict[str, Any] = {}
        if if_not_exists:
            precondition["if_generation_match"] = 0

        op_id = self._state.create_operation(
            "upload_object",
            {
                "project": project,
                "bucket_name": bucket_name,
                "object_path": object_path,
                "content_type": content_type,
                "if_not_exists": if_not_exists,
                "prior_generation": prior_generation,
            },
        )
        self._state.set_snapshot(op_id, prior_metadata)

        upload_blob = bucket.blob(object_path)
        data = content.encode() if isinstance(content, str) else content
        try:
            upload_blob.upload_from_string(data, content_type=content_type, **precondition)
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            exc_str = str(exc)
            if "412" in exc_str or "PreconditionFailed" in exc_str or "if_generation_match" in exc_str:
                raise ValueError(
                    f"Object '{object_path}' already exists (generation={prior_generation}). "
                    "Upload rejected because if_not_exists=True. "
                    "Pass if_not_exists=False to overwrite."
                ) from exc
            raise RuntimeError(f"upload_object failed: {exc}") from exc

        upload_blob.reload()
        new_generation = upload_blob.generation
        versioning = bool(bucket.versioning_enabled)

        if prior_generation is None:
            undo_call = (
                f"await delete_object(project={project!r}, bucket_name={bucket_name!r}, "
                f"object_path={object_path!r}, generation={new_generation})"
            )
            undo_desc = (
                f"Delete new object gs://{bucket_name}/{object_path} "
                f"(generation={new_generation}) — it didn't exist before."
            )
        elif versioning:
            undo_call = (
                f"await delete_object(project={project!r}, bucket_name={bucket_name!r}, "
                f"object_path={object_path!r}, generation={new_generation})"
            )
            undo_desc = (
                f"Delete new generation ({new_generation}) of gs://{bucket_name}/{object_path} "
                f"to restore prior generation ({prior_generation})."
            )
        else:
            undo_call = "# Cannot undo: bucket has no versioning and prior content was overwritten."
            undo_desc = (
                f"IRREVERSIBLE: bucket '{bucket_name}' has no versioning. "
                f"Prior content (generation={prior_generation}) is permanently overwritten."
            )
            warnings.append(undo_desc)

        undo = UndoRecipe(description=undo_desc, call=undo_call)
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(
            op_id, "completed", {"object_path": object_path, "new_generation": new_generation}
        )

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot=prior_metadata,
            warnings=warnings,
            details={
                "object_path": object_path,
                "new_generation": new_generation,
                "size_bytes": len(data),
                "content_type": content_type,
                "url": f"gs://{bucket_name}/{object_path}",
            },
            op_id=op_id,
        ).to_dict()

    async def delete_object(
        self,
        project: str,
        bucket_name: str,
        object_path: str,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """
        Delete an object or a specific generation.

        Safety stack:
          1. Pre-flight: ADC identity (GCS-7), confirm object exists
          2. Snapshot: metadata + generation
          3. Execute: blob.delete()
          4. Undo: soft-delete restore command; or versioning note; or irreversible warning (GCS-6)
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        bucket = client.get_bucket(bucket_name)

        blob = bucket.get_blob(object_path, generation=generation)
        if blob is None:
            gen_hint = f" (generation={generation})" if generation else ""
            raise ValueError(
                f"Object 'gs://{bucket_name}/{object_path}'{gen_hint} does not exist."
            )

        prior_metadata = _blob_to_dict(blob)
        actual_generation = blob.generation
        versioning = bool(bucket.versioning_enabled)
        soft_delete_retention = getattr(
            bucket.soft_delete_policy, "retention_duration_seconds", None
        )
        has_soft_delete = bool(soft_delete_retention)

        # Trap GCS-6: surface undo path clearly
        if versioning:
            undo_desc = (
                f"Versioning is enabled. A delete marker was created. "
                f"Generation {actual_generation} is now noncurrent. "
                "To restore, delete the delete marker."
            )
            undo_call = (
                f"# Restore by deleting the delete marker (makes generation {actual_generation} live again):\n"
                f"gcloud storage rm gs://{bucket_name}/{object_path}#<delete_marker_generation>"
            )
        elif has_soft_delete:
            undo_desc = (
                f"Soft delete enabled (retention={soft_delete_retention}s). "
                "Object can be restored within the retention window."
            )
            undo_call = (
                f"gcloud storage restore gs://{bucket_name}/{object_path} "
                f"--generation={actual_generation}"
            )
        else:
            undo_desc = (
                f"IRREVERSIBLE: no versioning and no soft delete on '{bucket_name}'. "
                f"Object gs://{bucket_name}/{object_path} (generation={actual_generation}) "
                "is permanently deleted."
            )
            undo_call = "# Cannot undo: no versioning and no soft delete configured."
            warnings.append(
                "This deletion is PERMANENT. No versioning or soft delete is configured. "
                "There is no undo path."
            )

        op_id = self._state.create_operation(
            "delete_object",
            {
                "project": project,
                "bucket_name": bucket_name,
                "object_path": object_path,
                "generation": actual_generation,
            },
        )
        self._state.set_snapshot(op_id, prior_metadata)

        try:
            blob.delete()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"delete_object failed: {exc}") from exc

        undo = UndoRecipe(description=undo_desc, call=undo_call)
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed", {"object_path": object_path})

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot=prior_metadata,
            warnings=warnings,
            details={
                "object_path": object_path,
                "deleted_generation": actual_generation,
                "size_bytes": prior_metadata.get("size_bytes"),
                "reversible": versioning or has_soft_delete,
            },
            op_id=op_id,
        ).to_dict()

    async def copy_object(
        self,
        project: str,
        src_bucket: str,
        src_path: str,
        dst_bucket: str,
        dst_path: str,
        if_not_exists: bool = True,
    ) -> dict[str, Any]:
        """
        Copy object to a new location.

        Default: if_not_exists=True prevents silent overwrite of the destination (GCS-2).
        Trap GCS-10: warns if destination bucket is Coldline or Archive.
        Undo: delete the copy at the destination.
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        src_bkt = client.get_bucket(src_bucket)
        src_blob = src_bkt.get_blob(src_path)
        if src_blob is None:
            raise ValueError(f"Source object 'gs://{src_bucket}/{src_path}' does not exist.")

        dst_bkt = client.get_bucket(dst_bucket)

        # Trap GCS-10: cold destination
        dst_sc = dst_bkt.storage_class or ""
        if dst_sc in _COLD_CLASSES and not getattr(dst_bkt, "autoclass_enabled", False):
            warnings.append(_RETRIEVAL_WARNINGS[dst_sc])

        dst_blob = dst_bkt.get_blob(dst_path)
        if dst_blob is not None:
            if if_not_exists:
                raise ValueError(
                    f"Destination 'gs://{dst_bucket}/{dst_path}' already exists "
                    f"(generation={dst_blob.generation}, size={dst_blob.size} bytes). "
                    "Pass if_not_exists=False to overwrite."
                )
            warnings.append(
                f"Destination already exists (generation={dst_blob.generation}, "
                f"size={dst_blob.size} bytes). It will be overwritten."
            )

        op_id = self._state.create_operation(
            "copy_object",
            {
                "project": project,
                "src_bucket": src_bucket,
                "src_path": src_path,
                "dst_bucket": dst_bucket,
                "dst_path": dst_path,
            },
        )
        self._state.set_snapshot(op_id, {"src": _blob_to_dict(src_blob)})

        try:
            new_blob = src_bkt.copy_blob(src_blob, dst_bkt, dst_path)
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"copy_object failed: {exc}") from exc

        undo_call = (
            f"await delete_object(project={project!r}, bucket_name={dst_bucket!r}, "
            f"object_path={dst_path!r}, generation={new_blob.generation})"
        )
        undo = UndoRecipe(
            description=f"Delete the copy at gs://{dst_bucket}/{dst_path}",
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed", {"dst_path": dst_path})

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=dst_bucket,
            undo_recipe=undo.to_dict(),
            snapshot={"src": _blob_to_dict(src_blob)},
            warnings=warnings,
            details={
                "src": f"gs://{src_bucket}/{src_path}",
                "dst": f"gs://{dst_bucket}/{dst_path}",
                "new_generation": new_blob.generation,
            },
            op_id=op_id,
        ).to_dict()

    async def set_bucket_iam(
        self,
        project: str,
        bucket_name: str,
        bindings: list[dict[str, Any]],
        allow_public_access: bool = False,
    ) -> dict[str, Any]:
        """
        Replace all IAM bindings on a bucket.

        Trap GCS-8: blocks allUsers/allAuthenticatedUsers unless allow_public_access=True.
        Snapshot of prior policy captured for undo.
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        # Trap GCS-8: scan for public members before touching the API
        public_entries = [
            f"{m} → {b['role']}"
            for b in bindings
            for m in b.get("members", [])
            if m in _PUBLIC_MEMBERS
        ]
        if public_entries and not allow_public_access:
            raise ValueError(
                f"Blocked: proposed bindings include public members: {public_entries}.\n"
                f"This would make every object in gs://{bucket_name} publicly readable. "
                "Automated scanners index public buckets within minutes.\n"
                "To share a single file, use a time-limited signed URL instead.\n"
                "To proceed anyway, pass allow_public_access=True explicitly."
            )
        if public_entries:
            warnings.append(
                f"[PUBLIC] Binding includes public members: {public_entries}. "
                f"Bucket gs://{bucket_name} will be publicly accessible."
            )

        client = self._client(project)
        bucket = client.bucket(bucket_name)

        # Snapshot current policy before mutating
        prior_policy = bucket.get_iam_policy(requested_policy_version=3)
        prior_bindings = _iam_bindings(prior_policy)

        op_id = self._state.create_operation(
            "set_bucket_iam",
            {"project": project, "bucket_name": bucket_name},
        )
        self._state.set_snapshot(op_id, {"bindings": prior_bindings})

        # Fetch a fresh policy to get the current etag (prevents concurrent modification)
        new_policy = bucket.get_iam_policy(requested_policy_version=3)
        new_policy.bindings = [
            {"role": b["role"], "members": frozenset(b.get("members", []))}
            for b in bindings
        ]

        try:
            bucket.set_iam_policy(new_policy)
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"set_bucket_iam failed: {exc}") from exc

        undo_call = (
            f"await set_bucket_iam("
            f"project={project!r}, bucket_name={bucket_name!r}, "
            f"bindings={prior_bindings!r})"
        )
        undo = UndoRecipe(
            description=f"Restore IAM bindings for gs://{bucket_name}",
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed")

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot={"bindings": prior_bindings},
            warnings=warnings,
            details={"new_binding_count": len(bindings), "has_public_access": bool(public_entries)},
            op_id=op_id,
        ).to_dict()

    async def set_lifecycle_policy(
        self,
        project: str,
        bucket_name: str,
        rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Set lifecycle rules (full replacement).

        Trap GCS-3: blocked if retention policy is locked.
        Trap GCS-5: warns on 24h propagation lag — old rules may still fire during the gap.
        Snapshot of prior rules captured for undo.
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        b = client.get_bucket(bucket_name)

        # Trap GCS-3: retention lock
        if b.retention_policy_locked:
            raise ValueError(
                f"RetentionPolicyLocked: bucket '{bucket_name}' has a locked retention policy. "
                "Lifecycle configuration cannot be modified on a locked bucket."
            )

        prior_rules = list(b.lifecycle_rules)

        op_id = self._state.create_operation(
            "set_lifecycle_policy",
            {"project": project, "bucket_name": bucket_name},
        )
        self._state.set_snapshot(op_id, {"lifecycle_rules": prior_rules})

        try:
            b.lifecycle_rules = rules
            b.patch()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"set_lifecycle_policy failed: {exc}") from exc

        # Trap GCS-5: 24h propagation warning on every lifecycle change
        warnings.append(_LIFECYCLE_LAG_WARNING)

        undo_call = (
            f"await set_lifecycle_policy("
            f"project={project!r}, bucket_name={bucket_name!r}, "
            f"rules={prior_rules!r})"
        )
        undo = UndoRecipe(
            description=f"Restore prior lifecycle rules for gs://{bucket_name}",
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed")

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot={"lifecycle_rules": prior_rules},
            warnings=warnings,
            details={"new_rule_count": len(rules), "prior_rule_count": len(prior_rules)},
            op_id=op_id,
        ).to_dict()

    async def enable_versioning(
        self,
        project: str,
        bucket_name: str,
        add_noncurrent_expiry: bool = True,
        noncurrent_expiry_days: int = 30,
    ) -> dict[str, Any]:
        """
        Enable object versioning.

        Trap GCS-9: by default also adds a noncurrent version expiry lifecycle rule
        (age={noncurrent_expiry_days} days, isLive=False) to prevent unbounded cost growth.
        Pass add_noncurrent_expiry=False to suppress the automatic rule.
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        b = client.get_bucket(bucket_name)

        prior_versioning = bool(b.versioning_enabled)
        prior_rules = list(b.lifecycle_rules)

        if prior_versioning:
            warnings.append(f"Versioning is already enabled on bucket '{bucket_name}'.")

        op_id = self._state.create_operation(
            "enable_versioning",
            {
                "project": project,
                "bucket_name": bucket_name,
                "add_noncurrent_expiry": add_noncurrent_expiry,
                "noncurrent_expiry_days": noncurrent_expiry_days,
            },
        )
        self._state.set_snapshot(
            op_id, {"versioning_enabled": prior_versioning, "lifecycle_rules": prior_rules}
        )

        try:
            b.versioning_enabled = True
            if add_noncurrent_expiry and not prior_versioning:
                # Trap GCS-9: pair versioning with automatic noncurrent expiry
                noncurrent_rule: dict[str, Any] = {
                    "action": {"type": "Delete"},
                    "condition": {"isLive": False, "age": noncurrent_expiry_days},
                }
                b.lifecycle_rules = list(prior_rules) + [noncurrent_rule]
            b.patch()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"enable_versioning failed: {exc}") from exc

        if add_noncurrent_expiry and not prior_versioning:
            warnings.append(
                f"Versioning enabled. A {noncurrent_expiry_days}-day noncurrent version expiry "
                "lifecycle rule was added automatically to prevent unbounded cost growth "
                "(every overwrite would otherwise accumulate at full storage cost indefinitely). "
                "Pass add_noncurrent_expiry=False to suppress this rule."
            )

        undo_call = (
            f"# Disabling versioning does NOT delete existing noncurrent versions.\n"
            f"# They continue to be billed until explicitly deleted.\n"
            f"# To restore prior lifecycle rules:\n"
            f"# await set_lifecycle_policy(project={project!r}, bucket_name={bucket_name!r}, "
            f"rules={prior_rules!r})"
        )
        undo = UndoRecipe(
            description=(
                "Disable versioning (existing noncurrent versions persist and "
                "continue billing until explicitly deleted)."
            ),
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed")

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot={"versioning_enabled": prior_versioning, "lifecycle_rules": prior_rules},
            warnings=warnings,
            details={
                "versioning_enabled": True,
                "noncurrent_expiry_added": add_noncurrent_expiry and not prior_versioning,
                "noncurrent_expiry_days": noncurrent_expiry_days if add_noncurrent_expiry else None,
            },
            op_id=op_id,
        ).to_dict()

    async def set_uniform_bucket_access(
        self,
        project: str,
        bucket_name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """
        Enable or disable uniform bucket-level access.

        Trap GCS-4:
          - Enabling immediately disables all object-level ACLs and legacy IAM bindings.
          - After 90 consecutive days, the setting is permanently locked and cannot be disabled.
          - Disable is blocked if the lock_time is in the past.
        """
        require_adc()
        identity = _resolve_adc_identity()
        warnings: list[str] = [f"Pre-flight: operating as {identity} on project {project}"]

        client = self._client(project)
        b = client.get_bucket(bucket_name)

        iam_cfg = b.iam_configuration
        currently_enabled = bool(iam_cfg.uniform_bucket_level_access_enabled)
        lock_time = iam_cfg.uniform_bucket_level_access_lock_time
        now = datetime.now(tz=timezone.utc)

        # Trap GCS-4: block disable if the 90-day lock has passed
        if not enabled and currently_enabled and lock_time is not None:
            lock_dt = lock_time if hasattr(lock_time, "utcoffset") else lock_time.replace(tzinfo=timezone.utc)
            if now >= lock_dt:
                raise ValueError(
                    f"Blocked: uniform bucket-level access on '{bucket_name}' was locked on "
                    f"{lock_time} and cannot be disabled. "
                    "GCS permanently locks this setting after 90 consecutive days of enablement."
                )
            days_remaining = (lock_dt - now).days
            warnings.append(
                f"Warning: uniform bucket-level access will be permanently locked in "
                f"~{days_remaining} days (on {lock_time}). After that it cannot be disabled."
            )

        if enabled and not currently_enabled:
            warnings.append(
                "Enabling uniform bucket-level access will immediately disable all object-level ACLs. "
                "Any service accounts or users relying on legacy ACLs will lose access immediately. "
                "After 90 consecutive days this setting cannot be reverted."
            )

        op_id = self._state.create_operation(
            "set_uniform_bucket_access",
            {
                "project": project,
                "bucket_name": bucket_name,
                "enabled": enabled,
                "prior_enabled": currently_enabled,
            },
        )
        self._state.set_snapshot(
            op_id,
            {
                "uniform_bucket_level_access_enabled": currently_enabled,
                "lock_time": str(lock_time) if lock_time else None,
            },
        )

        try:
            b.iam_configuration.uniform_bucket_level_access_enabled = enabled
            b.patch()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"set_uniform_bucket_access failed: {exc}") from exc

        undo_call = (
            f"await set_uniform_bucket_access("
            f"project={project!r}, bucket_name={bucket_name!r}, enabled={currently_enabled!r})"
        )
        undo = UndoRecipe(
            description=(
                f"Restore uniform bucket-level access to {currently_enabled} for '{bucket_name}'"
            ),
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed")

        return GCSExecuteResult(
            success=True,
            project=project,
            bucket_name=bucket_name,
            undo_recipe=undo.to_dict(),
            snapshot={
                "uniform_bucket_level_access_enabled": currently_enabled,
                "lock_time": str(lock_time) if lock_time else None,
            },
            warnings=warnings,
            details={
                "uniform_bucket_level_access_enabled": enabled,
                "was_already_locked": lock_time is not None and now >= lock_time
                if lock_time
                else False,
            },
            op_id=op_id,
        ).to_dict()

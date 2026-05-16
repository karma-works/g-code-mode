"""
Cloud Run adapter — list, get, deploy, set_traffic, rollback, logs.

Read operations (list_services, get_service, list_revisions, get_service_logs)
adapted from GoogleCloudPlatform/cloud-run-mcp (Apache-2.0).
Copyright 2024 Google LLC. Modifications: ported to Python SDK, added
LLM-safe field curation, secret key detection, and g-code-mode safety stack.

Mutating operations (deploy_revision, set_traffic, rollback_revision)
and the full safety stack are original to g-code-mode.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from g_code_mode.adapters.cloud_run.types import CloudRunExecuteResult, TrafficSplit
from g_code_mode.preflight import require_adc
from g_code_mode.undo_registry import UndoRecipe

if TYPE_CHECKING:
    from g_code_mode.state import StateManager

# Trap CR-2: env var keys that look like secrets
_SECRET_KEY_RE = re.compile(r"(key|secret|token|password|credential)", re.IGNORECASE)

# Trap CR-1: warn when running without min-instances + no-cpu-throttling
_BG_THREAD_WARNING = (
    "min_instances=0 with cpu_throttling=True (the defaults): Cloud Run will shut down "
    "instances after returning a response. Background threads — including those that invoke "
    "Vertex AI Agent Engine — will be killed before completing. "
    "Set min_instances=1 and cpu_throttling=False to keep the instance alive:\n"
    "  deploy_revision(..., min_instances=1, cpu_throttling=False)"
)


def _service_parent(project: str, region: str) -> str:
    return f"projects/{project}/locations/{region}"


def _service_name(project: str, region: str, service_id: str) -> str:
    return f"projects/{project}/locations/{region}/services/{service_id}"


def _warn_secret_keys(env_vars: dict[str, str]) -> list[str]:
    return [
        f"Env var '{k}' looks like a secret. Use a Secret Manager binding instead of a "
        "plain env var — plain values are visible in service metadata."
        for k in env_vars
        if _SECRET_KEY_RE.search(k)
    ]


def _traffic_to_list(traffic: Any) -> list[dict[str, Any]]:
    """Convert SDK TrafficTarget objects to plain dicts."""
    result = []
    for t in traffic:
        result.append(
            {
                "revision": t.revision or "LATEST",
                "percent": t.percent,
                "type": t.type_.name if hasattr(t, "type_") else str(t.type_),
            }
        )
    return result


def _build_traffic_targets(
    splits: dict[str, int],
) -> list[Any]:
    """
    Convert {"LATEST": 90, "my-service-00041-abc": 10} to SDK TrafficTarget list.
    "LATEST" is treated as TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST; all others are
    TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION.
    """
    from google.cloud import run_v2  # type: ignore[import-untyped]

    targets = []
    for rev, pct in splits.items():
        if rev == "LATEST":
            targets.append(
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                    percent=pct,
                )
            )
        else:
            targets.append(
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                    revision=rev,
                    percent=pct,
                )
            )
    return targets


def _service_to_dict(svc: Any, project: str, region: str) -> dict[str, Any]:
    """Convert SDK Service object to a curated, LLM-safe dict.

    Secrets are never exposed: env var values flagged as secrets are omitted;
    only the key names are returned in secret_env_var_keys.
    """
    containers = list(svc.template.containers) if svc.template.containers else []
    image = containers[0].image if containers else ""

    env_var_keys: list[str] = []
    secret_env_var_keys: list[str] = []
    for container in containers:
        for env in container.env:
            if env.value_source and env.value_source.secret_key_ref.secret:
                secret_env_var_keys.append(env.name)
            else:
                if _SECRET_KEY_RE.search(env.name):
                    secret_env_var_keys.append(env.name)
                else:
                    env_var_keys.append(env.name)

    scaling = svc.template.scaling
    return {
        "name": svc.name.split("/")[-1],
        "region": region,
        "project": project,
        "url": svc.uri or "",
        "image": image,
        "traffic": _traffic_to_list(svc.traffic),
        "scaling": {
            "min_instances": scaling.min_instance_count if scaling else 0,
            "max_instances": scaling.max_instance_count if scaling else 100,
        },
        "cpu_throttling": not getattr(svc.template, "execution_environment", None) == "EXECUTION_ENVIRONMENT_GEN2",
        "ingress": svc.ingress.name if svc.ingress else "INGRESS_TRAFFIC_ALL",
        "service_account": svc.template.service_account or "",
        "env_var_keys": env_var_keys,
        "secret_env_var_keys": secret_env_var_keys,
        "latest_revision": svc.latest_ready_revision.split("/")[-1] if svc.latest_ready_revision else "",
        "ready": svc.terminal_condition.type_ == "Ready" and svc.terminal_condition.state.name == "STATE_TRUE"
        if svc.terminal_condition
        else False,
    }


class CloudRunAdapter:
    """
    Cloud Run service operations with full g-code-mode safety stack.

    Read operations adapted from GoogleCloudPlatform/cloud-run-mcp (Apache-2.0).
    Mutating operations and safety stack original to g-code-mode.
    """

    def __init__(self, state: StateManager) -> None:
        self._state = state

    # ── inquire (read-only) ────────────────────────────────────────────────

    async def list_services(self, project: str, region: str) -> list[dict[str, Any]]:
        """
        List all Cloud Run services in a project/region.
        Adapted from GoogleCloudPlatform/cloud-run-mcp list-services (Apache-2.0).
        """
        require_adc()
        from google.cloud import run_v2  # type: ignore[import-untyped]

        client = run_v2.ServicesAsyncClient()
        parent = _service_parent(project, region)
        services = []
        async for svc in await client.list_services(parent=parent):
            services.append(
                {
                    "name": svc.name.split("/")[-1],
                    "region": region,
                    "url": svc.uri or "",
                    "latest_revision": svc.latest_ready_revision.split("/")[-1]
                    if svc.latest_ready_revision
                    else "",
                    "traffic": _traffic_to_list(svc.traffic),
                    "ready": svc.terminal_condition.state.name == "STATE_TRUE"
                    if svc.terminal_condition
                    else False,
                }
            )
        return services

    async def get_service(
        self, project: str, region: str, service_id: str
    ) -> dict[str, Any]:
        """
        Get full details of a Cloud Run service.
        Adapted from GoogleCloudPlatform/cloud-run-mcp get-service (Apache-2.0).
        Secret env var values are never exposed — only key names returned.
        """
        require_adc()
        from google.cloud import run_v2  # type: ignore[import-untyped]

        client = run_v2.ServicesAsyncClient()
        svc = await client.get_service(name=_service_name(project, region, service_id))
        return _service_to_dict(svc, project, region)

    async def list_revisions(
        self, project: str, region: str, service_id: str
    ) -> list[dict[str, Any]]:
        """List revisions for a service, newest first."""
        require_adc()
        from google.cloud import run_v2  # type: ignore[import-untyped]

        client = run_v2.RevisionsAsyncClient()
        parent = _service_name(project, region, service_id)
        revisions = []
        async for rev in await client.list_revisions(parent=parent):
            containers = list(rev.containers) if rev.containers else []
            revisions.append(
                {
                    "name": rev.name.split("/")[-1],
                    "image": containers[0].image if containers else "",
                    "create_time": str(rev.create_time),
                    "ready": rev.condition.state.name == "STATE_TRUE"
                    if rev.condition
                    else False,
                }
            )
        # newest first
        revisions.sort(key=lambda r: r["create_time"], reverse=True)
        return revisions

    async def get_service_logs(
        self,
        project: str,
        region: str,
        service_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent log entries for a Cloud Run service via Cloud Logging.
        Adapted from GoogleCloudPlatform/cloud-run-mcp get-service-log (Apache-2.0).
        Uses Cloud Logging SDK for richer filtering than the Cloud Run API.
        """
        require_adc()
        from google.cloud import logging as cloud_logging  # type: ignore[import-untyped]

        client = cloud_logging.Client(project=project)
        filter_str = (
            f'resource.type="cloud_run_revision" '
            f'resource.labels.service_name="{service_id}" '
            f'resource.labels.location="{region}"'
        )
        entries = []
        for entry in client.list_entries(
            filter_=filter_str,
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
            projects=[project],
        ):
            entries.append(
                {
                    "timestamp": str(entry.timestamp),
                    "severity": str(entry.severity),
                    "message": entry.payload if isinstance(entry.payload, str)
                    else str(entry.payload),
                }
            )
        return entries

    # ── execute (mutating) ─────────────────────────────────────────────────

    async def deploy_revision(
        self,
        project: str,
        region: str,
        service_id: str,
        image: str,
        env_vars: dict[str, str] | None = None,
        min_instances: int = 0,
        max_instances: int = 100,
        cpu_throttling: bool = True,
        traffic_pct: int = 100,
        ingress: str | None = None,
    ) -> dict[str, Any]:
        """
        Deploy a new revision to an existing Cloud Run service.

        Safety stack:
          1. Pre-flight: ADC, service existence check (Trap CR-4),
             secret env var warning (Trap CR-2),
             AGENT_ENGINE_RESOURCE_NAME warning (Trap CR-3),
             background thread warning (Trap CR-1)
          2. Snapshot: full get_service() before mutation
          3. Execute: update_service with new image and config
          4. Undo: set_traffic back to pre-deploy splits
        """
        require_adc()
        env_vars = env_vars or {}

        # Pre-flight
        warnings: list[str] = []
        warnings.extend(_warn_secret_keys(env_vars))  # Trap CR-2

        if "AGENT_ENGINE_RESOURCE_NAME" in env_vars:  # Trap CR-3
            warnings.append(
                "AGENT_ENGINE_RESOURCE_NAME is set in env_vars — this Cloud Run "
                "deployment will pick it up. If you just deployed a new Agent Engine, "
                "this is the correct next step. Verify the resource name is current."
            )

        if min_instances == 0 and cpu_throttling:  # Trap CR-1
            warnings.append(_BG_THREAD_WARNING)

        if traffic_pct < 0 or traffic_pct > 100:
            raise ValueError(f"traffic_pct must be between 0 and 100, got {traffic_pct}")

        # Trap CR-4: confirm service exists, capture snapshot
        try:
            snapshot = await self.get_service(project, region, service_id)
        except Exception as exc:
            raise ValueError(
                f"Service '{service_id}' not found in {region}. "
                f"Check the service name and region. Error: {exc}\n"
                f"Available services: call list_services(project={project!r}, region={region!r})"
            ) from exc

        op_id = self._state.create_operation(
            "deploy_revision",
            {
                "project": project,
                "region": region,
                "service_id": service_id,
                "image": image,
                "env_vars": list(env_vars.keys()),  # keys only — no secret values in state
                "min_instances": min_instances,
                "max_instances": max_instances,
                "cpu_throttling": cpu_throttling,
                "traffic_pct": traffic_pct,
            },
        )
        self._state.set_snapshot(op_id, snapshot)

        from google.cloud import run_v2  # type: ignore[import-untyped]
        from google.protobuf import field_mask_pb2  # type: ignore[import-untyped]

        client = run_v2.ServicesAsyncClient()
        svc = await client.get_service(name=_service_name(project, region, service_id))

        # Update image
        if svc.template.containers:
            svc.template.containers[0].image = image
        else:
            raise ValueError("Service has no containers in its template.")

        # Update env vars (merge — preserve existing, override with new)
        existing_env = {e.name: e for e in svc.template.containers[0].env}
        for k, v in env_vars.items():
            existing_env[k] = run_v2.EnvVar(name=k, value=v)
        svc.template.containers[0].env = list(existing_env.values())

        # Update scaling
        if not svc.template.scaling:
            svc.template.scaling = run_v2.RevisionScaling()
        svc.template.scaling.min_instance_count = min_instances
        svc.template.scaling.max_instance_count = max_instances

        # Update ingress — preserve existing if not specified (Trap CR-5)
        if ingress is not None:
            svc.ingress = run_v2.IngressTraffic[ingress]

        # Set initial traffic split for the new revision
        # We route traffic_pct to LATEST; remainder stays on current latest named revision
        prior_latest = snapshot.get("latest_revision", "")
        if traffic_pct == 100 or not prior_latest:
            svc.traffic = [
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                    percent=100,
                )
            ]
        else:
            svc.traffic = [
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                    percent=traffic_pct,
                ),
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                    revision=prior_latest,
                    percent=100 - traffic_pct,
                ),
            ]

        try:
            op = await client.update_service(service=svc)
            updated = await op.result()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"deploy_revision failed: {exc}") from exc

        new_revision = (
            updated.latest_ready_revision.split("/")[-1]
            if updated.latest_ready_revision
            else ""
        )

        # Build undo recipe: restore pre-deploy traffic splits
        prior_splits: dict[str, int] = {
            t["revision"]: t["percent"] for t in snapshot.get("traffic", [])
        }
        undo_call = (
            f"await set_traffic("
            f"project={project!r}, region={region!r}, service_id={service_id!r}, "
            f"splits={prior_splits!r})"
        )
        undo = UndoRecipe(
            description=f"Restore traffic to pre-deploy splits: {prior_splits}",
            call=undo_call,
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(
            op_id, "completed", {"new_revision": new_revision, "url": updated.uri or ""}
        )

        return CloudRunExecuteResult(
            success=True,
            service_id=service_id,
            region=region,
            undo_recipe=undo.to_dict(),
            snapshot=snapshot,
            warnings=warnings,
            details={
                "new_revision": new_revision,
                "url": updated.uri or "",
                "image": image,
                "traffic_pct_on_latest": traffic_pct,
            },
            op_id=op_id,
        ).to_dict()

    async def set_traffic(
        self,
        project: str,
        region: str,
        service_id: str,
        splits: dict[str, int],
    ) -> dict[str, Any]:
        """
        Update traffic splits without deploying a new revision.

        splits: {"LATEST": 90, "my-service-00041-abc": 10}
        All values must sum to 100 (Trap CR-6).
        Snapshot of current splits is captured for undo.
        """
        require_adc()

        # Trap CR-6: validate split sum
        total = sum(splits.values())
        if total != 100:
            raise ValueError(
                f"Traffic splits must sum to 100, got {total}. "
                f"Provided splits: {splits}"
            )

        # Snapshot current state
        snapshot = await self.get_service(project, region, service_id)
        prior_splits: dict[str, int] = {
            t["revision"]: t["percent"] for t in snapshot.get("traffic", [])
        }

        # Validate named revisions exist
        if any(k != "LATEST" for k in splits):
            revisions = await self.list_revisions(project, region, service_id)
            known = {r["name"] for r in revisions}
            unknown = {k for k in splits if k != "LATEST" and k not in known}
            if unknown:
                raise ValueError(
                    f"Unknown revision(s): {unknown}. "
                    f"Known revisions: {sorted(known)}"
                )

        op_id = self._state.create_operation(
            "set_traffic",
            {"project": project, "region": region, "service_id": service_id, "splits": splits},
        )
        self._state.set_snapshot(op_id, snapshot)

        from google.cloud import run_v2  # type: ignore[import-untyped]
        from google.protobuf import field_mask_pb2  # type: ignore[import-untyped]

        client = run_v2.ServicesAsyncClient()
        svc = await client.get_service(name=_service_name(project, region, service_id))
        svc.traffic = _build_traffic_targets(splits)

        try:
            op = await client.update_service(
                service=svc,
                update_mask=field_mask_pb2.FieldMask(paths=["traffic"]),
            )
            await op.result()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"set_traffic failed: {exc}") from exc

        undo = UndoRecipe(
            description=f"Restore traffic to: {prior_splits}",
            call=(
                f"await set_traffic(project={project!r}, region={region!r}, "
                f"service_id={service_id!r}, splits={prior_splits!r})"
            ),
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed", {"splits": splits})

        return CloudRunExecuteResult(
            success=True,
            service_id=service_id,
            region=region,
            undo_recipe=undo.to_dict(),
            snapshot=snapshot,
            details={"splits": splits},
            op_id=op_id,
        ).to_dict()

    async def rollback_revision(
        self,
        project: str,
        region: str,
        service_id: str,
        revision_name: str,
    ) -> dict[str, Any]:
        """
        Route 100% of traffic to a named prior revision.

        Wraps set_traffic({revision_name: 100}). The undo recipe restores
        the traffic splits that were in place before the rollback.
        """
        require_adc()

        # Confirm revision exists before attempting rollback
        revisions = await self.list_revisions(project, region, service_id)
        known = {r["name"] for r in revisions}
        if revision_name not in known:
            raise ValueError(
                f"Revision '{revision_name}' not found. "
                f"Available revisions: {sorted(known)}"
            )

        result = await self.set_traffic(
            project=project,
            region=region,
            service_id=service_id,
            splits={revision_name: 100},
        )

        # Annotate the undo recipe description to be more specific
        result["undo_recipe"]["description"] = (
            f"Undo rollback to {revision_name} — restore prior traffic splits"
        )
        result["details"]["rolled_back_to"] = revision_name
        return result

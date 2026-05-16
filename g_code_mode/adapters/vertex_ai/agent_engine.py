"""
Vertex AI Agent Engine adapter.

Absorbs the 7 traps documented in specs/implementation-plan-vertex-ai-adapter.md.
Every mutating operation ships with a pre-flight, snapshot, undo recipe, and
state tracking entry (SQLite via StateManager).
"""

from __future__ import annotations

import re
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from g_code_mode.adapters.vertex_ai.types import ExecuteResult
from g_code_mode.preflight import require_adc
from g_code_mode.truncate import truncate_response
from g_code_mode.undo_registry import UndoRecipe

if TYPE_CHECKING:
    from g_code_mode.state import StateManager

# Trap-3: resource names must match this pattern exactly
_RESOURCE_RE = re.compile(
    r"^projects/\d+/locations/[^/]+/reasoningEngines/\d+$"
)

# Trap-7: warn when env var keys look like secrets
_SECRET_KEY_RE = re.compile(r"(key|secret|token|password|credential)", re.IGNORECASE)


def _validate_resource_name(name: str) -> None:
    if not _RESOURCE_RE.match(name):
        raise ValueError(
            f"Invalid Agent Engine resource name: {name!r}\n"
            "Expected format: projects/<number>/locations/<region>/reasoningEngines/<number>"
        )


def _warn_secret_env_vars(env_vars: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    for k in env_vars:
        if _SECRET_KEY_RE.search(k):
            warnings.append(
                f"Env var '{k}' looks like a secret. "
                "Consider storing it in Secret Manager and using a Cloud Run secret binding instead."
            )
    return warnings


def _package_dir(package_path: str) -> str:
    """If package_path is a directory, tar.gz it to a temp file and return that path."""
    p = Path(package_path)
    if p.is_dir():
        tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        with tarfile.open(tmp.name, "w:gz") as tar:
            tar.add(p, arcname=p.name)
        return tmp.name
    return str(p)


class AgentEngineAdapter:
    def __init__(self, state: StateManager) -> None:
        self._state = state

    # ── inquire (read-only) ────────────────────────────────────────────────

    async def list_agent_engines(self, project: str, location: str) -> list[dict[str, Any]]:
        """
        List all Vertex AI Agent Engine resources in the given project and location.

        Trap-6: uses the Python SDK, never gcloud CLI.
        """
        require_adc()
        from google.cloud import aiplatform  # type: ignore[import-untyped]

        aiplatform.init(project=project, location=location)
        engines = aiplatform.agent_engines.list()
        results = []
        for e in engines:
            results.append(
                {
                    "resource_name": e.resource_name,
                    "display_name": getattr(e, "display_name", ""),
                    "create_time": str(getattr(e, "create_time", "")),
                    "update_time": str(getattr(e, "update_time", "")),
                }
            )
        return results

    async def get_agent_engine(self, resource_name: str) -> dict[str, Any]:
        """
        Get details of a specific Agent Engine resource.

        Trap-3: validates resource_name format before calling the API.
        """
        require_adc()
        _validate_resource_name(resource_name)

        from google.cloud import aiplatform  # type: ignore[import-untyped]

        engine = aiplatform.agent_engines.get(resource_name=resource_name)
        return {
            "resource_name": engine.resource_name,
            "display_name": getattr(engine, "display_name", ""),
            "create_time": str(getattr(engine, "create_time", "")),
            "update_time": str(getattr(engine, "update_time", "")),
            "spec": str(getattr(engine, "_gca_resource", "")),
        }

    # ── execute (mutating) ─────────────────────────────────────────────────

    async def deploy_agent_engine(
        self,
        project: str,
        location: str,
        display_name: str,
        package_path: str,
        requirements: list[str],
        env_vars: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Deploy a new Vertex AI Agent Engine.

        Safety stack:
          1. Pre-flight: ADC check + Firestore IAM binding check (Trap-1, Trap-2)
          2. Snapshot: N/A (new resource)
          3. Deploy via SDK — resource_name extracted by regex (Trap-3)
          4. Poll list() to confirm resource appears (Trap-5)
          5. Undo registered: delete the created resource_name
          6. Warn on secret-like env var keys (Trap-7)
          7. Warn about resource name instability (Trap-4)
        """
        require_adc()  # Trap-1

        env_vars = env_vars or {}
        warnings = _warn_secret_env_vars(env_vars)  # Trap-7

        # Trap-2: check Firestore IAM binding for Agent Engine service agent
        firestore_warning = await _check_firestore_iam(project)
        if firestore_warning:
            warnings.append(firestore_warning)

        pkg = _package_dir(package_path)
        op_id = self._state.create_operation(
            "deploy_agent_engine",
            {
                "project": project,
                "location": location,
                "display_name": display_name,
                "package_path": package_path,
                "requirements": requirements,
                "env_vars": env_vars,
            },
        )

        from google.cloud import aiplatform  # type: ignore[import-untyped]

        aiplatform.init(project=project, location=location)

        try:
            engine = aiplatform.agent_engines.create(
                agent_engine=_build_agent_engine_spec(
                    display_name=display_name,
                    package_path=pkg,
                    requirements=requirements,
                    env_vars=env_vars,
                ),
            )
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(
                f"Agent Engine deploy failed: {exc}\n"
                "If you see a long-running operation name in the error, store it "
                "and call resume_deploy(op_id) once the operation completes."
            ) from exc

        resource_name: str = engine.resource_name

        # Trap-3: validate extracted name
        if not _RESOURCE_RE.match(resource_name):
            self._state.update_status(op_id, "failed")
            raise RuntimeError(
                f"Deploy returned unexpected resource name: {resource_name!r}. "
                "Check the Vertex AI console for the actual resource."
            )

        # Trap-5: confirm resource appears in list()
        appeared = await _poll_until_listed(project, location, resource_name)
        if not appeared:
            warnings.append(
                f"Resource {resource_name} not yet visible in list() after deploy. "
                "It may still be initialising. Verify with list_agent_engines()."
            )

        undo = UndoRecipe(
            description=f"Delete the Agent Engine just created ({resource_name})",
            call=f"await delete_agent_engine(resource_name={resource_name!r})",
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed", {"resource_name": resource_name})

        # Trap-4: warn about downstream config update
        warnings.append(
            f"New resource name: {resource_name}. "
            "Update any downstream config that references AGENT_ENGINE_RESOURCE_NAME "
            "(e.g. Cloud Run env var) and redeploy Cloud Run to pick up the change."
        )

        return ExecuteResult(
            success=True,
            resource_name=resource_name,
            undo_recipe=undo.to_dict(),
            warnings=warnings,
            op_id=op_id,
        ).to_dict()

    async def delete_agent_engine(self, resource_name: str) -> dict[str, Any]:
        """
        Delete an Agent Engine resource.

        Safety stack:
          1. Pre-flight: ADC + resource_name format (Trap-3)
          2. Snapshot: full get_agent_engine() before deletion
          3. Delete via SDK
          4. Undo registered: redeploy from snapshot (with caveat if package unavailable)
        """
        require_adc()
        _validate_resource_name(resource_name)

        snapshot = await self.get_agent_engine(resource_name)
        op_id = self._state.create_operation(
            "delete_agent_engine", {"resource_name": resource_name}
        )
        self._state.set_snapshot(op_id, snapshot)

        from google.cloud import aiplatform  # type: ignore[import-untyped]

        try:
            engine = aiplatform.agent_engines.get(resource_name=resource_name)
            engine.delete()
        except Exception as exc:
            self._state.update_status(op_id, "failed")
            raise RuntimeError(f"Delete failed: {exc}") from exc

        undo = UndoRecipe(
            description=(
                f"Redeploy the deleted Agent Engine '{snapshot.get('display_name', '')}' "
                "from its pre-deletion snapshot. Requires the original package to be available locally."
            ),
            call=(
                "await deploy_agent_engine("
                f"project=<project>, location=<location>, "
                f"display_name={snapshot.get('display_name', '')!r}, "
                "package_path=<local_path>, requirements=<requirements>, env_vars=<env_vars>)"
            ),
        )
        self._state.set_undo_recipe(op_id, undo.to_dict())
        self._state.update_status(op_id, "completed", {"deleted": resource_name})

        return ExecuteResult(
            success=True,
            resource_name=resource_name,
            undo_recipe=undo.to_dict(),
            snapshot=snapshot,
            warnings=[
                "Undo requires the original agent package to be available locally. "
                "If the package no longer exists, undo is not possible."
            ],
            op_id=op_id,
        ).to_dict()

    async def query_agent_engine(
        self, resource_name: str, message: str
    ) -> dict[str, Any]:
        """
        Send a message to an Agent Engine. Useful as a smoke test.
        Read-only from an infrastructure perspective; stateless.
        """
        require_adc()
        _validate_resource_name(resource_name)

        from google.cloud import aiplatform  # type: ignore[import-untyped]

        engine = aiplatform.agent_engines.get(resource_name=resource_name)
        session = engine.create_session()
        response = session.send_message(message)
        return {
            "resource_name": resource_name,
            "message": message,
            "response": str(response),
        }


# ── helpers ────────────────────────────────────────────────────────────────


async def _check_firestore_iam(project: str) -> str | None:
    """
    Trap-2: Check whether the Vertex AI Reasoning Engine service agent has
    roles/datastore.user on the project. Returns a warning string if missing.
    """
    try:
        from google.cloud import resourcemanager_v3  # type: ignore[import-untyped]

        client = resourcemanager_v3.ProjectsClient()
        policy = client.get_iam_policy(resource=f"projects/{project}")
        service_agent_prefix = "serviceAccount:service-"

        for binding in policy.bindings:
            if binding.role == "roles/datastore.user":
                for member in binding.members:
                    if (
                        member.startswith(service_agent_prefix)
                        and "gcp-sa-aiplatform-re" in member
                    ):
                        return None  # binding exists — OK

        return (
            "The Vertex AI Reasoning Engine service agent may lack roles/datastore.user. "
            "If Agent Engine runs fail to write state, run:\n"
            f"  gcloud projects add-iam-policy-binding {project} \\\n"
            "    --member=serviceAccount:service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com \\\n"
            "    --role=roles/datastore.user"
        )
    except Exception:
        # IAM check is best-effort; don't fail the deploy over it
        return None


async def _poll_until_listed(
    project: str, location: str, resource_name: str, attempts: int = 5
) -> bool:
    """Trap-5: poll list() to confirm the resource appears after deploy."""
    import asyncio

    from google.cloud import aiplatform  # type: ignore[import-untyped]

    aiplatform.init(project=project, location=location)
    for _ in range(attempts):
        try:
            engines = aiplatform.agent_engines.list()
            if any(e.resource_name == resource_name for e in engines):
                return True
        except Exception:
            pass
        await asyncio.sleep(3)
    return False


def _build_agent_engine_spec(
    display_name: str,
    package_path: str,
    requirements: list[str],
    env_vars: dict[str, str],
) -> Any:
    """Build the agent engine spec object for the SDK create() call."""
    from google.cloud.aiplatform_v1beta1.types import reasoning_engine as re_types  # type: ignore[import-untyped]

    spec = re_types.ReasoningEngine(
        display_name=display_name,
        spec=re_types.ReasoningEngineSpec(
            package_spec=re_types.ReasoningEngineSpec.PackageSpec(
                python_version="3.12",
                requirements=requirements,
            ),
        ),
    )
    return spec

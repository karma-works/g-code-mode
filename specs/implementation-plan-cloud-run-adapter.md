# Implementation Plan: Cloud Run Adapter

**Status:** Draft — 2026-05-16  
**Scope:** Cloud Run adapter for g-code-mode — list, get, deploy revision, set traffic, rollback, get logs. Full five-layer safety stack.

---

## Can we borrow from Terraform / Terraform MCP?

Short answer: **Terraform MCP — no. Terraform provider — yes for field reference. cloud-run-mcp — yes for read operations.**

### Terraform MCP server

The HashiCorp Terraform MCP server is a **registry documentation lookup and HCP Terraform workspace management tool**. It does not call Cloud Run APIs directly. Its `create_run` tool manages Terraform *runs* inside an HCP Terraform workspace — not GCP resources. Using it for the Cloud Run adapter would require:

1. Writing a Terraform config file
2. Uploading it to an HCP Terraform workspace
3. Triggering `plan_only` → review → `plan_and_apply`

That is not the right shape for g-code-mode, which makes direct, fast, authenticated Python SDK calls. **Terraform MCP is not usable here.**

However, two conceptual patterns transfer cleanly:

| Terraform concept | g-code-mode equivalent |
|---|---|
| `plan_only` run | pre-flight dry-run in `deploy_revision` |
| `plan_and_apply` run | pre-flight → execute in one call |
| Destructive ops disabled by default (`ENABLE_TF_OPERATIONS=true`) | `execute` ops require explicit call; `inquire` is always safe |
| State file tracks prior resource config | SQLite snapshot before every mutation |

### Terraform Google provider (`google_cloud_run_v2_service`)

The Terraform resource documents every meaningful field on a Cloud Run service. We use it as a **field reference** — not for execution, but to know which parameters matter and which have tricky defaults:

- `traffic` block: `type` (LATEST or REVISION) + `revision` + `percent` — maps directly to our `set_traffic` operation
- `template.scaling`: `min_instance_count`, `max_instance_count` — critical for the background-thread trap
- `template.execution_environment`: SANDBOX vs GEN2
- `ingress`: `INGRESS_TRAFFIC_ALL` (public) vs `INGRESS_TRAFFIC_INTERNAL_ONLY`
- `template.service_account`: easy to forget, defaults to Compute default SA
- Env vars: plain `value` vs `value_source.secret_key_ref` (Secret Manager) — the secret-exposure trap

### `GoogleCloudPlatform/cloud-run-mcp` (Apache-2.0)

This is directly **reusable source** for the three read-only operations. We adapt their logic (TypeScript → Python SDK), retain copyright headers per Apache-2.0:

| Their tool | Our operation | Borrow? |
|---|---|---|
| `list-services` | `list_services` | ✓ adapt |
| `get-service` | `get_service` | ✓ adapt |
| `get-service-log` | `get_service_logs` | ✓ adapt |
| `deploy-file-contents` | `deploy_revision` | ✗ different approach (image-based, not file-based) |
| No traffic tool | `set_traffic` | build fresh |
| No rollback tool | `rollback_revision` | build fresh |

---

## Cloud Run Python SDK

Package: `google-cloud-run` (`google.cloud.run_v2`)

```python
from google.cloud import run_v2

services = run_v2.ServicesClient()
revisions = run_v2.RevisionsClient()

# Parent path
parent = f"projects/{project}/locations/{region}"
service_name = f"{parent}/services/{service_id}"
```

**Key types:**
- `run_v2.Service` — top-level service resource
- `run_v2.Revision` — immutable snapshot of a deployed configuration
- `run_v2.TrafficTarget` — `revision` + `percent` + `type` (LATEST or named revision)
- `run_v2.RevisionTemplate` — image, env vars, scaling, service account, resources

**Traffic-only update** (no new revision):

```python
from google.protobuf import field_mask_pb2

service.traffic = [
    run_v2.TrafficTarget(revision=revision_name, percent=10, type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION),
    run_v2.TrafficTarget(type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST, percent=90),
]
op = services.update_service(
    service=service,
    update_mask=field_mask_pb2.FieldMask(paths=["traffic"]),
)
result = op.result()  # blocks until done
```

---

## Operations in scope

| Operation | Type | Undo action |
|---|---|---|
| `list_services(project, region)` | inquire | — |
| `get_service(project, region, service_id)` | inquire | — |
| `list_revisions(project, region, service_id)` | inquire | — |
| `get_service_logs(project, region, service_id, limit)` | inquire | — |
| `deploy_revision(project, region, service_id, image, env_vars, min_instances, max_instances, cpu_throttling, traffic_pct)` | execute | `set_traffic` back to pre-deploy splits |
| `set_traffic(project, region, service_id, splits)` | execute | `set_traffic` with captured pre-change splits |
| `rollback_revision(project, region, service_id, revision_name)` | execute | `set_traffic` to restore current splits |

`rollback_revision` is sugar over `set_traffic`: routes 100% traffic to a named prior revision. Explicit rollback is safer than asking the LLM to construct a traffic split.

---

## GapHunter traps to absorb

From `specs/llm-learnings.md` and operational experience:

**Trap CR-1 — Background threads die after HTTP 202**  
Cloud Run shuts instances down after returning a response. Background work launched after a `202` is killed before it completes. Symptom: run stays `queued` with no `parsing_constraints` event.  
Adapter response: when `min_instances` is `0` and `cpu_throttling` is `True` (the default), emit a warning with the remediation:
```
--min-instances=1 --no-cpu-throttling
```

**Trap CR-2 — Plain env vars visible in service metadata**  
`gcloud run services describe` shows raw values for any env var set as `value=`. API keys set this way are readable by anyone with Cloud Run reader access.  
Adapter response: warn on any env var key matching `_KEY`, `_SECRET`, `_TOKEN`, `_PASSWORD`, `_CREDENTIAL`. Suggest Secret Manager binding.

**Trap CR-3 — Two-deployment problem**  
Updating `AGENT_ENGINE_RESOURCE_NAME` in a Cloud Run env var requires a new Cloud Run deployment. LLMs frequently update the Agent Engine but forget to redeploy Cloud Run.  
Adapter response: if `env_vars` contains `AGENT_ENGINE_RESOURCE_NAME`, add a warning: "Cloud Run must be redeployed for this env var change to take effect — the current deployment includes it."

**Trap CR-4 — Region mismatch**  
The LLM knows the service exists but deploys to the wrong region. Cloud Run services are regional; `list_services` in the wrong region returns empty.  
Adapter response: `list_services` returns the region alongside each service. `deploy_revision` pre-flight calls `get_service` first — if not found, suggests checking other regions.

**Trap CR-5 — `--allow-unauthenticated` defaults to private**  
Deploying a Cloud Run service without specifying ingress defaults to `INGRESS_TRAFFIC_INTERNAL_ONLY`. Public web apps silently become unreachable.  
Adapter response: surface the current ingress setting in `get_service` output. If `deploy_revision` omits `ingress`, default to preserving the existing setting — never silently change it.

**Trap CR-6 — Traffic percentage must sum to 100**  
The Cloud Run API rejects traffic splits that don't sum to exactly 100%.  
Adapter response: `set_traffic` pre-flight validates that `sum(splits.values()) == 100` and raises a clear error before calling the API.

---

## Interface exposed to the code tool

```python
# ── inquire ────────────────────────────────────────────────────────────────

async def list_services(project: str, region: str) -> list[dict]:
    """List all Cloud Run services. Returns name, region, url, traffic, latest_revision."""

async def get_service(project: str, region: str, service_id: str) -> dict:
    """Full service detail: image, env_vars (keys only for secrets), traffic splits,
    scaling config, ingress, service_account, urls."""

async def list_revisions(project: str, region: str, service_id: str) -> list[dict]:
    """List revisions newest-first. Returns revision_name, image, create_time, traffic_pct."""

async def get_service_logs(
    project: str, region: str, service_id: str, limit: int = 50
) -> list[dict]:
    """Recent log entries for the service. Returns timestamp, severity, message."""

# ── execute ────────────────────────────────────────────────────────────────

async def deploy_revision(
    project: str,
    region: str,
    service_id: str,
    image: str,
    env_vars: dict[str, str] | None = None,
    min_instances: int = 0,
    max_instances: int = 100,
    cpu_throttling: bool = True,
    traffic_pct: int = 100,
    ingress: str | None = None,   # preserve existing if None
) -> dict:
    """Deploy a new revision. Returns new revision_name, url, undo_recipe, warnings."""

async def set_traffic(
    project: str,
    region: str,
    service_id: str,
    splits: dict[str, int],   # {"LATEST": 90, "my-service-00042-abc": 10}
) -> dict:
    """Update traffic splits without deploying a new revision. Returns undo_recipe."""

async def rollback_revision(
    project: str,
    region: str,
    service_id: str,
    revision_name: str,
) -> dict:
    """Route 100% traffic to a named prior revision. Returns undo_recipe to restore."""
```

**`get_service` output shape** (secrets never exposed):
```python
{
    "name": "my-service",
    "region": "europe-west6",
    "url": "https://my-service-abc123-ew.a.run.app",
    "image": "europe-west6-docker.pkg.dev/my-project/repo/app:sha-abc",
    "traffic": [{"revision": "LATEST", "percent": 90}, {"revision": "my-service-00041-xyz", "percent": 10}],
    "scaling": {"min_instances": 1, "max_instances": 100},
    "cpu_throttling": False,
    "ingress": "INGRESS_TRAFFIC_ALL",
    "service_account": "my-sa@my-project.iam.gserviceaccount.com",
    "env_var_keys": ["GCP_PROJECT_ID", "AGENT_ENGINE_RESOURCE_NAME"],  # keys only
    "secret_env_vars": ["BRAVE_SEARCH_API_KEY"],   # keys flagged as secrets
    "latest_revision": "my-service-00042-abc",
    "ready": True,
}
```

---

## Safety stack per operation

### `deploy_revision`

1. **Pre-flight**
   - ADC check
   - `get_service` to confirm service exists and capture current state (Trap CR-4)
   - Warn on secret-like env var keys (Trap CR-2)
   - Warn on `AGENT_ENGINE_RESOURCE_NAME` in env_vars (Trap CR-3)
   - Warn if `min_instances=0` and `cpu_throttling=True` (Trap CR-1)
   - Validate `traffic_pct` in `[0, 100]`

2. **Snapshot** — full `get_service()` response including current traffic splits

3. **Execute** — `services.update_service(service=updated_service).result()`

4. **Undo** — `set_traffic` back to pre-deploy snapshot splits

5. **Result** — new revision name, service URL, undo_recipe, warnings

### `set_traffic`

1. **Pre-flight**
   - ADC check
   - Validate `sum(splits.values()) == 100` (Trap CR-6)
   - Confirm all named revisions exist via `list_revisions`

2. **Snapshot** — current `service.traffic`

3. **Execute** — traffic-only update via `FieldMask(paths=["traffic"])`

4. **Undo** — `set_traffic` with snapshotted splits

### `rollback_revision`

1. **Pre-flight**
   - ADC check
   - Confirm `revision_name` exists in `list_revisions`

2. **Snapshot** — current `service.traffic`

3. **Execute** — `set_traffic({revision_name: 100})`

4. **Undo** — `set_traffic` with snapshotted splits

---

## Implementation steps

| Step | What | Files |
|---|---|---|
| 1 | Add `google-cloud-run` to `pyproject.toml` | `pyproject.toml` |
| 2 | `list_services` + `get_service` (adapted from cloud-run-mcp, Apache-2.0) | `adapters/cloud_run/service.py` |
| 3 | `list_revisions` | `adapters/cloud_run/service.py` |
| 4 | `get_service_logs` (adapted from cloud-run-mcp) | `adapters/cloud_run/service.py` |
| 5 | `deploy_revision` with full safety stack | `adapters/cloud_run/service.py` |
| 6 | `set_traffic` with pre-flight sum validation | `adapters/cloud_run/service.py` |
| 7 | `rollback_revision` as sugar over `set_traffic` | `adapters/cloud_run/service.py` |
| 8 | Register adapter in `server.py` namespace | `server.py` |
| 9 | Unit tests — all 6 traps + undo recipes | `tests/adapters/test_cloud_run.py` |
| 10 | Update `code` tool description with Cloud Run functions | `server.py` |
| 11 | Update `README.md` adapter table (planned → v0.2) | `README.md` |

---

## Files to create

```
g_code_mode/
  adapters/
    cloud_run/
      __init__.py
      service.py      # all operations
      types.py        # CloudRunServiceDetail, TrafficSplit
tests/
  adapters/
    test_cloud_run.py
```

---

## What we are NOT borrowing from cloud-run-mcp

Their `deploy-file-contents` and `deploy-local-folder` tools build and deploy from source files (Buildpacks / source deploy). g-code-mode targets image-based deploys — the developer has already built and pushed an image to Artifact Registry. Source deploy is a separate workflow and adds Buildpacks as a dependency.

Their `create-project` tool is out of scope for g-code-mode (project provisioning is a one-time setup, not an operational tool).

---

## Open questions

1. **Logs via Cloud Logging vs Cloud Run API** — `cloud-run-mcp` uses the Cloud Run API for logs. The Cloud Logging Python SDK (`google-cloud-logging`) gives richer filtering. Recommendation: use `google-cloud-logging` with a `resource.type=cloud_run_revision` filter — same data, better query control.

2. **Secret Manager read** — should `get_service` resolve Secret Manager references to show which secrets are bound, without exposing values? Recommendation: yes — list bound secret names and versions, never values.

3. **Artifact Registry auth helper** — should the adapter include an `ensure_docker_auth(region)` helper that runs `gcloud auth configure-docker`? Recommendation: document it in the tool description, don't automate it (it's a one-time setup per region).

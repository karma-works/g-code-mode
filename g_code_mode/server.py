"""
g-code-mode MCP server.

Exposes a single `code` tool. The calling LLM writes `async def run()` that
orchestrates Google Cloud operations via injected adapter functions.
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

from g_code_mode.executor import run_code
from g_code_mode.preflight import check_adc
from g_code_mode.state import StateManager

log = logging.getLogger("g-code-mode")

mcp = FastMCP("g-code-mode")
_state = StateManager()


def _build_namespace() -> dict:
    """Collect all registered adapter callables into the exec namespace."""
    from g_code_mode.adapters.cloud_run.service import CloudRunAdapter
    from g_code_mode.adapters.firestore.service import FirestoreAdapter
    from g_code_mode.adapters.vertex_ai.agent_engine import AgentEngineAdapter

    vertex = AgentEngineAdapter(state=_state)
    cloud_run = CloudRunAdapter(state=_state)
    firestore = FirestoreAdapter(state=_state)
    return {
        # Vertex AI Agent Engine
        "list_agent_engines": vertex.list_agent_engines,
        "get_agent_engine": vertex.get_agent_engine,
        "deploy_agent_engine": vertex.deploy_agent_engine,
        "delete_agent_engine": vertex.delete_agent_engine,
        "query_agent_engine": vertex.query_agent_engine,
        # Cloud Run
        "list_services": cloud_run.list_services,
        "get_service": cloud_run.get_service,
        "list_revisions": cloud_run.list_revisions,
        "get_service_logs": cloud_run.get_service_logs,
        "deploy_revision": cloud_run.deploy_revision,
        "set_traffic": cloud_run.set_traffic,
        "rollback_revision": cloud_run.rollback_revision,
        # Firestore
        "list_collections": firestore.list_collections,
        "list_documents": firestore.list_documents,
        "get_document": firestore.get_document,
        "query_documents": firestore.query_documents,
        "list_subcollections": firestore.list_subcollections,
        "set_document": firestore.set_document,
        "update_document": firestore.update_document,
        "delete_document": firestore.delete_document,
    }


_TOOL_DESCRIPTION = """\
Execute a Python async function to orchestrate Google Cloud operations safely.

Write an `async def run()` function. Its return value becomes the tool result.
The following adapter functions are available in the function's scope:

## Vertex AI Agent Engine

### Read-only (inquire)
- `list_agent_engines(project: str, location: str) -> list[dict]`
  List all Agent Engine resources. Returns resource_name, display_name, timestamps.

- `get_agent_engine(resource_name: str) -> dict`
  Get full details of one Agent Engine. resource_name format:
  projects/<number>/locations/<region>/reasoningEngines/<number>

### Mutating (execute) — returns ExecuteResult with undo_recipe
- `deploy_agent_engine(project, location, display_name, package_path, requirements, env_vars) -> dict`
  Deploy a new Agent Engine. Validates ADC, checks Firestore IAM, polls until listed.
  Returns resource_name + undo_recipe. Always surface undo_recipe to the user.

- `delete_agent_engine(resource_name: str) -> dict`
  Delete an Agent Engine. Captures snapshot before deletion.
  Returns undo_recipe with redeploy instructions.

- `query_agent_engine(resource_name: str, message: str) -> dict`
  Send a test message to an Agent Engine. Useful as a smoke test after deploy.

## Cloud Run

### Read-only (inquire)
- `list_services(project: str, region: str) -> list[dict]`
  List Cloud Run services. Returns name, region, url, traffic, latest_revision, ready.

- `get_service(project: str, region: str, service_id: str) -> dict`
  Full service detail: image, env_var_keys (secret values never exposed), traffic splits,
  scaling, ingress, service_account, latest_revision, ready.

- `list_revisions(project: str, region: str, service_id: str) -> list[dict]`
  List revisions newest-first. Returns name, image, create_time, ready.

- `get_service_logs(project: str, region: str, service_id: str, limit: int = 50) -> list[dict]`
  Recent log entries via Cloud Logging. Returns timestamp, severity, message.

### Mutating (execute) — returns dict with undo_recipe
- `deploy_revision(project, region, service_id, image, env_vars, min_instances, max_instances, cpu_throttling, traffic_pct, ingress) -> dict`
  Deploy a new revision. Validates ADC, confirms service exists, warns on secret env vars,
  warns on background-thread risk. Returns new_revision, url, undo_recipe, warnings.

- `set_traffic(project: str, region: str, service_id: str, splits: dict[str, int]) -> dict`
  Update traffic splits without a new revision. splits must sum to 100.
  Example: splits={"LATEST": 90, "my-service-00041-abc": 10}
  Returns undo_recipe to restore prior splits.

- `rollback_revision(project: str, region: str, service_id: str, revision_name: str) -> dict`
  Route 100% traffic to a named prior revision. Returns undo_recipe to restore.

## Firestore

### Read-only (inquire)
- `list_collections(project: str, database: str = "(default)") -> list[str]`
  List root-level collection IDs. Subcollections (e.g. runs/{id}/events) are NOT returned —
  use list_subcollections for those.

- `list_documents(project, database, collection, limit=50, fields=None) -> list[dict]`
  Stream documents from a collection with optional field filtering. collection can be a
  subcollection path like "runs/abc123/events". Each result includes "_id".

- `get_document(project, database, collection, document_id) -> dict | None`
  Get a single document. Returns None if not found.

- `query_documents(project, database, collection, filters, order_by, limit=50) -> list[dict]`
  Query with filters: list of (field, operator, value) tuples.
  Operators: "==", "!=", "<", "<=", ">", ">=", "in", "array_contains"
  Example: filters=[("status", "==", "completed"), ("created_at", ">", "2026-01-01")]

- `list_subcollections(project, database, collection, document_id) -> list[str]`
  List subcollection IDs under a document.
  Example: list_subcollections(..., "runs", "abc123") → ["events", "sources"]

### Mutating (execute) — returns dict with undo_recipe
- `set_document(project, database, collection, document_id, data) -> dict`
  Create or replace a document. Snapshots prior state.
  Undo: set_document(prior_data) if existed; delete_document if new.

- `update_document(project, database, collection, document_id, updates) -> dict`
  Partial update of an existing document. Fails if document doesn't exist — use set_document.
  Undo: set_document(full_snapshot) to restore all fields.

- `delete_document(project, database, collection, document_id) -> dict`
  Delete a document. Snapshots before deletion.
  Undo: set_document(snapshot) to recreate.

## Rules
- Never pass credentials into the script — ADC is used automatically.
- Always `return` the final result from `run()`.
- After a mutating operation, surface the `undo_recipe` to the user.

## Example — inspect a GapHunter run and its events from Firestore
```python
async def run():
    # Get a specific run document
    doc = await get_document(
        project="my-project", database="(default)",
        collection="runs", document_id="abc123"
    )
    if not doc:
        return "Run not found"
    # List its subcollections (e.g. events, sources)
    subcols = await list_subcollections(
        project="my-project", database="(default)",
        collection="runs", document_id="abc123"
    )
    # Read events subcollection
    events = await list_documents(
        project="my-project", database="(default)",
        collection="runs/abc123/events", limit=20
    )
    return {"run": doc, "subcollections": subcols, "events": events}
```

## Example — list Cloud Run services and check traffic
```python
async def run():
    services = await list_services(project="my-project", region="europe-west1")
    if not services:
        return "No services found — check the region"
    svc = await get_service(
        project="my-project", region="europe-west1", service_id=services[0]["name"]
    )
    return svc
```

## Example — deploy revision with canary split
```python
async def run():
    result = await deploy_revision(
        project="my-project",
        region="europe-west1",
        service_id="my-service",
        image="europe-west1-docker.pkg.dev/my-project/repo/app:sha-abc",
        env_vars={"GCP_PROJECT_ID": "my-project"},
        min_instances=1,
        cpu_throttling=False,
        traffic_pct=10,  # 10% to new revision, 90% stays on current
    )
    return result  # always show result["undo_recipe"] to the user
```

## Example — discover Agent Engine then query
```python
async def run():
    engines = await list_agent_engines(project="my-project", location="us-central1")
    if not engines:
        return "No agent engines found"
    rn = engines[0]["resource_name"]
    return await query_agent_engine(rn, "hello")
```

## Example — deploy Agent Engine
```python
async def run():
    result = await deploy_agent_engine(
        project="my-project",
        location="us-central1",
        display_name="my-agent",
        package_path="./agent/dist",
        requirements=["google-cloud-aiplatform>=1.112.0"],
        env_vars={"MY_VAR": "value"},
    )
    return result  # includes result["undo_recipe"] — show it to the user
```
"""


@mcp.tool(description=_TOOL_DESCRIPTION)
async def code(script: str) -> str:
    """Run LLM-generated orchestration code against Google Cloud adapters."""
    log.debug("Executing script:\n%s", script)

    namespace = _build_namespace()
    result = await run_code(script, namespace)

    if result.logs:
        log.debug("Script stdout:\n%s", result.logs)

    if result.error:
        raise ValueError(result.error)

    return result.output


@mcp.tool()
async def adc_status() -> str:
    """Check whether Application Default Credentials are configured."""
    ok, msg = check_adc()
    if ok:
        return "ADC is configured."
    return f"ADC is NOT configured.\n{msg}"


@mcp.tool()
async def list_in_flight_operations() -> str:
    """
    List in-flight Google Cloud operations tracked by g-code-mode.

    Use this to find operations that timed out and may need to be resumed
    or investigated in the Google Cloud console.
    """
    ops = _state.get_in_flight()
    if not ops:
        return "No in-flight operations."
    return json.dumps(ops, indent=2, default=str)


_REPORT_GAP_DESCRIPTION = """\
Report a new gap or missing capability in g-code-mode.

Use this when you hit something the tool doesn't handle well:
- A GCP operation with no adapter support
- An error message that wasn't helpful
- A GCP behaviour the adapters don't warn about
- Anything you had to work around using raw gcloud or the API directly

## Workflow — always two calls

**Call 1 (dry run):** submit=False
Returns the formatted issue body, duplicate candidates, and a privacy reminder.
Show all of this to the user before proceeding.

**Call 2 (submit):** submit=True
Only after the user has explicitly approved the content AND confirmed no PII remains.

## Parameters

- operation_attempted: The GCP task you were trying to do
- gap_description: What g-code-mode was missing or got wrong (be specific)
- workaround_used: What you did instead; empty string if completely blocked
- suggestion: What would fix it; empty string if unknown
- severity: "low" (friction but workable) | "medium" (significant workaround) | "high" (blocked)
- llm_model: Your model name, e.g. "claude-sonnet-4-6"
- submit: False = preview + duplicate check; True = create GitHub issue

## Rules

- One gap per call. File separately if there are multiple issues.
- Never call with submit=True without explicit user approval.
- If duplicate_candidates are returned, ask the user to review them first.
"""


@mcp.tool(description=_REPORT_GAP_DESCRIPTION)
async def report_gap(
    operation_attempted: str,
    gap_description: str,
    workaround_used: str = "",
    suggestion: str = "",
    severity: str = "medium",
    llm_model: str = "",
    submit: bool = False,
) -> str:
    """Report a new gap or missing capability in g-code-mode to the GitHub issue tracker."""
    from g_code_mode.reporting import report_gap as _report_gap

    result = _report_gap(
        operation_attempted=operation_attempted,
        gap_description=gap_description,
        workaround_used=workaround_used,
        suggestion=suggestion,
        severity=severity,
        llm_model=llm_model,
        submit=submit,
    )
    return json.dumps(result, indent=2)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()

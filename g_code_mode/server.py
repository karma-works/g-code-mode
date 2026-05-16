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
    from g_code_mode.adapters.vertex_ai.agent_engine import AgentEngineAdapter

    vertex = AgentEngineAdapter(state=_state)
    cloud_run = CloudRunAdapter(state=_state)
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

## Rules
- Never pass credentials into the script — ADC is used automatically.
- Always `return` the final result from `run()`.
- After a mutating operation, surface the `undo_recipe` to the user.

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


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()

---
name: g-code-mode
description: >
  Operate Google Cloud infrastructure safely via the g-code-mode MCP server.
  Use when the user asks to list, deploy, delete, query, or inspect Google Cloud
  resources — specifically Vertex AI Agent Engine, Cloud Run, or Firestore.
  Handles read-only discovery (inquire) and validated mutations with undo (execute).
allowed-tools: mcp__g-code-mode__code mcp__g-code-mode__adc_status mcp__g-code-mode__list_in_flight_operations
---

## Active GCP context

- Project: !`gcloud config get-value project 2>/dev/null || echo "not set"`
- Region:  !`gcloud config get-value compute/region 2>/dev/null || echo "not set"`
- Account: !`gcloud config get-value account 2>/dev/null || echo "not set"`
- ADC:     !`gcloud auth application-default print-access-token >/dev/null 2>&1 && echo "configured" || echo "NOT CONFIGURED — run: gcloud auth application-default login"`

## How to use g-code-mode

Call the `code` MCP tool with an `async def run()` function.
Injected adapter functions are available directly in the function scope.

### Read-only discovery

```python
async def run():
    engines = await list_agent_engines(project="my-project", location="us-central1")
    return engines
```

### Validated mutation

Every mutating call returns a dict with `undo_recipe`. Always surface this to the user.

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
    return result  # show result["undo_recipe"] to the user
```

## Rules

1. Check ADC status above before writing any code. If NOT CONFIGURED, stop and
   tell the user to run `gcloud auth application-default login`.
2. Never pass credentials or tokens into the script. ADC handles auth.
3. After every successful mutation, show the `undo_recipe` to the user.
4. After a deploy, remind the user to update downstream config that references
   the old Agent Engine resource name (e.g. `AGENT_ENGINE_RESOURCE_NAME`).
5. If a deploy times out, call `list_in_flight_operations` to find the op_id.

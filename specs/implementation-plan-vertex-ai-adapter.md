# Implementation Plan: Vertex AI Agent Engine Adapter

**Status:** Draft — 2026-05-16  
**Scope:** First adapter. Covers Vertex AI Agent Engine (formerly Reasoning Engine) list, deploy, query, and delete operations with full undo/snapshot/retry/rollback support.

---

## Why first

No existing MCP server covers Vertex AI Agent Engine. Google's managed MCP servers, `gcloud-mcp`, and `cloud-run-mcp` all omit it. GapHunter's `llm-learnings.md` documents ten concrete failure modes when operating Agent Engine through gcloud or the Python SDK — these are the traps the adapter must absorb.

---

## Operations in scope

| Operation | Type | Undo action |
|---|---|---|
| `list_agent_engines(project, location)` | inquire | — |
| `get_agent_engine(resource_name)` | inquire | — |
| `deploy_agent_engine(...)` | execute | `delete_agent_engine(resource_name)` |
| `delete_agent_engine(resource_name)` | execute | `deploy_agent_engine(snapshot)` |
| `query_agent_engine(resource_name, message)` | execute | — (stateless) |

---

## Known traps to absorb (from GapHunter `llm-learnings.md`)

These are real failures. The adapter must prevent or surface each one explicitly.

**Trap 1 — ADC vs gcloud auth**  
The Vertex AI Python SDK uses Application Default Credentials, not `gcloud auth login`. The adapter must verify ADC is configured before attempting any operation and emit a clear error if not (`gcloud auth application-default login`).

**Trap 2 — Agent Engine service identity needs Firestore access**  
The Reasoning Engine service agent (`service-{project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com`) requires `roles/datastore.user` to write run state to Firestore. The adapter's `deploy_agent_engine` pre-flight must check this binding and fail with a remediation command if missing.

**Trap 3 — Shell pipelines hide failed deploys**  
Deployment via `python agent/deploy.py | tail -n 1` can return the last log line even when the Python process failed. The adapter must extract the resource name by matching the full pattern `projects/[0-9]+/locations/[^/]+/reasoningEngines/[0-9]+` and reject anything else.

**Trap 4 — Resource name instability**  
Each Agent Engine deploy creates a new resource with a new numeric ID. The adapter must return the new resource name explicitly in the response and warn that any downstream config (e.g. Cloud Run env var `AGENT_ENGINE_RESOURCE_NAME`) must be updated.

**Trap 5 — Deploy operation can look stuck**  
Long-running deploy operations may time out locally without producing an error or a listable resource. The adapter must poll `agent_engines.list()` after a deploy rather than trusting the SDK's operation completion, and surface a timeout with the operation name for manual investigation.

**Trap 6 — Operation surface inconsistency**  
`gcloud ai operations list` is not available in all environments. The adapter must use the Python SDK (`aiplatform.agent_engines.list()`, `operation.result()`) as the primary interface, never gcloud CLI.

**Trap 7 — Secrets in Cloud Run env vars**  
`AGENT_ENGINE_RESOURCE_NAME` is typically stored as a plain Cloud Run env var, making it visible in `gcloud run services describe`. The adapter's deployment response must note if any secret-like values are exposed as plain env vars and recommend Secret Manager migration.

---

## Interface exposed to the code tool

```typescript
// Inquire — read-only
inquire_list_agent_engines(project: string, location: string): AgentEngineList
inquire_get_agent_engine(resource_name: string): AgentEngineDetail

// Execute — mutating, full safety stack
execute_deploy_agent_engine(params: DeployParams): ExecuteResult
execute_delete_agent_engine(resource_name: string): ExecuteResult
execute_query_agent_engine(resource_name: string, message: string): QueryResult

type DeployParams = {
  project: string
  location: string           // e.g. "us-central1"
  display_name: string
  agent_package_path: string // local path to packaged agent (tar.gz or directory)
  requirements: string[]
  env_vars: Record<string, string>
}

type ExecuteResult = {
  success: boolean
  resource_name?: string     // the created/affected resource
  undo: UndoRecipe           // returned on every execute
  snapshot?: AgentEngineDetail  // state before mutation
  warnings: string[]         // trap warnings surfaced
}

type UndoRecipe = {
  description: string
  call: string               // the exact execute call to reverse this action
}
```

---

## Safety stack implementation

### Pre-flight (before any execute)

1. Verify ADC is configured — fail with remediation if not.
2. Verify the target project and location are accessible.
3. For `deploy`: check Agent Engine service agent IAM binding on Firestore.
4. For `deploy`: validate `agent_package_path` exists and is a valid package or directory.
5. Return a dry-run summary: what will be created/deleted, estimated duration.

### Snapshot (before mutation)

- `deploy`: no prior resource to snapshot; snapshot = null.
- `delete`: capture full `get_agent_engine(resource_name)` response as the snapshot.

### Paired undo

- `deploy` → undo: `execute_delete_agent_engine(new_resource_name)`
- `delete` → undo: `execute_deploy_agent_engine(snapshot)` — only possible if agent package is still locally available; warn if not.

### Retry with state tracking

Track deploy operations by operation name. If a deploy times out, the adapter stores the operation name and can be resumed:

```typescript
execute_resume_deploy(operation_name: string): ExecuteResult
```

Poll `agent_engines.list()` to confirm the resource appears before declaring success.

### Transactional rollback

For multi-step sequences (e.g. deploy new Agent Engine → update Cloud Run env var → smoke test):
- If the Cloud Run update fails, roll back by deleting the newly deployed Agent Engine.
- If the smoke test fails, roll back Cloud Run to the previous `AGENT_ENGINE_RESOURCE_NAME` and delete the new Agent Engine.

---

## Implementation steps

### Step 1 — Project scaffold

- [ ] Initialise TypeScript MCP server project (`@modelcontextprotocol/sdk`)
- [ ] Wire the single `code` tool using `@cloudflare/codemode` pattern adapted for Node/non-Worker sandbox (Node VM or isolated subprocess)
- [ ] Define `Adapter` interface: `{ inquire, execute, undo_registry }`
- [ ] Add `ExecuteResult` and `UndoRecipe` types

### Step 2 — Vertex AI SDK integration

- [ ] Add `@google-cloud/aiplatform` dependency
- [ ] Implement ADC check utility
- [ ] Implement `inquire_list_agent_engines` — wraps `aiplatform.agent_engines.list()`
- [ ] Implement `inquire_get_agent_engine` — wraps resource get

### Step 3 — Deploy operation

- [ ] Implement package validation (directory → tar.gz if needed)
- [ ] Implement pre-flight: ADC, project access, Firestore IAM binding check
- [ ] Implement `execute_deploy_agent_engine` with resource name extraction via regex
- [ ] Implement post-deploy polling: confirm resource appears in `list()`
- [ ] Register undo: delete the created resource name

### Step 4 — Delete operation

- [ ] Implement snapshot capture before delete
- [ ] Implement `execute_delete_agent_engine`
- [ ] Register undo: redeploy from snapshot (with caveat if package unavailable)

### Step 5 — Query operation

- [ ] Implement `execute_query_agent_engine` — send a test message and return response
- [ ] Useful as smoke-test step in multi-step sequences

### Step 6 — Resume and retry

- [ ] Persist in-flight operation names (in-memory for now, file for persistence)
- [ ] Implement `execute_resume_deploy(operation_name)`

### Step 7 — Tests and trap verification

- [ ] Unit tests for each trap: ADC missing, IAM missing, bad resource name extraction, timed-out operation
- [ ] Integration test against a real GCP project (guard-railed: only touches a designated test project)

---

## Out of scope for this adapter

- Vertex AI endpoints, models, datasets, pipelines — separate adapters
- Agent Engine session management — post-deploy concern
- Cost estimation — future adapter feature

---

## Files to create

```
src/
  adapters/
    vertex-ai/
      agent-engine.ts       # all operations
      agent-engine.test.ts
      types.ts
  core/
    executor.ts             # sandbox + code tool
    undo-registry.ts
    preflight.ts
    retry.ts
  server.ts                 # MCP server entrypoint
```

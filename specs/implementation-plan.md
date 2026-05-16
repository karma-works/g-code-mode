# Implementation Plan

**Status:** Draft — 2026-05-16  
**Scope:** Full v1 — from project scaffold to PyPI-installable MCP server with Vertex AI Agent Engine adapter and Claude Code skill.

ADRs: [001 Language](ADR-001-language-runtime.md) · [002 Sandbox](ADR-002-sandbox.md) · [003 State](ADR-003-state-persistence.md) · [004 Transport](ADR-004-mcp-transport.md) · [005 Adapters](ADR-005-adapter-architecture.md)  
Reference: [Cloudflare MCP learnings](cloudflare-mcp-learnings.md) · [Vertex AI adapter plan](implementation-plan-vertex-ai-adapter.md)

---

## Directory layout

```
g-code-mode/
├── pyproject.toml
├── README.md
├── g_code_mode/
│   ├── server.py               # FastMCP entrypoint, code tool definition
│   ├── executor.py             # exec() sandbox, async runner
│   ├── truncate.py             # response truncation
│   ├── state.py                # SQLite state manager
│   ├── undo_registry.py        # undo recipe store
│   ├── preflight.py            # ADC check, project access check
│   └── adapters/
│       └── vertex_ai/
│           ├── __init__.py
│           ├── agent_engine.py # all Agent Engine operations
│           └── types.py
├── skills/
│   └── g-code-mode/
│       └── SKILL.md            # Claude Code skill
└── tests/
    ├── test_executor.py
    ├── test_state.py
    └── adapters/
        └── test_agent_engine.py
```

---

## Phase 1 — Project scaffold

### 1.1 `pyproject.toml`

```toml
[project]
name = "g-code-mode"
version = "0.1.0"
description = "LLM-safe MCP server for Google Cloud operations"
requires-python = ">=3.12"
license = "Apache-2.0"
dependencies = [
  "mcp>=1.0.0",
  "google-cloud-aiplatform[agent_engines]>=1.112.0",
  "google-auth>=2.0.0",
  "pydantic>=2.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "pytest-asyncio>=0.23.0", "pyright>=1.1.0", "ruff>=0.5.0"]

[project.scripts]
g-code-mode = "g_code_mode.server:main"
```

### 1.2 FastMCP server skeleton

The Python MCP SDK uses `FastMCP` with `@mcp.tool()` decorators. The tool description is the docstring — this is where Cloudflare's lesson applies: **the docstring must include the full function catalog and a concrete example**.

```python
# g_code_mode/server.py
from mcp.server.fastmcp import FastMCP
from g_code_mode.executor import run_code
from g_code_mode.adapters.vertex_ai.agent_engine import AgentEngineAdapter

mcp = FastMCP("g-code-mode")

def build_tool_description(adapters: list) -> str:
    """Generated at startup from registered adapters — includes all signatures."""
    ...

@mcp.tool()
async def code(script: str) -> str:
    """
    Execute a Python async function to orchestrate Google Cloud operations.

    Write an `async def run()` function. The return value becomes the result.
    Available adapter functions are injected into your function's scope.

    ## Available functions

    ### Vertex AI Agent Engine
    - `list_agent_engines(project, location) -> list[dict]`
    - `get_agent_engine(resource_name) -> dict`
    - `deploy_agent_engine(project, location, display_name, package_path, requirements, env_vars) -> ExecuteResult`
    - `delete_agent_engine(resource_name) -> ExecuteResult`
    - `query_agent_engine(resource_name, message) -> dict`

    ## Example

    ```python
    async def run():
        engines = await list_agent_engines(project="my-project", location="us-central1")
        if not engines:
            return "No agent engines found"
        first = await get_agent_engine(engines[0]["resource_name"])
        return first
    ```

    Errors from your code are returned as tool errors. GCP credentials come
    from Application Default Credentials — never pass credentials explicitly.
    """
    result = await run_code(script, namespace=build_namespace())
    if result.error:
        raise ValueError(result.error)
    return result.output

def main():
    mcp.run()  # stdio by default
```

**Key point from SDK docs:** raise exceptions for errors — FastMCP converts them to MCP error responses automatically. Do not return error dicts.

---

## Phase 2 — Core infrastructure

### 2.1 Executor (`executor.py`)

Implements ADR-002. The LLM writes `async def run()`. The server calls it via `asyncio.run()` with a timeout.

```python
# g_code_mode/executor.py
import asyncio, io, contextlib, traceback
from dataclasses import dataclass
from typing import Any

@dataclass
class ExecResult:
    output: str       # truncated JSON or text result
    error: str | None  # full traceback if exception
    logs: str         # captured stdout

async def run_code(script: str, namespace: dict[str, Any]) -> ExecResult:
    stdout_buf = io.StringIO()
    local_ns = {**namespace}

    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compile(script, "<llm_code>", "exec"), local_ns)
            if "run" not in local_ns or not asyncio.iscoroutinefunction(local_ns["run"]):
                return ExecResult(output="", error="Script must define `async def run()`", logs="")
            result = await asyncio.wait_for(local_ns["run"](), timeout=60.0)
    except asyncio.TimeoutError:
        return ExecResult(output="", error="Execution timed out after 60s", logs=stdout_buf.getvalue())
    except Exception:
        return ExecResult(output="", error=traceback.format_exc(), logs=stdout_buf.getvalue())

    from g_code_mode.truncate import truncate_response
    return ExecResult(
        output=truncate_response(result),
        error=None,
        logs=stdout_buf.getvalue(),
    )
```

**Key points:**
- Fresh `local_ns = {**namespace}` on every call — no state bleed between executions (Cloudflare learning #8).
- Full `traceback.format_exc()` on error — LLM needs this to self-correct (Cloudflare learning #7).
- `asyncio.wait_for` with 60s timeout covers runaway loops.
- Server logs all executed scripts at DEBUG level for auditability.

### 2.2 Truncation (`truncate.py`)

Directly from Cloudflare learning #4. Hard limit with actionable guidance.

```python
# g_code_mode/truncate.py
import json

MAX_TOKENS = 6_000
CHARS_PER_TOKEN = 4
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN

def truncate_response(content: object) -> str:
    text = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
    if len(text) <= MAX_CHARS:
        return text
    estimated = len(text) // CHARS_PER_TOKEN
    return (
        f"{text[:MAX_CHARS]}\n\n--- TRUNCATED ---\n"
        f"Response was ~{estimated:,} tokens (limit: {MAX_TOKENS:,}). "
        f"Add filters (location, display_name, project) to narrow results."
    )
```

### 2.3 State manager (`state.py`)

Implements ADR-003. SQLite at `~/.g-code-mode/state.db`.

```python
# g_code_mode/state.py
import sqlite3, json, os
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(os.environ.get("G_CODE_MODE_STATE_PATH", "~/.g-code-mode/state.db")).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    params      TEXT NOT NULL,
    snapshot    TEXT,
    result      TEXT,
    undo_recipe TEXT,
    gcp_op_name TEXT
);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
"""

class StateManager:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(SCHEMA)

    def record_operation(self, op_id: str, op_type: str, params: dict) -> None: ...
    def update_status(self, op_id: str, status: str, result: dict | None = None) -> None: ...
    def set_undo_recipe(self, op_id: str, recipe: dict) -> None: ...
    def set_snapshot(self, op_id: str, snapshot: dict) -> None: ...
    def set_gcp_op_name(self, op_id: str, gcp_op_name: str) -> None: ...
    def get_in_flight(self) -> list[dict]: ...
```

### 2.4 Undo registry (`undo_registry.py`)

Each mutating adapter operation registers its inverse before execution. The recipe is stored in SQLite and returned in the tool result.

```python
# g_code_mode/undo_registry.py
from dataclasses import dataclass

@dataclass
class UndoRecipe:
    description: str   # human-readable: "Delete the Agent Engine resource just created"
    call: str          # exact Python call to undo: "await delete_agent_engine('projects/...')"
```

### 2.5 Preflight checker (`preflight.py`)

ADC check on server startup and before each execute call.

```python
# g_code_mode/preflight.py
import google.auth

def check_adc() -> tuple[bool, str]:
    """Returns (ok, error_message). Call before any GCP operation."""
    try:
        credentials, project = google.auth.default()
        return True, ""
    except google.auth.exceptions.DefaultCredentialsError:
        return False, (
            "No Application Default Credentials found. Run:\n"
            "  gcloud auth application-default login\n"
            "  gcloud auth application-default set-quota-project YOUR_PROJECT"
        )
```

---

## Phase 3 — Vertex AI Agent Engine adapter

Full spec in [implementation-plan-vertex-ai-adapter.md](implementation-plan-vertex-ai-adapter.md).

### 3.1 Adapter structure

```python
# g_code_mode/adapters/vertex_ai/agent_engine.py

from google.cloud import aiplatform
from g_code_mode.state import StateManager
from g_code_mode.undo_registry import UndoRecipe
from g_code_mode.truncate import truncate_response
from g_code_mode.preflight import check_adc
import uuid, re

RESOURCE_NAME_RE = re.compile(
    r"^projects/\d+/locations/[^/]+/reasoningEngines/\d+$"
)

class AgentEngineAdapter:
    def __init__(self, state: StateManager): ...

    async def list_agent_engines(self, project: str, location: str) -> list[dict]:
        """Trap-safe list. Absorbs Trap-6: uses SDK, not gcloud CLI."""
        ...

    async def get_agent_engine(self, resource_name: str) -> dict:
        """Validates resource_name format before calling. Trap-3."""
        if not RESOURCE_NAME_RE.match(resource_name):
            raise ValueError(f"Invalid resource name format: {resource_name!r}")
        ...

    async def deploy_agent_engine(self, project, location, display_name,
                                   package_path, requirements, env_vars) -> dict:
        """
        Five-layer safety stack:
        1. Pre-flight: ADC, project access, Firestore IAM binding (Trap-1, Trap-2)
        2. Snapshot: None (new resource)
        3. Execute with resource name regex extraction (Trap-3)
        4. Poll list() to confirm resource appears (Trap-5)
        5. Register undo: delete newly created resource_name
        Returns resource_name, undo_recipe, and Trap-4 warning if applicable.
        """
        ...

    async def delete_agent_engine(self, resource_name: str) -> dict:
        """
        1. Snapshot: full get_agent_engine() before deletion
        2. Execute deletion
        3. Register undo: redeploy from snapshot (with caveat)
        """
        ...

    async def query_agent_engine(self, resource_name: str, message: str) -> dict:
        """Send a message. Useful as smoke-test step."""
        ...
```

### 3.2 Trap implementations

Each trap from `llm-learnings.md` maps to explicit code:

| Trap | Location | Implementation |
|---|---|---|
| ADC vs gcloud auth | `preflight.check_adc()` | Called at top of every execute op |
| Firestore IAM missing | `deploy_agent_engine` pre-flight | Check binding; fail with `gcloud projects add-iam-policy-binding` command |
| Pipeline hiding failed deploy | `deploy_agent_engine` | Extract resource name via `RESOURCE_NAME_RE`, reject anything else |
| Resource name instability | `deploy_agent_engine` response | Return new resource_name + warn about Cloud Run env var update |
| Deploy looks stuck | `deploy_agent_engine` | Poll `list()` after timeout; store GCP op name in SQLite for resume |
| gcloud CLI surface inconsistency | All operations | Use SDK only; never shell out to gcloud |
| Secrets in env vars | `deploy_agent_engine` response | Warn if `API_KEY` or `SECRET` appears in env_var keys |

---

## Phase 4 — Claude Code skill

**Official docs finding:** Skills use `SKILL.md` with YAML frontmatter. The `context: fork` field runs the skill in an isolated subagent. `allowed-tools` pre-approves MCP tools. Dynamic context injection (`` !`command` ``) runs shell commands before Claude sees the content.

Two skills ship with g-code-mode:

### 4.1 `g-code-mode` (auto-triggered, general)

```
skills/g-code-mode/SKILL.md
```

```yaml
---
name: g-code-mode
description: >
  Operate Google Cloud infrastructure safely via g-code-mode MCP server.
  Use when the user asks to list, deploy, delete, or query Google Cloud
  resources (Cloud Run, Vertex AI Agent Engine, Firestore). Handles
  inquire (read-only discovery) and execute (validated mutations with undo).
allowed-tools: mcp__g-code-mode__code
---

## Active GCP context

- Project: !`gcloud config get-value project 2>/dev/null || echo "not set"`
- Account: !`gcloud config get-value account 2>/dev/null || echo "not set"`
- ADC status: !`gcloud auth application-default print-access-token >/dev/null 2>&1 && echo "configured" || echo "NOT CONFIGURED — run: gcloud auth application-default login"`

## How to use g-code-mode

Call the `code` MCP tool with an `async def run()` function that uses the
injected adapter functions. Always assign the final result to `return`.

### Read-only discovery (inquire pattern)

```python
async def run():
    engines = await list_agent_engines(project="my-project", location="us-central1")
    return engines
```

### Validated mutation (execute pattern)

Every execute call returns an `undo_recipe` alongside the result.
Always surface the undo recipe to the user after a successful mutation.

```python
async def run():
    result = await deploy_agent_engine(
        project="my-project",
        location="us-central1",
        display_name="my-agent",
        package_path="./agent/dist",
        requirements=["google-cloud-aiplatform>=1.112.0"],
        env_vars={"BRAVE_SEARCH_API_KEY": "..."},
    )
    return result  # includes result["undo_recipe"]
```

## Rules

- Never pass credentials or tokens into the code. ADC is used automatically.
- Always show the user the `undo_recipe` after a successful execute.
- If a deploy returns a new resource_name, remind the user to update any
  downstream config that references the old resource name.
- If ADC is not configured (see context above), stop and ask the user to
  run `gcloud auth application-default login` before proceeding.
```

### 4.2 `gcloud-execute` (user-invoked only, for explicit deployments)

```yaml
---
name: gcloud-execute
description: Execute a Google Cloud mutation via g-code-mode with full safety stack
disable-model-invocation: true
allowed-tools: mcp__g-code-mode__code
---

Execute the following Google Cloud operation safely:

$ARGUMENTS

Use the `code` MCP tool. Write an `async def run()` that calls the
appropriate adapter function. Return the full result including the
`undo_recipe`. Surface the undo recipe and any warnings to the user.
```

**Key docs learnings applied:**
- `disable-model-invocation: true` on the execute skill — prevents Claude from autonomously firing mutations (Cloudflare's `needsApproval` equivalent for skills).
- `allowed-tools: mcp__g-code-mode__code` — pre-approves the MCP tool so Claude doesn't prompt for permission each time.
- Dynamic context injection surfaces ADC status before Claude writes any code — prevents the ADC trap hitting at runtime.
- `context: fork` was considered but omitted: the skill needs conversation history to know what project the user is working in.

---

## Phase 5 — Distribution

### 5.1 Install path

```bash
# via uvx (no install)
uvx g-code-mode

# via pip
pip install g-code-mode
```

### 5.2 Claude Code MCP config

```json
{
  "mcpServers": {
    "g-code-mode": {
      "command": "uvx",
      "args": ["g-code-mode"]
    }
  }
}
```

### 5.3 Skill install

```bash
# Copy skills to personal skills directory
cp -r skills/g-code-mode ~/.claude/skills/
cp -r skills/gcloud-execute ~/.claude/skills/
```

Or documented in README for project-level install (`.claude/skills/`).

---

## Implementation sequence

| Step | What | Files | Validates |
|---|---|---|---|
| 1 | Scaffold + FastMCP server | `pyproject.toml`, `server.py` | `uvx g-code-mode` starts without error |
| 2 | Executor + truncation | `executor.py`, `truncate.py` | Unit tests: timeout, error capture, stdout capture |
| 3 | State manager | `state.py` | Unit tests: write/read/update operations table |
| 4 | Preflight + undo registry | `preflight.py`, `undo_registry.py` | Unit tests: ADC missing gives correct error |
| 5 | Agent Engine `list` + `get` | `agent_engine.py` | Integration: lists real resources against test project |
| 6 | Agent Engine `deploy` | `agent_engine.py` | Integration: deploys, captures resource name, polls list() |
| 7 | Agent Engine `delete` + undo | `agent_engine.py` | Integration: delete, verify undo recipe redeploys correctly |
| 8 | Agent Engine `query` | `agent_engine.py` | Integration: query returns response from engine |
| 9 | Trap tests | `test_agent_engine.py` | Unit: all 7 traps produce correct errors/warnings |
| 10 | Skills | `skills/` | Manual: Claude Code auto-triggers on "list my agent engines" |
| 11 | PyPI packaging | `pyproject.toml`, CI | `uvx g-code-mode` installs from PyPI |

---

## Open questions (pre-implementation)

1. **Namespace building** — at server startup, adapter functions are collected into the `code` tool's injected namespace. The tool docstring must enumerate them accurately. Should the docstring be generated dynamically from the registered adapters, or hand-authored per version? Recommendation: hand-authored for v1 (easier to read), generated for v2.

2. **Integration test project** — integration tests need a real GCP project. Recommendation: use a dedicated `g-code-mode-test` project, guard it with an env var (`G_CODE_MODE_TEST_PROJECT`), and skip integration tests if not set.

3. **Resume flow UX** — when a deploy times out, the GCP op name is stored in SQLite. How does the user trigger resume? Options: (a) automatic on next `list_agent_engines` call that detects in-flight state, (b) explicit `resume_deploy(op_id)` adapter function. Recommendation: (b) explicit, surfaced in the timeout error message.

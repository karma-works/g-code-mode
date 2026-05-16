<p align="center">
  <img src="assets/logo.svg" width="120" alt="g-code-mode logo"/>
</p>

<h1 align="center">g-code-mode</h1>

<p align="center">
  <strong>LLM-safe MCP server for Google Cloud operations</strong><br/>
  Curated adapters · Pre-flight dry-run · Paired undo · Snapshot restore · Retry with state tracking · Transactional rollback
</p>

<p align="center">
  <a href="https://pypi.org/project/g-code-mode/"><img src="https://img.shields.io/pypi/v/g-code-mode?color=4285f4" alt="PyPI"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-34a853" alt="License"/></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-ea4335" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/MCP-stdio-fbbc04" alt="MCP stdio"/>
</p>

---

LLMs are bad at Google Cloud. The official tooling exposes hundreds of raw endpoints with inconsistent naming, outdated documentation, and irreversible operations with no undo path.

`g-code-mode` fixes the surface. It is an open-source [MCP](https://modelcontextprotocol.io) server that gives any LLM coding assistant (Claude Code, Cursor, Windsurf) a safe, reliable, concise interface to Google Cloud — using the [CodeAct](https://machinelearning.apple.com/research/codeact) / [Cloudflare Code Mode](https://blog.cloudflare.com/code-mode-mcp/) pattern.

## How it works

Instead of registering dozens of individual tools, `g-code-mode` exposes a **single `code` tool**. The calling LLM writes a small `async def run()` function that orchestrates adapter calls. The server executes it in a sandboxed Python environment with injected adapter functions:

```python
# The LLM writes this inside the code tool:
async def run():
    engines = await list_agent_engines(project="my-project", location="us-central1")
    if not engines:
        return "No agent engines found"
    return await query_agent_engine(engines[0]["resource_name"], "hello")
```

Every mutating call goes through a **five-layer safety stack**:

1. **Pre-flight dry-run** — validate the plan before touching anything
2. **Paired undo action** — every operation registers its inverse, returned in the response
3. **Snapshot + restore** — resource state captured before mutation
4. **Retry with state tracking** — interrupted operations stored in local SQLite, resumable
5. **Transactional rollback** — if step N fails, steps 1–N-1 are automatically undone

## Differences from existing solutions

| | g-code-mode | [googleapis/gcloud-mcp](https://github.com/googleapis/gcloud-mcp) | [GoogleCloudPlatform/cloud-run-mcp](https://github.com/GoogleCloudPlatform/cloud-run-mcp) | [Google managed MCP](https://docs.cloud.google.com/mcp/overview) | [eniayomi/gcp-mcp](https://github.com/eniayomi/gcp-mcp) |
|---|---|---|---|---|---|
| Code-mode single tool | ✓ | — | — | — | partial |
| LLM-curated surface (traps hidden) | ✓ | — | — | — | — |
| Pre-flight dry-run | ✓ | — | — | — | — |
| Paired undo action | ✓ | — | — | — | — |
| Snapshot + restore | ✓ | — | — | — | — |
| Retry with state tracking | ✓ | — | — | — | — |
| Transactional rollback | ✓ | — | — | — | — |
| Vertex AI Agent Engine | ✓ | — | — | — | — |
| Cloud Run | ✓ | — | ✓ | ✓ | — |
| Firestore | ✓ | — | — | ✓ | — |
| License | Apache-2.0 | Apache-2.0 | Apache-2.0 | closed | MIT |

### Why not just use the existing tools?

**`googleapis/gcloud-mcp`** wraps the gcloud CLI as MCP tools. It blocks a few dangerous commands but exposes the same complex surface the LLM was already struggling with. No curated abstraction, no undo, no safety stack. Preview only, not officially supported.

**`GoogleCloudPlatform/cloud-run-mcp`** is a solid Cloud Run deployment tool. No traffic splitting, no rollback, no undo. Does not cover Vertex AI Agent Engine or Firestore.

**Google managed remote MCP servers** cover 40+ services including Cloud Run and Firestore. They are raw API mirrors with no curated surface and no undo. Closed source; cannot be extended or self-hosted.

## Adapters

| Service | Status | Operations |
|---|---|---|
| **Vertex AI Agent Engine** | **v0.1** | list, get, deploy (with undo), delete (with snapshot), query |
| **Cloud Run** | **v0.2** | list, get, list revisions, deploy revision (with undo), set traffic, rollback, logs |
| **Firestore** | **v0.3** | list collections, list/get/query documents, subcollections, set/update/delete (with undo) |

## Installation

### Prerequisites

- Python 3.12+
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Application Default Credentials configured:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

### Install the MCP server

```bash
# Using uvx (no install required — recommended)
uvx g-code-mode

# Or install via pip
pip install g-code-mode
```

### Configure your MCP client

**Claude Code** (`.claude/mcp.json` or `~/.claude/mcp.json`):

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

**Cursor** (`.cursor/mcp.json`):

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

### Install the Claude Code skills (optional)

Skills give Claude Code context about how to use g-code-mode and pre-approve the MCP tool so you don't get prompted each time:

```bash
# Personal install — available across all your projects
cp -r skills/g-code-mode ~/.claude/skills/
cp -r skills/gcloud-execute ~/.claude/skills/
```

Or commit the `skills/` directory to your project's `.claude/skills/` for project-level install.

After install, Claude Code picks up the skills automatically. You can also invoke them directly:

```
/g-code-mode        # general Google Cloud operations
/gcloud-execute     # explicit mutation with full safety stack
```

## Usage

Once the MCP server is running and your client is configured, use natural language:

> "List all Agent Engine resources in my project"

> "Deploy the agent at `./agent/dist` to `us-central1` as `my-agent`"

> "What Agent Engines are running and what are their resource names?"

The `code` tool is called automatically. For mutations, you'll see an `undo_recipe` in the response — keep it to hand.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `G_CODE_MODE_STATE_PATH` | `~/.g-code-mode/state.db` | SQLite state file location |
| `G_CODE_MODE_EXEC_TIMEOUT` | `60` | Seconds before code execution times out |

## Development

```bash
git clone https://github.com/karma-works/g-code-mode
cd g-code-mode
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## License

Apache-2.0. Portions of the adapter implementations reference patterns from
[`googleapis/gcloud-mcp`](https://github.com/googleapis/gcloud-mcp) and
[`GoogleCloudPlatform/cloud-run-mcp`](https://github.com/GoogleCloudPlatform/cloud-run-mcp),
both Apache-2.0.

# ADR-004: MCP Transport — stdio (v1)

**Status:** Decided  
**Date:** 2026-05-16

## Context

The MCP specification supports two transports: stdio (process-based) and HTTP with Server-Sent Events (network-based). The choice determines how LLM clients (Claude Code, Cursor, Windsurf) connect to g-code-mode and how the server is installed and authenticated.

## Decision

Use **stdio** transport for v1.

The server is invoked as a subprocess by the MCP client. Communication happens over stdin/stdout. Installation is a single entry in the client's MCP config:

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

The Python MCP SDK's `stdio_server()` context manager handles the transport. No networking code needed.

## Rationale

- **Standard for local tools.** Every major MCP client (Claude Code, Cursor, Windsurf, Claude Desktop) supports stdio as the primary transport for locally installed servers. HTTP/SSE is supported but less commonly configured.
- **Zero auth surface.** The server inherits the developer's local GCP credentials (ADC) and runs under their OS user. No API keys, no token management, no TLS certificates.
- **Simplest install path.** `uvx g-code-mode` (or `pip install g-code-mode`) is a one-line setup. HTTP/SSE would require running a persistent daemon, managing a port, and handling firewall rules.
- **The MCP Python SDK supports both.** Switching from stdio to HTTP/SSE later requires changing one line in `server.py` (the transport context manager). Adapter logic is transport-agnostic.

## What This Option Does NOT Do Well

- **Not shareable.** A stdio server runs per-user, per-machine. A team that wants shared access to a central g-code-mode instance needs HTTP/SSE. Out of scope for v1.
- **No remote access.** The server cannot be reached from a remote Claude agent or a CI pipeline. Again, out of scope for v1.
- **Process lifetime tied to client.** The server starts when the MCP client starts and stops when it stops. Long-running background polls (e.g., waiting on a Vertex AI deploy) must be managed within the server process or handed off to the SQLite state layer for resume on next invocation.

## Consequences

- Entry point: `g_code_mode/server.py` with `mcp.server.stdio.stdio_server()`.
- Distribution: PyPI package (`g-code-mode`), installable via `pip` or `uvx`.
- Auth: developer must have `gcloud auth application-default login` configured. The server checks for ADC on startup and prints a clear error if not found.
- v2 consideration: add `--transport http --port 8080` flag when remote/shared deployment becomes a requirement. No adapter changes needed.

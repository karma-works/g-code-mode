# ADR-001: Language and Runtime — Python

**Status:** Decided  
**Date:** 2026-05-16

## Context

g-code-mode is an MCP server that exposes a code tool to calling LLMs. The server runs LLM-generated orchestration code and dispatches the resulting adapter calls to Google Cloud services. Two languages are credible: TypeScript and Python.

The code-mode pattern (Cloudflare, CodeAct) was designed in JavaScript/TypeScript, and the `@cloudflare/codemode` package is a TypeScript library. However, the Google Cloud service adapters that form the core of g-code-mode — especially Vertex AI Agent Engine — are Python-first. The authoritative Google Cloud AI Platform client (`google-cloud-aiplatform`) is Python. The Google ADK is Python. The Node.js equivalents are thinner and lag behind the Python SDK in coverage of newer features (Agent Engine deployment, ADK integration).

Alternatives considered: TypeScript throughout, Python throughout, TypeScript server with Python adapter subprocess.

## Decision

Python throughout: MCP server, sandbox, and all adapters.

## Rationale

- The Vertex AI Agent Engine Python SDK is the canonical deployment interface. Using it directly avoids translation layers and ensures access to the full API surface, including features not yet exposed in the Node.js client.
- The Python MCP SDK (`mcp`) is mature and supports both stdio and HTTP/SSE transports.
- The Google ADK (used in GapHunter and the primary framework for Vertex AI agents) is Python-only. Adapters that introspect or deploy ADK agents must be Python.
- LLM-generated orchestration code in Python gives the calling model access to the Python standard library and `google-cloud-*` client libraries directly in the sandbox — useful for data processing in `inquire` results.
- The code-mode pattern is not language-dependent. The mechanism (single tool, injected callables, exec, result return) maps cleanly to Python.

## What This Option Does NOT Do Well

- The JavaScript/TypeScript code-mode ecosystem (`@cloudflare/codemode`, `DynamicWorkerExecutor`) cannot be reused. The sandbox must be implemented from scratch in Python.
- TypeScript's type system provides stronger compile-time guarantees for the tool interface. Python type hints are enforced only with a linter (`mypy`, `pyright`).
- If g-code-mode is later extended to non-Google cloud providers with stronger Node.js SDK support, a Python server may feel unnatural.

## Consequences

- Use `mcp` (Python MCP SDK) for the server.
- Use `google-cloud-aiplatform`, `google-cloud-run`, `google-cloud-firestore` as adapter clients.
- Enforce types with `pyright` in strict mode.
- The sandbox executes Python code via `exec()` with an injected callable namespace (see ADR-002).
- Target Python 3.12+ (consistent with GapHunter).

# ADR-005: Adapter Architecture — Built-in, No Plugin System

**Status:** Decided  
**Date:** 2026-05-16

## Context

g-code-mode's value comes from curated, LLM-safe adapter implementations — not from exposing raw APIs. Each adapter requires:
- A curated tool surface with LLM-legible names and descriptions.
- Documented trap handling baked into the implementation.
- Undo action registration for every mutating operation.
- Pre-flight validation logic.

The question is whether adapters should be a plugin system (community-contributed, loaded at runtime from separate packages) or a built-in curated set (shipped and maintained in the main repository).

Alternatives considered: plugin system with a registry, built-in adapters only, monorepo with separate adapter packages.

## Decision

Built-in adapters only for v1. All adapters live in the main `g-code-mode` repository under `g_code_mode/adapters/`. There is no runtime plugin loading, no adapter registry, no third-party adapter packages.

```
g_code_mode/
  adapters/
    vertex_ai/
      agent_engine.py
    cloud_run/        # planned
    firestore/        # planned
  core/
    executor.py
    undo_registry.py
    preflight.py
    state.py
    server.py
```

## Rationale

- **Quality over breadth.** The entire value proposition is that g-code-mode's adapters are safer and more LLM-legible than raw API wrappers. A plugin system incentivises quantity; built-in enforces quality. Every adapter must meet the same bar: curated surface, trap handling, undo registration.
- **Trap knowledge is hard to document.** The GapHunter `llm-learnings.md` took months of production failures to accumulate. That knowledge must be embedded in the adapter code, not left to plugin authors who may not have encountered the same traps.
- **Simpler architecture.** No adapter discovery, no version compatibility matrix, no plugin API to maintain. The adapter interface is an internal Python protocol (`Adapter` base class), free to evolve without semver concerns.
- **Community contributions via PRs.** Contributors who want to add a new Google Cloud service open a PR to the main repository. The maintainers review trap coverage and undo completeness before merging. This is the right gatekeeping mechanism for safety-critical adapter code.

## What This Option Does NOT Do Well

- **Slower surface expansion.** Adding Cloud Build, Cloud Tasks, or IAM requires a PR and review cycle. A plugin system would let the community move faster. Accepted trade-off: correctness over coverage speed.
- **No private adapters.** An organisation with proprietary internal Google Cloud services cannot build a private adapter without forking. If this becomes a real need, the `Adapter` base class and registration mechanism can be made public-facing without a full plugin system.

## Consequences

- The `Adapter` base class is an internal interface. It is not part of the public API and has no stability guarantee between versions.
- Day-one adapters: Vertex AI Agent Engine (v1), Cloud Run (v1.1), Firestore (v1.2).
- New adapter proposals must include: curated tool surface, at least one trap test, undo registration for every mutating operation.
- The `code` tool's injected namespace is built at server startup by collecting all registered adapters. No hot-reload needed.

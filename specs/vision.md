# Vision: g-code-mode

**Status:** Draft — 2026-05-16

---

## The problem

LLMs are bad at Google Cloud.

The official Google MCP server and the `gcloud` CLI expose hundreds of endpoints with inconsistent naming, outdated documentation, and silent foot-guns — operations that succeed but leave infrastructure in a broken or irreversible state. Models that encounter this surface hallucinate endpoints, misread auth flows, and fire-and-forget mutations without any proof of success.

This is not a model capability problem. It is a surface design problem. No human operator would accept a control panel with 2,500 unlabelled switches, no undo button, and no confirmation dialog. LLMs shouldn't have to either.

---

## The insight

Two lines of research point to the same fix.

**CodeAct** (Apple ML, 2024) showed that LLMs outperform traditional tool-calling by up to 20% when they write executable code instead of JSON blobs. Code lets the model use loops, conditionals, and composition — things that multi-step tool calling cannot express without many expensive round-trips.

**Cloudflare Code Mode** (2025) applied this to MCP: instead of registering 2,500 endpoints as individual tools (which would consume over a million tokens of context), expose a single `code` tool. The model writes a small program; the server runs it in a sandbox. Token cost collapses by 99.9%.

The combination gives us the shape: a **code-mode MCP server** that hides a curated set of Google Cloud adapters behind a minimal, LLM-optimised surface, and executes generated code safely on the server side.

---

## What g-code-mode is

**g-code-mode** is an open-source MCP server that gives any LLM coding assistant a safe, reliable, and concise interface to Google Cloud.

It exposes a single `code` tool. The calling LLM writes orchestration code that calls two high-level operations — `inquire` and `execute` — plus lower-level typed adapter calls for specific services. The server runs that code in a sandbox, dispatching adapter calls back to the host, and returns structured results.

It is **infrastructure-facing, not user-facing**. There is no frontend. The consumers are LLMs: Claude Code, Cursor, Windsurf, or any MCP-compatible agent.

---

## The interface

### `inquire(query: string)`

Natural-language read-only discovery. Expands internally into one or more adapter calls — listing resources, reading configuration, checking IAM bindings, comparing states — and returns a structured answer. Never mutates.

Example:
```
inquire("Which Cloud Run services in project gaphunter-496315 have public ingress?")
```

### `execute(command: string)`

Natural-language execution with a full safety stack. Every execute call goes through:

1. **Pre-flight dry-run** — validate the plan before touching anything.
2. **Paired undo action** — every operation registers its inverse before running. The undo recipe is returned in the response.
3. **Snapshot + restore** — relevant resource state is captured before mutation so a rollback `execute` call can reconstruct it.
4. **Retry with state tracking** — if execution is interrupted, the server can resume from the last confirmed step.
5. **Transactional rollback** — if step N of a multi-step plan fails, steps 1 through N-1 are automatically undone.

Example:
```
execute("Roll out image gcr.io/gaphunter-496315/app:sha-abc to the gaphunter Cloud Run service in europe-west6 with 10% traffic")
```

---

## The adapter model

Adapters are the knowledge layer. Each adapter wraps a Google Cloud service with:

- A curated, LLM-legible tool surface (not a raw API mirror).
- Documented traps and known failure modes baked into the implementation.
- Undo action registrations for every mutating operation.

**Day-one adapters** (drawn from real operational pain on GapHunter):

| Service | Scope |
|---|---|
| Cloud Run | Deploy, list, describe, traffic splitting, rollback, delete |
| Firestore | Collections, documents, indexes, rules |
| Vertex AI | Agent Engine list, deploy, query, delete |

Additional adapters follow the same pattern and can be contributed by the community.

---

## What it is not

- Not a frontend or dashboard. No UI.
- Not a replacement for `gcloud` for humans. Humans can keep using `gcloud`.
- Not Google-specific LLM tooling. Works with any model that speaks MCP — Claude, GPT, Gemini.
- Not a managed service. Install it locally or self-host; you bring your own GCP credentials.
- Not a general-purpose cloud abstraction. Google Cloud only, and done well.

---

## Who it is for

Any developer who uses an AI coding assistant and runs workloads on Google Cloud. The server is installed once, pointed at a GCP project, and from that point any LLM session that uses it can operate Google Cloud safely — without the developer having to guide the model around every undocumented trap.

The first target user is the developer who has already been burned: who has watched an LLM call the wrong endpoint, get confused by ADC vs OAuth, or deploy successfully to the wrong service. That pain is the product's reason to exist.

---

## Success looks like

An LLM agent can:

1. Ask "what is running in my project and what does it cost?" and get a correct, structured answer.
2. Deploy a new container revision with traffic splitting and receive a working rollback command in the same response.
3. Make a mistake — wrong service, wrong region — and undo it in one follow-up call, with the server guaranteeing the prior state is restored.

And the developer watching did not have to correct the model once.

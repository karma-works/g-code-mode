# Learnings from Cloudflare MCP Source

**Source:** https://github.com/cloudflare/mcp  
**Date reviewed:** 2026-05-16  
**Relevance:** Direct design reference for g-code-mode architecture and implementation.

---

## 1. Tool descriptions must include concrete code examples

Cloudflare's `execute` tool description embeds a full, production-quality code example showing multipart/form-data Worker deployment. The `search` tool description includes three concrete query examples.

The quality of LLM-generated orchestration code depends almost entirely on the examples in the tool description. Generic descriptions produce generic (broken) code. Examples that show real patterns produce usable code.

**Transfer:** Every g-code-mode tool description must include at least one runnable example. The `code` tool description must enumerate all available adapter functions with their exact signatures and show a realistic multi-step call pattern.

---

## 2. Wrap generated code as an async function, don't inject into exec scope

Cloudflare wraps the LLM's code as:

```js
const result = await (${code})();
```

The LLM writes an async arrow function that `return`s its result. This is cleaner than injecting variables into an `exec()` namespace because the return value is explicit and the LLM's intent is unambiguous.

**Transfer:** For Python, use the same pattern. The LLM writes an `async def run()` function. The server compiles and calls it:

```python
exec(compile(code + "\nimport asyncio\n_result = asyncio.run(run())", "<llm>", "exec"), namespace)
result = namespace["_result"]
```

Or simpler: require the LLM to write a top-level `async def run()` and call it via `asyncio.run()`. Avoids the `result` variable convention ambiguity.

---

## 3. Credentials never enter the execution namespace

Cloudflare's API token is injected into the host-side `request()` method. The executor passes the token to the Worker's binding, not into the code itself. The user's code can only make calls via `cloudflare.request()` — it never holds the token directly.

**Transfer:** GCP credentials (ADC token, service account key) must never appear in the `exec()` namespace. Adapter methods handle auth internally using `google.auth.default()`. The LLM code calls `list_agent_engines(project="X")` — it never sees a credential.

---

## 4. Hard truncation with actionable guidance

```typescript
const MAX_TOKENS = 6000
// ...
return `${truncated}\n\n--- TRUNCATED ---\nResponse was ~${estimatedTokens} tokens. Use more specific queries.`
```

Without truncation, a `list` call returning many resources blows up the LLM context. The truncation message tells the LLM what happened and what to do differently.

**Transfer:** Implement `truncate_response(content, max_tokens=6000)` in `core/truncate.py`. Apply to every `inquire` and `execute` result before returning to the LLM. The truncation message must suggest a narrowing action specific to the operation (e.g., "Filter by location or add a display_name filter to narrow results").

---

## 5. Multi-project awareness in tool descriptions

When a user has multiple Cloudflare accounts, the execute tool description dynamically includes the list and says: "Required — this token has access to multiple accounts: [list]."

**Transfer:** At server startup, detect available GCP projects from ADC and include them in the `code` tool description. If the user has one project, hardcode it as the default. If multiple, list them and require the LLM to pass `project` explicitly.

---

## 6. Search tool enables discovery without loading the full spec

The `search` tool lets the LLM query the OpenAPI spec by writing JavaScript against a pre-resolved spec object. The LLM doesn't need to know all 2,500 endpoints upfront — it discovers them on demand.

**Transfer:** For g-code-mode, the adapter surface is curated (not auto-generated from an API spec), so the LLM already knows all available functions from the `code` tool description. However, for `inquire`, the LLM may need to explore what data is available before forming a query. Consider an `inquire("list available adapter tools")` meta-query that returns the current tool registry.

---

## 7. Error messages from executed code must be surfaced verbatim

```typescript
{ result: undefined, err: err.message, stack: err.stack }
// ...
if (response.err) { throw new Error(response.err) }
```

The error from the sandbox propagates back to the LLM as a tool error. The LLM uses it to self-correct on the next turn.

**Transfer:** In the Python `exec()` wrapper, catch all exceptions, capture the traceback, and return it in `ExecResult.error`. The MCP tool must return `isError=True` with the full traceback. A truncated or swallowed error prevents self-correction.

---

## 8. UUID execution context naming

Each Cloudflare Worker execution gets a UUID name (`cloudflare-api-{uuid}`). Prevents state bleed between executions.

**Transfer:** Each `exec()` call gets a fresh namespace dict. No shared mutable state between executions. The undo registry and state DB are separate from the execution namespace.

---

## 9. Spec is pre-processed offline, not at query time

$refs are resolved and product tags are extracted in a scheduled background job. The LLM's `search` queries run against an already-clean, fully-resolved structure.

**Transfer:** g-code-mode's adapter "spec" is static Python code, not a fetched API spec. But if we ever add a discovery layer (e.g., auto-generating the adapter surface from a Google Discovery API document), pre-process it at startup, not per-query.

---

## Pitfalls to avoid

| Pitfall | Cloudflare's approach | g-code-mode mitigation |
|---|---|---|
| LLM writes bad code with no examples | Embeds examples in every tool description | Required: code examples in all tool descriptions |
| Credential leakage into sandbox | Token injected to host binding only | ADC handled inside adapter methods, never in namespace |
| Unbounded response size | Hard truncate at 6K tokens | `truncate_response()` on every result |
| Silent sandbox errors | Re-throws with full error + stack | Return full traceback in `ExecResult.error` |
| State bleed between executions | UUID-named workers | Fresh `namespace = {}` per exec call |
| LLM doesn't know what it can call | Tool description lists all products | `code` tool description enumerates all adapter functions |

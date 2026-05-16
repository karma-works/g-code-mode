# ADR-002: Code Execution Sandbox — exec() with Injected Namespace

**Status:** Decided  
**Date:** 2026-05-16

## Context

g-code-mode exposes a single `code` tool to the calling LLM. The LLM writes a Python function that orchestrates one or more adapter calls (`inquire`, `execute`, and lower-level typed tools). The server must execute that code and route the tool calls back to the adapter layer.

The execution mechanism must:
1. Run arbitrary LLM-generated Python code.
2. Inject callable adapter tools into the code's scope.
3. Capture the return value and any stdout/stderr.
4. Enforce a timeout.
5. Prevent runaway code from crashing the server.

Alternatives considered: `exec()` with injected namespace, subprocess with JSON-RPC routing, `RestrictedPython`, Docker container per execution.

## Decision

Use Python's `exec()` with an injected callable namespace, wrapped in a `concurrent.futures.ThreadPoolExecutor` with a timeout.

```python
import concurrent.futures
import asyncio

async def run_code(code: str, tools: dict[str, Callable]) -> ExecResult:
    namespace = {"__builtins__": __builtins__, **tools}
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = loop.run_in_executor(pool, _exec_sync, code, namespace)
        try:
            return await asyncio.wait_for(future, timeout=60.0)
        except asyncio.TimeoutError:
            return ExecResult(error="Execution timed out after 60s")

def _exec_sync(code: str, namespace: dict) -> ExecResult:
    stdout_capture = io.StringIO()
    with contextlib.redirect_stdout(stdout_capture):
        exec(compile(code, "<llm_code>", "exec"), namespace)
    return ExecResult(result=namespace.get("result"), logs=stdout_capture.getvalue())
```

Tool calls from the LLM code invoke the injected callables directly. No inter-process communication needed.

## Rationale

- **Simplicity.** The adapter tools are already Python functions in the same process. Injecting them into `exec()` scope requires no IPC protocol, no serialization, no subprocess management.
- **Full SDK access.** Injected tools can use the complete `google-cloud-*` SDK surface, including streaming responses and async operations, without serialization constraints.
- **Appropriate threat model.** g-code-mode is a local developer tool running under the developer's own credentials. The LLM generating the code is a trusted model (Claude, Gemini) controlled by the developer. The threat is reliability (infinite loops, bad code), not adversarial injection from an untrusted third party. A timeout handles the former.
- **Precedent.** CodeAct (Apple ML) uses an embedded interpreter in the same process. The isolation guarantee is the LLM's own correctness, not a kernel boundary.

## What This Option Does NOT Do Well

- **Process isolation.** A bug in LLM-generated code that corrupts the Python heap or calls `os.exit()` will affect the server process. Mitigation: validate generated code structure before exec (check for obvious dangerous patterns); restart the server automatically if it crashes (use a process supervisor or Claude Code's native restart).
- **Memory limits.** A runaway allocation in generated code is not bounded by the executor. Mitigation: the 60-second timeout covers infinite loops; memory abuse is considered an acceptable risk for a local tool.
- **Not suitable for multi-tenant deployment.** If g-code-mode is ever deployed as a shared remote server, this sandbox must be replaced with subprocess isolation or a container-per-execution model. This decision should be revisited at that point.

## Consequences

- The `code` tool receives a Python source string from the LLM. The convention is that the LLM assigns its final result to a variable named `result` in the module scope.
- Injected tool names must be valid Python identifiers. Any adapter tool with a hyphen or dot in its name is sanitised on registration.
- Execution timeout is 60 seconds, configurable via `G_CODE_MODE_EXEC_TIMEOUT` env var.
- Stdout from generated code is captured and returned in `ExecResult.logs`.
- The server logs all executed code at DEBUG level for auditability.

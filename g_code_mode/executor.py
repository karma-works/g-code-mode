"""Code execution sandbox — exec() with injected namespace (ADR-002)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import traceback
from dataclasses import dataclass, field
from typing import Any

from g_code_mode.truncate import truncate_response

_TIMEOUT = float(os.environ.get("G_CODE_MODE_EXEC_TIMEOUT", "60"))


@dataclass
class ExecResult:
    output: str
    error: str | None = None
    logs: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


async def run_code(script: str, namespace: dict[str, Any]) -> ExecResult:
    """
    Compile and run `script` inside a fresh namespace that contains `namespace`.

    The script must define `async def run()`. Its return value is captured,
    truncated, and returned as ExecResult.output. Stdout is captured into logs.
    Full tracebacks are returned in ExecResult.error so the LLM can self-correct.
    """
    local_ns: dict[str, Any] = {**namespace}
    stdout_buf = io.StringIO()

    try:
        compiled = compile(script, "<llm_code>", "exec")
    except SyntaxError:
        return ExecResult(output="", error=traceback.format_exc())

    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compiled, local_ns)  # noqa: S102

            run_fn = local_ns.get("run")
            if run_fn is None or not asyncio.iscoroutinefunction(run_fn):
                return ExecResult(
                    output="",
                    error="Script must define `async def run()`. Nothing else is executed.",
                    logs=stdout_buf.getvalue(),
                )

            result = await asyncio.wait_for(run_fn(), timeout=_TIMEOUT)

    except asyncio.TimeoutError:
        return ExecResult(
            output="",
            error=f"Execution timed out after {_TIMEOUT:.0f}s. Simplify the operation or add filters.",
            logs=stdout_buf.getvalue(),
        )
    except Exception:
        return ExecResult(
            output="",
            error=traceback.format_exc(),
            logs=stdout_buf.getvalue(),
        )

    return ExecResult(
        output=truncate_response(result),
        logs=stdout_buf.getvalue(),
    )

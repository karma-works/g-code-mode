"""Tests for the exec() sandbox."""

import pytest

from g_code_mode.executor import ExecResult, run_code


@pytest.mark.asyncio
async def test_basic_run():
    result = await run_code("async def run():\n    return 42", {})
    assert result.ok
    assert "42" in result.output


@pytest.mark.asyncio
async def test_injected_callable():
    async def my_tool(x: int) -> int:
        return x * 2

    script = "async def run():\n    return await my_tool(21)"
    result = await run_code(script, {"my_tool": my_tool})
    assert result.ok
    assert "42" in result.output


@pytest.mark.asyncio
async def test_missing_run_function():
    result = await run_code("x = 1", {})
    assert not result.ok
    assert "async def run()" in (result.error or "")


@pytest.mark.asyncio
async def test_syntax_error_captured():
    result = await run_code("async def run(\n    return", {})
    assert not result.ok
    assert result.error is not None


@pytest.mark.asyncio
async def test_runtime_exception_captured():
    script = "async def run():\n    raise ValueError('boom')"
    result = await run_code(script, {})
    assert not result.ok
    assert "boom" in (result.error or "")
    assert "ValueError" in (result.error or "")


@pytest.mark.asyncio
async def test_stdout_captured():
    script = "async def run():\n    print('hello stdout')\n    return 'done'"
    result = await run_code(script, {})
    assert result.ok
    assert "hello stdout" in result.logs


@pytest.mark.asyncio
async def test_no_state_bleed_between_calls():
    script_a = "x = 99\nasync def run():\n    return x"
    script_b = "async def run():\n    return x"
    await run_code(script_a, {})
    result = await run_code(script_b, {})
    assert not result.ok  # x should not be visible in second exec


@pytest.mark.asyncio
async def test_timeout(monkeypatch):
    monkeypatch.setenv("G_CODE_MODE_EXEC_TIMEOUT", "0.1")
    import importlib

    import g_code_mode.executor as ex

    importlib.reload(ex)
    script = "import asyncio\nasync def run():\n    await asyncio.sleep(10)"
    result = await ex.run_code(script, {})
    assert not result.ok
    assert "timed out" in (result.error or "").lower()

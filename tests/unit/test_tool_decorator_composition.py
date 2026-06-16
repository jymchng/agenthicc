"""Smoke tests: @tool() decorator composability with @timeout and @retry.

Confirms that standard Python function-wrapper decorators using functools.wraps
compose correctly with @tool():
  - TOOL_META (__lauren_ai_tool__) is preserved on the outermost wrapper
  - Schema generation (name, signature, type hints) survives all wrapper layers
  - Timeout fires at the correct threshold
  - Retry calls the underlying function the correct number of times
  - Three-layer composition (@tool @retry @timeout) works end-to-end
  - __dict__.update semantics: wrapper holds the same ToolMeta object (not a copy)
"""
from __future__ import annotations

import asyncio
import functools
import pytest

pytestmark = pytest.mark.unit


# ── Decorator helpers (production-quality implementations) ────────────────────

def timeout(seconds: float):
    """Wrap an async function with asyncio.wait_for."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
        return wrapper
    return decorator


def retry(max_attempts: int = 3, backoff: float = 0.0):
    """Retry an async function up to *max_attempts* times with exponential backoff."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    if attempt < max_attempts - 1:
                        if backoff:
                            await asyncio.sleep(backoff * (2 ** attempt))
                    else:
                        raise
        return wrapper
    return decorator


# ── TOOL_META presence ────────────────────────────────────────────────────────

def test_tool_meta_present_when_timeout_above_tool():
    """@timeout above @tool: TOOL_META survives on the outer wrapper."""
    from lauren_ai._tools import tool, TOOL_META

    @timeout(30)
    @tool()
    async def fn_a(x: str) -> dict:
        """Tool A."""
        return {"x": x}

    meta = getattr(fn_a, TOOL_META, None)
    assert meta is not None, "TOOL_META must be on the wrapper when @timeout is outermost"
    assert meta.name == "fn_a"
    assert meta.is_async is True


def test_tool_meta_present_when_tool_above_timeout():
    """@tool above @timeout: TOOL_META survives when @timeout is innermost."""
    from lauren_ai._tools import tool, TOOL_META

    @tool()
    @timeout(30)
    async def fn_b(y: int) -> str:
        """Tool B."""
        return str(y)

    meta = getattr(fn_b, TOOL_META, None)
    assert meta is not None, "TOOL_META must be on the wrapper when @tool is outermost"
    assert meta.name == "fn_b"
    assert meta.is_async is True


def test_tool_meta_present_through_three_layers():
    """@tool @retry @timeout: TOOL_META survives three decorator layers."""
    from lauren_ai._tools import tool, TOOL_META

    @tool()
    @retry(max_attempts=2)
    @timeout(5.0)
    async def fn_c(z: float) -> bool:
        """Tool C."""
        return z > 0

    meta = getattr(fn_c, TOOL_META, None)
    assert meta is not None
    assert meta.name == "fn_c"


# ── Schema correctness ────────────────────────────────────────────────────────

def test_signature_preserved_through_timeout():
    """inspect.signature and type hints survive @timeout wrapping."""
    import inspect
    import typing
    from lauren_ai._tools import tool

    @timeout(30)
    @tool()
    async def fn_d(query: str, limit: int = 10) -> list:
        """Tool D."""
        return []

    sig = inspect.signature(fn_d)
    assert list(sig.parameters) == ["query", "limit"]

    hints = typing.get_type_hints(fn_d)
    assert hints["query"] is str
    assert hints["limit"] is int


def test_json_schema_params_correct_after_wrapping():
    """ToolMeta.parameters.input_schema reflects original function signature."""
    from lauren_ai._tools import tool, TOOL_META

    @timeout(30)
    @tool()
    async def fn_e(path: str, encoding: str = "utf-8") -> dict:
        """Tool E."""
        return {}

    meta = getattr(fn_e, TOOL_META)
    props = meta.parameters.get("input_schema", {}).get("properties", {})
    assert "path" in props, f"'path' missing from schema: {props}"
    assert "encoding" in props, f"'encoding' missing from schema: {props}"


def test_dunder_name_preserved():
    """__name__ is the original function name after wrapping."""
    from lauren_ai._tools import tool

    @timeout(30)
    @tool()
    async def my_named_tool(x: str) -> str:
        return x

    assert my_named_tool.__name__ == "my_named_tool"


def test_wrapped_attribute_set():
    """@timeout sets __wrapped__ pointing to the @tool()-decorated function."""
    from lauren_ai._tools import tool

    @timeout(30)
    @tool()
    async def fn_f(x: str) -> str:
        return x

    assert hasattr(fn_f, "__wrapped__"), "__wrapped__ should be set by functools.wraps"


# ── Timeout behaviour ─────────────────────────────────────────────────────────

async def test_timeout_allows_fast_calls():
    """A call that completes within the timeout succeeds normally."""
    from lauren_ai._tools import tool

    @tool()
    @timeout(1.0)
    async def fast_tool() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result = await fast_tool()
    assert result == "done"


async def test_timeout_fires_on_slow_calls():
    """A call that exceeds the timeout raises asyncio.TimeoutError."""
    from lauren_ai._tools import tool

    @tool()
    @timeout(0.05)   # 50 ms limit
    async def slow_tool() -> str:
        await asyncio.sleep(0.5)   # 500 ms — well over the limit
        return "should not reach"

    with pytest.raises(asyncio.TimeoutError):
        await slow_tool()


# ── Retry behaviour ───────────────────────────────────────────────────────────

async def test_retry_succeeds_after_failures():
    """Retry eventually succeeds when the tool stops failing."""
    from lauren_ai._tools import tool

    call_count = 0

    @tool()
    @retry(max_attempts=3, backoff=0.0)
    async def flaky_tool() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError(f"failure #{call_count}")
        return "ok"

    result = await flaky_tool()
    assert result == "ok"
    assert call_count == 3


async def test_retry_raises_after_exhausting_attempts():
    """Retry re-raises after all attempts are exhausted."""
    from lauren_ai._tools import tool

    call_count = 0

    @tool()
    @retry(max_attempts=2, backoff=0.0)
    async def always_fails() -> str:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("always bad")

    with pytest.raises(RuntimeError, match="always bad"):
        await always_fails()

    assert call_count == 2


# ── Three-layer composition ───────────────────────────────────────────────────

async def test_three_layer_composition_end_to_end():
    """@tool() @retry(3) @timeout(1.0) works end-to-end."""
    from lauren_ai._tools import tool, TOOL_META

    attempt_count = 0

    @tool()
    @retry(max_attempts=3, backoff=0.0)
    @timeout(1.0)
    async def composed_tool(n: int) -> str:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < n:
            raise ValueError(f"not yet ({attempt_count})")
        return f"success on attempt {attempt_count}"

    meta = getattr(composed_tool, TOOL_META, None)
    assert meta is not None
    assert meta.name == "composed_tool"

    result = await composed_tool(2)
    assert result == "success on attempt 2"
    assert attempt_count == 2


# ── __dict__.update mechanics ─────────────────────────────────────────────────

def test_functools_wraps_copies_tool_meta_by_reference():
    """functools.wraps copies TOOL_META via __dict__.update — same object."""
    from lauren_ai._tools import tool, TOOL_META

    @tool()
    async def base(x: str) -> str:
        return x

    @functools.wraps(base)
    async def wrapper(*args, **kwargs):
        return await base(*args, **kwargs)

    assert TOOL_META in wrapper.__dict__, "TOOL_META must be in wrapper.__dict__"
    assert wrapper.__dict__[TOOL_META] is base.__dict__[TOOL_META], (
        "TOOL_META must be the same ToolMeta object (not a copy)"
    )


def test_tool_meta_not_present_without_functools_wraps():
    """Without functools.wraps, TOOL_META is NOT copied — the gap this solves."""
    from lauren_ai._tools import tool, TOOL_META

    @tool()
    async def base(x: str) -> str:
        return x

    # Deliberately broken wrapper — no functools.wraps
    async def bad_wrapper(*args, **kwargs):
        return await base(*args, **kwargs)

    assert TOOL_META not in bad_wrapper.__dict__, (
        "Without functools.wraps, TOOL_META must NOT appear on the wrapper"
    )

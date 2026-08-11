"""Metered wrappers for LLM clients.

Token metering has one awkward property: with streaming, the token count does
not exist until the stream is finished. Instrumentation that reads
`response.usage` at call time therefore records zero for every streamed
response, which is exactly the traffic most likely to be expensive.

These wrappers hook the point where the stream closes. They duck-type against
the client object rather than importing the SDKs, so `cupo` has no hard
dependency on `anthropic` or `openai` and works against either.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cupo.integrations")


class _MeteredStream:
    """Wraps a streaming context manager and meters when it closes.

    Metering on close rather than on open is the entire point: usage totals are
    only final once the stream ends. The counter is also updated when the
    stream is abandoned early, since those tokens were generated and billed by
    the provider regardless.
    """

    def __init__(self, inner: Any, on_close, idempotency_key: str | None):
        self._inner = inner
        self._on_close = on_close
        self._idempotency_key = idempotency_key
        self._entered: Any = None

    def __enter__(self):
        raise TypeError(
            "cupo meters async clients only. Use anthropic.AsyncAnthropic or "
            "openai.AsyncOpenAI and 'async with client.messages.stream(...)'. "
            "A synchronous client cannot await the usage write when the stream "
            "closes, and silently dropping that write would undercount tokens."
        )

    def __exit__(self, *exc):  # pragma: no cover - unreachable, __enter__ raises
        return False

    async def __aenter__(self):
        self._entered = await self._inner.__aenter__()
        return self._entered

    async def __aexit__(self, *exc):
        try:
            usage = _extract_usage(self._entered)
        except Exception:
            log.exception("cupo: could not read usage from stream")
            usage = None
        result = await self._inner.__aexit__(*exc)
        if usage:
            await self._on_close(usage, self._idempotency_key)
        return result


def _extract_usage(obj: Any) -> dict[str, int] | None:
    """Pull input/output token counts off whatever the SDK returned.

    Anthropic exposes `usage.input_tokens` / `usage.output_tokens`; OpenAI uses
    `usage.prompt_tokens` / `usage.completion_tokens`. Streams expose a
    `get_final_message()` accessor instead of the attribute directly.
    """
    usage = getattr(obj, "usage", None)

    if usage is None and hasattr(obj, "get_final_message"):
        usage = getattr(obj.get_final_message(), "usage", None)
    if usage is None and hasattr(obj, "get_final_response"):
        usage = getattr(obj.get_final_response(), "usage", None)
    if usage is None:
        return None

    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    if inp is None:
        inp = getattr(usage, "prompt_tokens", 0)
    if out is None:
        out = getattr(usage, "completion_tokens", 0)

    return {"input": int(inp or 0), "output": int(out or 0), "total": int(inp or 0) + int(out or 0)}


class MeteredMessages:
    """Wraps `client.messages`, metering both blocking and streaming calls."""

    def __init__(self, inner: Any, meter, count: str):
        self._inner = inner
        self._meter = meter
        self._count = count

    async def create(self, *args, cupo_idempotency_key: str | None = None, **kwargs):
        response = self._inner.create(*args, **kwargs)
        if hasattr(response, "__await__"):
            response = await response
        usage = _extract_usage(response)
        if usage:
            await self._meter(usage[self._count], cupo_idempotency_key)
        return response

    def stream(self, *args, cupo_idempotency_key: str | None = None, **kwargs):
        async def on_close(usage, key):
            await self._meter(usage[self._count], key)

        return _MeteredStream(
            self._inner.stream(*args, **kwargs), on_close, cupo_idempotency_key
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class MeteredClient:
    """A thin proxy over an LLM client that reports token usage to Cupo.

        client = metered(anthropic.AsyncAnthropic(), cupo,
                         customer_id=customer.id, feature="ai_tokens")

        msg = await client.messages.create(model="...", messages=[...])

    Everything not explicitly wrapped falls through to the underlying client,
    so this is safe to drop in place of the original object.
    """

    def __init__(self, inner: Any, cupo, customer_id: str, feature: str, count: str):
        self._inner = inner
        self._cupo = cupo
        self._customer_id = customer_id
        self._feature = feature
        self._count = count

    async def _meter(self, units: int, idempotency_key: str | None):
        if units:
            await self._cupo.track(
                self._customer_id,
                self._feature,
                units,
                idempotency_key=idempotency_key,
            )

    @property
    def messages(self):
        return MeteredMessages(self._inner.messages, self._meter, self._count)

    @property
    def chat(self):
        # OpenAI shape: client.chat.completions.create(...)
        return _OpenAIChat(self._inner.chat, self._meter, self._count)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _OpenAIChat:
    def __init__(self, inner, meter, count):
        self._inner = inner
        self._meter = meter
        self._count = count

    @property
    def completions(self):
        return MeteredMessages(self._inner.completions, self._meter, self._count)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def metered(
    client: Any,
    cupo,
    *,
    customer_id: str,
    feature: str = "ai_tokens",
    count: str = "total",
) -> MeteredClient:
    """Wrap an Anthropic or OpenAI client so token usage lands in Cupo.

    `count` selects which figure is metered: "total", "input" or "output".
    """
    if count not in ("total", "input", "output"):
        raise ValueError("count must be 'total', 'input' or 'output'")
    return MeteredClient(client, cupo, customer_id, feature, count)

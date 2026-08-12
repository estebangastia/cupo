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


def _usage_object(obj: Any) -> Any:
    """Locate the usage object on a response, whatever shape the provider used.

    Anthropic puts it on `.usage`; streams expose `get_final_message()`.
    OpenAI-compatible providers put it on `.usage` too, but only when the
    caller passed `stream_options={"include_usage": True}`.

    Groq is the interesting case: it is otherwise OpenAI-compatible, but on a
    stream it reports usage under a vendor field, `x_groq.usage`. Code that
    only reads `.usage` silently records zero for every Groq stream. Since
    undercounting is the failure this module exists to prevent, the vendor
    field is checked explicitly rather than treated as an edge case.
    """
    usage = getattr(obj, "usage", None)
    if usage is not None:
        return usage

    for accessor in ("get_final_message", "get_final_response"):
        method = getattr(obj, accessor, None)
        if callable(method):
            usage = getattr(method(), "usage", None)
            if usage is not None:
                return usage

    # Groq: chunk.x_groq.usage. The SDK may expose it as an attribute or, when
    # the field is unmodelled, inside model_extra / a plain dict.
    x_groq = getattr(obj, "x_groq", None)
    if x_groq is None:
        extra = getattr(obj, "model_extra", None) or {}
        x_groq = extra.get("x_groq") if isinstance(extra, dict) else None
    if isinstance(x_groq, dict):
        return x_groq.get("usage")
    if x_groq is not None:
        return getattr(x_groq, "usage", None)

    return None


def _extract_usage(obj: Any) -> dict[str, int] | None:
    """Pull input/output token counts off whatever the SDK returned.

    Anthropic exposes `usage.input_tokens` / `usage.output_tokens`; OpenAI and
    its compatible providers use `prompt_tokens` / `completion_tokens`. Both
    attribute access and dict access are supported, because vendor fields like
    Groq's arrive unmodelled.
    """
    usage = _usage_object(obj)
    if usage is None:
        return None

    def field(*names):
        for name in names:
            if isinstance(usage, dict):
                if usage.get(name) is not None:
                    return usage[name]
            elif getattr(usage, name, None) is not None:
                return getattr(usage, name)
        return None

    inp = field("input_tokens", "prompt_tokens") or 0
    out = field("output_tokens", "completion_tokens") or 0

    return {
        "input": int(inp),
        "output": int(out),
        "total": int(inp) + int(out),
    }


class _MeteredIterator:
    """Wraps an async iterator of stream chunks and meters after the last one.

    OpenAI-compatible providers return an async iterable rather than a context
    manager, and the usage totals ride on the final chunk. Every chunk is
    inspected rather than only the last, because providers disagree about which
    chunk carries the totals -- some append an extra empty chunk after the one
    with `finish_reason`, so "the last chunk" is not a reliable location.
    """

    def __init__(self, inner: Any, on_close, idempotency_key: str | None):
        self._inner = inner
        self._on_close = on_close
        self._idempotency_key = idempotency_key
        self._usage: dict[str, int] | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._inner.__anext__()
        except StopAsyncIteration:
            if self._usage:
                await self._on_close(self._usage, self._idempotency_key)
                self._usage = None
            raise

        try:
            usage = _extract_usage(chunk)
            if usage and usage["total"]:
                self._usage = usage
        except Exception:
            log.exception("cupo: could not read usage from stream chunk")

        return chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MeteredMessages:
    """Wraps `client.messages`, metering both blocking and streaming calls."""

    def __init__(self, inner: Any, meter, count: str):
        self._inner = inner
        self._meter = meter
        self._count = count

    async def create(self, *args, cupo_idempotency_key: str | None = None, **kwargs):
        streaming = kwargs.get("stream", False)

        # Without this the provider omits usage from the stream entirely and
        # there is nothing to meter. Callers who set it themselves are left
        # alone.
        if streaming and "stream_options" not in kwargs:
            kwargs["stream_options"] = {"include_usage": True}

        response = self._inner.create(*args, **kwargs)
        if hasattr(response, "__await__"):
            response = await response

        if streaming or hasattr(response, "__anext__"):
            async def on_close(usage, key):
                await self._meter(usage[self._count], key)

            return _MeteredIterator(
                response.__aiter__() if hasattr(response, "__aiter__") else response,
                on_close,
                cupo_idempotency_key,
            )

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

    def __init__(
        self,
        inner: Any,
        cupo,
        customer_id: str,
        feature: str,
        count: str,
        plan: str | None = None,
    ):
        self._inner = inner
        self._cupo = cupo
        self._customer_id = customer_id
        self._feature = feature
        self._count = count
        self._plan = plan

    async def _meter(self, units: int, idempotency_key: str | None):
        if units:
            await self._cupo.track(
                self._customer_id,
                self._feature,
                units,
                plan=self._plan,
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
    plan: str | None = None,
) -> MeteredClient:
    """Wrap an Anthropic or OpenAI client so token usage lands in Cupo.

    `count` selects which figure is metered: "total", "input" or "output".

    `plan` matters more than it looks. Counters are keyed by the window the
    feature is defined with, so metering a feature declared `window: day`
    without naming the plan writes into the monthly row instead -- the write
    succeeds, no error is raised, and the daily counter silently reads zero.
    Pass the customer's plan whenever the feature is not on a monthly window.
    """
    if count not in ("total", "input", "output"):
        raise ValueError("count must be 'total', 'input' or 'output'")
    return MeteredClient(client, cupo, customer_id, feature, count, plan)

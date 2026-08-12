"""Streaming responses must be metered too.

These use fake clients shaped like the Anthropic and OpenAI SDKs rather than
the real ones, so the suite runs without network access or API keys. The shapes
are what matter: usage lives on the response for blocking calls, and behind
get_final_message() for streams.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cupo.integrations import metered
from cupo.windows import window_start

pytestmark = pytest.mark.asyncio


class FakeUsage:
    def __init__(self, inp, out):
        self.input_tokens = inp
        self.output_tokens = out


class FakeOpenAIUsage:
    def __init__(self, inp, out):
        self.prompt_tokens = inp
        self.completion_tokens = out


class FakeMessage:
    def __init__(self, usage):
        self.usage = usage
        self.content = "hello"


class FakeStream:
    """Mimics anthropic's async streaming context manager."""

    def __init__(self, usage, chunks=("he", "llo")):
        self._usage = usage
        self._chunks = chunks
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    @property
    def text_stream(self):
        yield from self._chunks

    def get_final_message(self):
        return FakeMessage(self._usage)


class FakeMessages:
    def __init__(self, usage):
        self._usage = usage

    def create(self, **kwargs):
        return FakeMessage(self._usage)

    def stream(self, **kwargs):
        return FakeStream(self._usage)


class FakeAnthropic:
    def __init__(self, inp=100, out=250):
        self.messages = FakeMessages(FakeUsage(inp, out))
        self.base_url = "https://example.invalid"


class FakeCompletions:
    def __init__(self, usage):
        self._usage = usage

    def create(self, **kwargs):
        return FakeMessage(self._usage)


class FakeOpenAI:
    def __init__(self, inp=40, out=60):
        self.chat = type(
            "Chat", (), {"completions": FakeCompletions(FakeOpenAIUsage(inp, out))}
        )()


def _month():
    return window_start("month", datetime.now(timezone.utc))


async def test_blocking_call_is_metered(cupo, customer):
    client = metered(FakeAnthropic(100, 250), cupo, customer_id=customer)
    await client.messages.create(model="claude-sonnet-4-6", messages=[])

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 350


async def test_streaming_call_is_metered_on_close(cupo, customer):
    """The case naive instrumentation misses entirely."""
    client = metered(FakeAnthropic(80, 400), cupo, customer_id=customer)

    stream_cm = client.messages.stream(model="claude-sonnet-4-6", messages=[])
    async with stream_cm as stream:
        collected = "".join(stream.text_stream)
        # Mid-stream, nothing has been metered yet: the total is not known.
        assert await cupo.store.used(customer, "ai_tokens", _month()) == 0

    assert collected == "hello"
    assert await cupo.store.used(customer, "ai_tokens", _month()) == 480


async def test_count_selects_which_tokens_are_metered(cupo, customer):
    client = metered(
        FakeAnthropic(1_000, 25), cupo, customer_id=customer, count="output"
    )
    await client.messages.create(model="claude-sonnet-4-6", messages=[])

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 25


async def test_openai_shape_is_supported(cupo, customer):
    client = metered(FakeOpenAI(40, 60), cupo, customer_id=customer)
    await client.chat.completions.create(model="gpt-4o", messages=[])

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 100


async def test_idempotency_key_flows_through(cupo, customer):
    client = metered(FakeAnthropic(10, 10), cupo, customer_id=customer)

    await client.messages.create(model="m", messages=[], cupo_idempotency_key="k1")
    await client.messages.create(model="m", messages=[], cupo_idempotency_key="k1")

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 20


async def test_unwrapped_attributes_fall_through(cupo, customer):
    inner = FakeAnthropic()
    client = metered(inner, cupo, customer_id=customer)
    assert client.base_url == "https://example.invalid"


async def test_sync_stream_use_fails_loudly(cupo, customer):
    """Silently undercounting is worse than refusing to run."""
    client = metered(FakeAnthropic(), cupo, customer_id=customer)
    with pytest.raises(TypeError, match="async clients only"):
        with client.messages.stream(model="m", messages=[]):
            pass


# --------------------------------------------------------------- OpenAI-style
# Providers that follow the OpenAI wire format return an async iterator of
# chunks rather than a context manager, and put the totals on a final chunk.


class FakeChunk:
    """A streaming chunk.

    Note the empty `choices` when there is no content: that is what real
    providers send on the final usage-bearing chunk, and an earlier version of
    this fake got it wrong by always supplying one choice. The mistake only
    surfaced against the live API, where `chunk.choices[0]` raised IndexError
    at the end of an otherwise successful stream.
    """

    def __init__(self, content=None, usage=None, x_groq=None):
        if content is None:
            self.choices = []
        else:
            self.choices = [
                type("C", (), {"delta": type("D", (), {"content": content})()})()
            ]
        self.usage = usage
        if x_groq is not None:
            self.x_groq = x_groq


class FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


class FakeStreamingCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return FakeAsyncStream(self._chunks)


class FakeOpenAICompatible:
    def __init__(self, chunks):
        self.completions = FakeStreamingCompletions(chunks)
        self.chat = type("Chat", (), {"completions": self.completions})()


async def test_openai_style_stream_is_metered(cupo, customer):
    """Usage arrives on a trailing chunk, after the content is done."""
    chunks = [
        FakeChunk("he"),
        FakeChunk("llo"),
        FakeChunk(None, usage=FakeOpenAIUsage(30, 70)),
    ]
    client = metered(FakeOpenAICompatible(chunks), cupo, customer_id=customer)

    text = ""
    async for chunk in await client.chat.completions.create(model="m", messages=[], stream=True):
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            text += piece

    assert text == "hello"
    assert await cupo.store.used(customer, "ai_tokens", _month()) == 100


async def test_groq_x_groq_usage_shape_is_metered(cupo, customer):
    """Groq reports stream usage under a vendor field, not `.usage`.

    Code that only reads `.usage` records zero for every Groq stream. This is
    the silent-undercount failure the module exists to prevent, so it gets a
    test of its own.
    """
    chunks = [
        FakeChunk("hi"),
        FakeChunk(None, x_groq={"usage": {"prompt_tokens": 21, "completion_tokens": 42}}),
    ]
    client = metered(FakeOpenAICompatible(chunks), cupo, customer_id=customer)

    async for _ in await client.chat.completions.create(model="m", messages=[], stream=True):
        pass

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 63


async def test_usage_on_a_non_final_chunk_is_still_caught(cupo, customer):
    """Some providers append an empty chunk after the one carrying usage."""
    chunks = [
        FakeChunk("x"),
        FakeChunk(None, usage=FakeOpenAIUsage(5, 15)),
        FakeChunk(None),  # trailing empty chunk
    ]
    client = metered(FakeOpenAICompatible(chunks), cupo, customer_id=customer)

    async for _ in await client.chat.completions.create(model="m", messages=[], stream=True):
        pass

    assert await cupo.store.used(customer, "ai_tokens", _month()) == 20


async def test_include_usage_is_requested_automatically(cupo, customer):
    """Without stream_options the provider omits usage and there is nothing to meter."""
    inner = FakeOpenAICompatible([FakeChunk(None, usage=FakeOpenAIUsage(1, 1))])
    client = metered(inner, cupo, customer_id=customer)

    async for _ in await client.chat.completions.create(model="m", messages=[], stream=True):
        pass

    assert inner.completions.last_kwargs["stream_options"] == {"include_usage": True}


async def test_plan_is_needed_for_non_monthly_windows(cupo, customer):
    """Metering without the plan writes into the wrong window.

    Counters are keyed by window start. A feature declared `window: day`
    metered without naming the plan lands in the monthly row instead: the write
    succeeds, nothing raises, and the daily counter reads zero. That silent
    disagreement between what was written and what is read is worse than an
    error, so both directions are pinned here.
    """
    from datetime import datetime, timezone
    from cupo.windows import window_start

    day = window_start("day", datetime.now(timezone.utc))

    # Without plan: lands in the monthly row, invisible to the daily limit.
    unaware = metered(FakeAnthropic(10, 10), cupo, customer_id=customer)
    await unaware.messages.create(model="m", messages=[])
    assert await cupo.store.used(customer, "ai_tokens", day) == 0
    assert await cupo.store.used(customer, "ai_tokens", _month()) == 20

    # With plan: lands where usage() and check() will look for it.
    aware = metered(
        FakeAnthropic(10, 10), cupo, customer_id=customer, plan="daily_tokens"
    )
    await aware.messages.create(model="m", messages=[])
    assert await cupo.store.used(customer, "ai_tokens", day) == 20

    report = await cupo.usage(customer, "daily_tokens")
    assert report["ai_tokens"].used == 20


async def test_final_chunk_has_no_choices(cupo, customer):
    """Consumers must be able to iterate without guarding every chunk.

    The usage-bearing chunk has an empty `choices` list on real providers.
    Metering must still work, and iteration must not raise.
    """
    chunks = [
        FakeChunk("token"),
        FakeChunk(None, x_groq={"usage": {"prompt_tokens": 9, "completion_tokens": 11}}),
    ]
    client = metered(FakeOpenAICompatible(chunks), cupo, customer_id=customer)

    seen = []
    async for chunk in await client.chat.completions.create(
        model="m", messages=[], stream=True
    ):
        if chunk.choices:
            seen.append(chunk.choices[0].delta.content)

    assert seen == ["token"]
    assert await cupo.store.used(customer, "ai_tokens", _month()) == 20

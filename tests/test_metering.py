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

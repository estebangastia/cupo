"""Streaming token metering against a real provider, on a free tier.

Groq is used here because its free tier needs no credit card and its API
follows the OpenAI wire format, which makes this example representative of
every OpenAI-compatible provider. Swap the base_url and model and it runs
against OpenAI, Cerebras, Together, or a local vLLM.

    pip install cupo openai
    docker compose up -d                      # or point CUPO_DSN at any Postgres
    setx GROQ_API_KEY "gsk_..."               # Windows; use export on Linux/macOS
    python examples/groq_streaming.py

What it demonstrates:

  1. check() gates the request before a single token is generated
  2. the response streams to the terminal as it arrives
  3. real token counts are metered when the stream ends, not estimated
  4. the fourth run is refused, because the free plan allows three

Point 3 is the one worth watching. Groq reports stream usage under a vendor
field (`x_groq.usage`) rather than the standard `.usage`, so instrumentation
written against the OpenAI shape alone records zero for every streamed
response. Cupo checks both.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from cupo import Cupo
from cupo.integrations import metered

try:
    from openai import AsyncOpenAI
except ImportError:
    sys.exit("This example needs the OpenAI SDK: pip install openai")

DSN = os.environ.get("CUPO_DSN", "postgresql://cupo:cupo@localhost:5433/cupo_test")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

PLANS = {
    "free": {
        "ai_chat": {"limit": 3, "window": "day"},
        "ai_tokens": {"limit": 5_000, "window": "day"},
    },
    "pro": {
        "ai_chat": {"limit": 1_000, "window": "day"},
        "ai_tokens": {"limit": 2_000_000, "window": "day"},
    },
}

CUSTOMER = "demo_customer"
PLAN = "free"


async def ask(cupo: Cupo, client, prompt: str) -> None:
    # 1. Gate the request. This is atomic: concurrent callers cannot both
    #    consume the same last credit.
    gate = await cupo.check(CUSTOMER, "ai_chat", plan=PLAN)
    if not gate.allowed:
        print(f"  refused: {gate.reason} ({gate.used}/{gate.limit} used)")
        print(f"  resets at {gate.resets_at:%Y-%m-%d %H:%M} UTC")
        return

    print(f"  [{gate.used}/{gate.limit} requests] ", end="", flush=True)

    # 2. Stream. Token usage is unknown until the last chunk arrives.
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120,
        stream=True,
        # Passing a key makes a retried request safe to re-send: the usage is
        # recorded once regardless of how many times it lands.
        cupo_idempotency_key=str(uuid.uuid4()),
    )

    async for chunk in stream:
        # The final chunk carries the usage totals and no content at all --
        # on Groq its `choices` list is empty, so indexing it blindly raises
        # IndexError right at the end of an otherwise successful stream.
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            print(piece, end="", flush=True)
    print()


async def main() -> None:
    if not GROQ_KEY:
        sys.exit("Set GROQ_API_KEY first. Free key, no card: https://console.groq.com")

    cupo = Cupo(plans=PLANS, dsn=DSN)
    await cupo.connect()
    await cupo.reset(CUSTOMER)  # so the demo is repeatable

    raw = AsyncOpenAI(api_key=GROQ_KEY, base_url="https://api.groq.com/openai/v1")
    # Naming the plan matters: ai_tokens is on a daily window here, and
    # metering without it would write into the monthly counter instead.
    client = metered(
        raw, cupo, customer_id=CUSTOMER, feature="ai_tokens", plan=PLAN
    )

    prompts = [
        "In one sentence, what is a race condition?",
        "In one sentence, why is idempotency useful in billing?",
        "In one sentence, what is a token in an LLM?",
        "This fourth request should never reach the model.",
    ]

    for i, prompt in enumerate(prompts, 1):
        print(f"\n--- request {i} ---")
        await ask(cupo, client, prompt)

    print("\n--- usage this window ---")
    for feature, res in (await cupo.usage(CUSTOMER, PLAN)).items():
        print(f"  {feature:<12} {res.used:>7} / {res.limit}")

    print(
        "\nThe ai_tokens figure came from the provider's own totals on the final"
        "\nstream chunk, not from an estimate. Under the free plan the fourth"
        "\nrequest was refused before any tokens were generated."
    )

    await cupo.close()


if __name__ == "__main__":
    asyncio.run(main())

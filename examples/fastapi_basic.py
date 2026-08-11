"""A runnable FastAPI app with plan limits enforced.

    docker compose up -d
    pip install -e ".[dev]" fastapi uvicorn
    uvicorn examples.fastapi_basic:app --reload

Then:

    curl -X POST localhost:8000/chat -H "X-Customer: alice" -H "X-Plan: free"
    curl localhost:8000/usage        -H "X-Customer: alice" -H "X-Plan: free"

Hit /chat four times on the free plan and the fifth returns 429 with the reset
time and an upgrade link.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from cupo import Cupo

DSN = os.environ.get(
    "CUPO_DSN", "postgresql://cupo:cupo@localhost:5433/cupo_test"
)

PLANS = {
    "free": {
        "ai_chat": {"limit": 3, "window": "day"},
        "ai_tokens": {"limit": 10_000, "window": "month"},
        "pdf_export": False,
    },
    "pro": {
        "ai_chat": {"limit": 500, "window": "day"},
        "ai_tokens": {"limit": 2_000_000, "window": "month", "on_limit": "degrade"},
        "pdf_export": True,
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cupo = Cupo(
        plans=PLANS, dsn=DSN, upgrade_url="https://example.com/pricing"
    )
    await app.state.cupo.connect()
    yield
    await app.state.cupo.close()


app = FastAPI(lifespan=lifespan)


@app.post("/chat")
async def chat(
    request: Request,
    x_customer: str = Header(...),
    x_plan: str = Header("free"),
):
    cupo: Cupo = request.app.state.cupo

    res = await cupo.check(x_customer, "ai_chat", plan=x_plan)
    if not res.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "error": res.reason,
                "used": res.used,
                "limit": res.limit,
                "resets_at": res.resets_at.isoformat() if res.resets_at else None,
                "upgrade_url": res.upgrade_url,
            },
        )

    # ... your LLM call goes here. Token usage is metered afterwards, because
    # the real figure is only known once the response is complete.
    reply, output_tokens = "hello from the model", 128

    await cupo.track(
        x_customer,
        "ai_tokens",
        output_tokens,
        plan=x_plan,
        idempotency_key=request.headers.get("X-Request-Id"),
    )

    return {
        "reply": reply,
        "remaining": res.remaining,
        "degraded": res.degraded,
    }


@app.get("/usage")
async def usage(
    request: Request,
    x_customer: str = Header(...),
    x_plan: str = Header("free"),
):
    cupo: Cupo = request.app.state.cupo
    report = await cupo.usage(x_customer, x_plan)
    return {
        feature: {
            "used": r.used,
            "limit": r.limit,
            "remaining": r.remaining,
            "resets_at": r.resets_at.isoformat() if r.resets_at else None,
        }
        for feature, r in report.items()
    }

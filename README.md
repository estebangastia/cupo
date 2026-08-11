# Cupo

**Usage limits, feature gates, and metering for AI products. Drop-in for FastAPI and Next.js.**

> **Status: design RFC — no code yet.**
> This README specifies the API I'm about to build. Nothing described below is implemented; `src/cupo` is a placeholder and v0.1 is in progress (see [Roadmap](#roadmap)).
> I'm publishing the design before the code so the interface gets torn apart while it's still cheap to change. If this would — or wouldn't — solve a problem you have, open an issue. Blunt feedback is the point.

---

## The problem

Billing platforms (Stripe, Lago, Metronome, Orb) answer *"how much do I charge?"* — they count usage and generate invoices **after the fact**.

None of them answer the question your code asks a hundred times per second: *"is this customer allowed to do this, right now?"*

So every AI SaaS ends up hand-rolling the same thing:

- A `usage` table with counters that break under concurrent requests
- Plan limits scattered across `if customer.plan == "pro"` checks
- Token counting glued onto LLM calls (and silently wrong for streaming)
- A cron job that resets counters monthly (usually in the wrong timezone)
- No warning to the customer before they hit the wall

Cupo is meant to be that layer, built once, in the open.

## What it will look like

*The snippets below are the target API, not working code.*

```python
from cupo import Cupo

cupo = Cupo()  # embedded mode: uses your existing Postgres, no server needed

@app.post("/chat")
@cupo.protect(feature="ai_chat")            # blocks with 429 + upgrade info if over limit
async def chat(req: ChatRequest, customer: Customer):
    ...
```

Or with explicit control:

```python
res = cupo.check(customer.id, feature="ai_chat", units=1)   # atomic check-and-consume
if not res.allowed:
    return JSONResponse(429, {"error": "plan_limit", "resets_at": res.resets_at,
                              "upgrade_url": res.upgrade_url})

reply = anthropic.messages.create(...)

cupo.track(customer.id, feature="ai_chat",
           tokens=reply.usage.output_tokens,
           idempotency_key=req.id)          # safe to retry, never double-counts
```

## Plans as code

Plans live in a versioned YAML file in your repo — reviewable in a PR, not hidden in a dashboard:

```yaml
# cupo.yaml
features:
  ai_chat:    { unit: message }
  ai_tokens:  { unit: token }
  pdf_export: { unit: export }

plans:
  free:
    ai_chat:    { limit: 50,      window: month }
    ai_tokens:  { limit: 100_000, window: month }
    pdf_export: false

  pro:
    ai_chat:    { limit: 5_000,      window: month, on_limit: degrade }  # block | degrade | bill
    ai_tokens:  { limit: 10_000_000, window: month }
    pdf_export: true

  enterprise:
    ai_chat:    unlimited
    ai_tokens:  { limit: 100_000_000, window: month, on_limit: bill, overage_price: 0.50/1_000_000 }
    pdf_export: true
```

`on_limit` policies:

- **block** — deny the request (default)
- **degrade** — allow it, but flag `res.degraded = True` so you can route to a cheaper model
- **bill** — allow it and emit an overage event your billing system can invoice

## Token-aware AI helpers

The part everyone gets wrong. Cupo will ship thin wrappers around the Anthropic and OpenAI clients that meter tokens automatically — **including streaming**, where usage is only known when the stream ends:

```python
from cupo.anthropic import metered

client = metered(anthropic.Anthropic(), cupo, feature="ai_tokens")

# streaming: tokens are tracked when the stream closes, with the request's idempotency key
with client.messages.stream(model="claude-sonnet-4-6", ...) as stream:
    for text in stream.text_stream:
        yield text
```

## How it works

```
your app ──▶ Cupo SDK ──▶ counters (your Postgres, or Redis, or Cupo server)
                │
                ├─ local entitlement cache (checks add ~0 network latency)
                └─ async usage flush (batched, idempotent)
```

Three problems Cupo is designed to solve so you don't have to:

1. **Atomicity.** Two concurrent requests with one credit left: exactly one passes. Counters mutate through a single atomic operation (`INSERT ... ON CONFLICT DO UPDATE` on Postgres, `INCR` on Redis) — never read-modify-write.
2. **Latency.** Entitlements are cached in-process and synced in the background, so a `check()` is a dictionary lookup rather than a network call. Trade-off: near the limit, a small overshoot is possible — configurable, with `strict: true` forcing a synchronous check for expensive features.
3. **Idempotency.** Every `track()` takes an idempotency key. Retries, at-least-once queues, and network flakiness never double-count.

**Failure mode is yours to choose:** `fail_open` (if Cupo is unreachable, allow the request — default, your product stays up) or `fail_closed` (deny — for features where overshoot costs you real money).

## Deployment modes

| Mode           | What it needs                           | For                                        | Lands in |
| -------------- | --------------------------------------- | ------------------------------------------ | -------- |
| **Embedded**   | Your existing Postgres (Supabase works) | Solo devs, single service                  | v0.1     |
| **Server**     | Docker container + Redis                | Multiple services / languages              | v0.2     |
| **Cloud**      | Nothing — hosted                        | Teams that want dashboards, analytics, SLA | v0.3     |

## Webhooks

The server (v0.2) will emit events so you can warn customers *before* they hit the wall:

- `usage.threshold` — configurable (e.g. at 80% of any limit)
- `usage.limit_reached`
- `usage.overage` — with units and computed price, ready to forward to your billing

## What Cupo is not

- **Not a billing platform.** It doesn't generate invoices or charge cards. It pairs with Stripe, Mercado Pago, Lago, or whatever you already use (plan sync integrations are on the roadmap).
- **Not an API gateway.** It runs inside your app, not in front of it. If you want per-team spend caps on outbound LLM traffic, you want LiteLLM or Portkey — different layer, and they compose fine with this one.
- **Not for enterprise contract management.** If you have negotiated multi-year commits with drawdowns, you want Metronome or Orb.

## FAQ

**vs. Lago / Metronome / Orb?** Those meter usage to *bill* it. Cupo meters usage to *enforce* it, in the request path, in real time. Different layer — Cupo can feed them.

**vs. LiteLLM / Portkey / Helicone?** Those are gateways: they sit in front of your model providers and cap what *your* infrastructure spends, per key or per team. Cupo sits inside your app and answers a different question — what *your customer* is entitled to under the plan they pay for. Most products end up wanting both.

**vs. Stigg?** Closest neighbour. Stigg is a hosted, dashboard-first entitlements platform aimed at teams. Cupo is open source, config-as-code, and designed so a solo developer is enforcing limits 15 minutes after `pip install cupo`.

**vs. rolling my own?** You can, and plenty do. The hand-rolled versions I've run into share three bugs: a read-modify-write counter that breaks under concurrency, streaming responses that never get metered, and no idempotency on retries. Getting those three right is the entire reason this project exists.

**Why should I trust the counters?** Right now you shouldn't — there's nothing to trust yet. When v0.1 lands it ships with a concurrency test suite that hammers every counter path, and the intent is that you judge the project on that suite rather than on this paragraph. If it isn't convincing, say so in an issue.

## Roadmap

- [ ] **v0.1** *(in progress)* — Python SDK, embedded mode (Postgres), atomic counters, idempotent `track()`, Anthropic/OpenAI metered wrappers incl. streaming, concurrency test suite, one runnable FastAPI example
- [ ] **v0.2** — Plans-as-code YAML engine, standalone server (Docker), TypeScript SDK, webhooks, Redis counters
- [ ] **v0.3** — Stripe & Mercado Pago plan sync, usage dashboard, hosted cloud (free tier + flat self-serve pricing — no "talk to sales")

## License

SDKs: MIT. Server: AGPL-3.0. Self-hosting is free forever; the hosted cloud is how the project sustains itself.

---

**Would you use this?** Open an issue titled `feedback:` and tell me — especially if the answer is no and why. If you've hand-rolled this layer before, I'd love 20 minutes of your war stories.

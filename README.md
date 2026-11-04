# Pierside

**An AI agent that scopes an underwater ROV inspection, prices it from a fixed rate card, takes a 30% deposit through PayPal, and books the dive slot. All in one conversation.**

Booking a dock, piling, seawall or hull inspection today takes days of phone tag. The contractor needs a dozen details (asset, size, depth, visibility, marine growth, distance), sends a quote by email, then chases a deposit before committing a crew and an ROV to a date. Pierside does that in about two minutes. A customer describes the site (or sends a photo), Claude asks the follow-up questions, a deterministic rate card produces the quote, and PayPal checkout takes the deposit. The slot is booked only after the server has verified the payment.

![Booked job (mock mode)](docs/screens/4_booked.png)

## Try it

- **Hosted demo:** https://pierside.onrender.com (Render free tier: the first load after idle takes about a minute)
- **PayPal sandbox buyer:** `sb-omnq353715140@personal.example.com` / `xN8%EHX2` (fake money, PayPal sandbox only)

Try: "12 wooden pilings at our marina in Quincy, about 25 ft deep", answer the agent's questions, pick a slot, then pay the deposit with the sandbox buyer.

## How PayPal and AI are used

| Step | Who does it | How |
|---|---|---|
| Scope the job | Claude (`claude-haiku-5-5`) | Multi-turn chat. Asks for missing facts and states its assumptions. With a site photo, Claude vision estimates marine growth and water clarity |
| Price it | Server code ([`pricing.py`](pricing.py)) | The agent calls the strict-schema tool `price_quote`. **The model never produces a number**: it can only repeat the rate card's output |
| Pick a slot | Claude + server | Tools `list_slots` / `hold_slot` (15-min hold). The server refuses rush slots unless the quote includes the rush surcharge |
| Take the deposit | PayPal **Orders v2** + **JS SDK** buttons | The server creates the order for exactly 30% of the quote (idempotent `PayPal-Request-Id`). The buyer approves in the PayPal popup |
| Book it | Server code | Captures the order, then checks that the status is `COMPLETED` and the amount equals the deposit. Only then does it book the slot |
| Balance | PayPal **Invoicing v2** | Drafts an invoice for the remaining 70%, with the deposit shown as paid |
| Follow-up | Claude | Gets a server-generated PAYMENT EVENT and writes the confirmation plus a pre-dive plan for that site |

The split is the point: **the LLM handles the conversation, and money moves only through deterministic, verified code paths.** A prompt-injected or confused agent cannot discount a job, invent a price, or book an unpaid slot.

## Architecture

```
browser (static/index.html)
  ├─ POST /api/chat    → Claude tool loop (app.py) → price_quote / list_slots / hold_slot
  ├─ PayPal JS SDK     → createOrder → POST /api/order   → PayPal Orders v2: create
  │                      onApprove   → POST /api/capture → PayPal Orders v2: capture + verify
  │                                                       → book slot → PayPal Invoicing v2: draft balance
  │                                                       → Claude: confirmation + pre-dive plan
  └─ job sheet: live quote lines, slot, deposit button, booking card
```

About 550 lines of Python and one HTML page. PayPal is called over plain REST with the standard library ([`paypal.py`](paypal.py)). Claude is called with the official `anthropic` SDK.

**Demo guardrails:** Haiku model, low effort, a per-IP hourly limit, a global daily cap on model calls, 25 messages per chat, a capped tool loop, client-side photo downscaling to 1024 px, and a spend limit on the API key.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # MOCK=1 runs with no keys at all
set -a; . ./.env; set +a
.venv/bin/uvicorn app:app --reload   # http://127.0.0.1:8000
```

**One switch:** `MOCK=1` replaces the Claude client with a scripted fake (`fake_claude.py`) and the PayPal HTTP layer with an in-memory fake of the same Orders v2 endpoints. Everything else (agent loop, pricing, verification, booking) runs the same code as production. For the real thing, set `MOCK=0` and fill in a PayPal sandbox app's client ID and secret plus an Anthropic API key.

## Tests

```bash
.venv/bin/python pricing.py   # rate card self-check
.venv/bin/python test_e2e.py  # boots the server with MOCK=1: chat → quote → hold → order → capture → booked,
                              # plus double-booking and forged-order checks
```

## Deploy

`render.yaml` is a Render Blueprint (free web service). Set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and `ANTHROPIC_API_KEY` in the dashboard.

## Notes

- The company, rate card and prices are illustrative. Payments run in the PayPal sandbox only.
- State is in memory (bookings reset when the free instance sleeps). Fine for a demo; a real deployment would add a database and PayPal webhooks.

## License

MIT

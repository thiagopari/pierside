"""Pierside: an AI agent that scopes an underwater ROV inspection, prices it from a fixed
rate card, takes a 30% deposit through PayPal, and books the dive slot.

The model gathers scope and talks to the customer. Money and bookings are deterministic
server code: prices come only from pricing.quote, and a slot is booked only after the
server has captured and verified the PayPal payment.
"""
import datetime as dt, json, os, threading, time, uuid
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import paypal
import pricing

MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
MOCK_AI = paypal.MOCK if os.environ.get("MOCK", "auto") != "auto" else not os.environ.get("ANTHROPIC_API_KEY")
if os.environ.get("MOCK") == "0" and not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("MOCK=0 needs ANTHROPIC_API_KEY")
PER_IP_HOURLY = int(os.environ.get("PER_IP_HOURLY", "40"))
DAILY_CAP = int(os.environ.get("DAILY_MODEL_CALLS", "600"))
MAX_USER_TURNS = 25
MAX_LOOP = 6

if MOCK_AI:
    from fake_claude import FakeClaude
    client = FakeClaude()
else:
    import anthropic
    client = anthropic.Anthropic()

# ponytail: in-memory state, lost on restart/Render sleep; add SQLite if bookings must survive
SESSIONS = defaultdict(lambda: {"messages": [], "quote": None, "scope": None, "slot": None,
                                "order_id": None, "booking": None, "turns": 0, "lock": threading.Lock()})
BOOKED = {}  # slot_id -> booking id
HOLDS = {}  # slot_id -> (session_id, expiry)
CALLS_BY_IP = defaultdict(deque)
DAILY = {"day": None, "n": 0}

SYSTEM = """You are Pierside, the booking assistant for a small underwater inspection company that \
surveys docks, pier pilings, seawalls, moorings and vessel hulls with a tethered ROV. The base is in \
Boston Harbor. All jobs, prices and payments in this demo are sandbox only.

Your job: scope the inspection, get a firm quote, help the customer pick a dive slot, and hand off \
to PayPal for a 30% deposit.

Scope you need for price_quote: asset type, quantity (hull length in ft, number of piles, seawall \
length in ft, or number of moorings), max water depth, visibility, marine growth (fouling), distance \
from Boston Harbor in miles, optional add-ons (3D photogrammetry model, cathodic-protection \
readings), and whether they need a slot within 3 business days (rush).
- Ask at most two short questions per turn. If the customer doesn't know something, make a \
reasonable assumption, say it out loud, and use "unknown" for fouling.
- If the customer sends a photo, look at it: say what you see (asset, growth, water clarity, damage \
worth a closer look) and use it to set fouling and visibility.
- Never state, estimate or change a price yourself. Always call price_quote and repeat its numbers \
exactly. Re-quote whenever the scope changes.
- After the quote, call list_slots and offer two or three options. When the customer picks one, \
call hold_slot. Then tell them to pay the deposit with the PayPal button on the job sheet.
- You cannot take payment or confirm a booking. The server books the slot only after PayPal \
confirms the deposit, and then sends you a PAYMENT EVENT. When you get it, write a short \
confirmation and a pre-dive plan as "-" bullets: site access, tide/current window, dive plan for \
this asset, safety, and the deliverables.
- Plain text only: no markdown headings, bold or tables. Keep replies under 120 words except the \
pre-dive plan. Politely decline anything unrelated to booking an inspection."""

TOOLS = [
    {
        "name": "price_quote",
        "description": "Price an ROV inspection from the company's fixed rate card. The only source of prices. "
                       "Returns line items, total, 30% deposit and balance.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_type", "quantity", "max_depth_ft", "visibility", "fouling",
                         "distance_miles", "addons", "rush", "site_name"],
            "properties": {
                "asset_type": {"type": "string", "enum": ["hull", "pilings", "seawall", "mooring"]},
                "quantity": {"type": "number", "description": "hull: length ft; pilings: piles; seawall: length ft; mooring: count"},
                "max_depth_ft": {"type": "number"},
                "visibility": {"type": "string", "enum": ["good", "fair", "poor"]},
                "fouling": {"type": "string", "enum": ["light", "moderate", "heavy", "unknown"]},
                "distance_miles": {"type": "number", "description": "one-way from Boston Harbor"},
                "addons": {"type": "array", "items": {"type": "string", "enum": ["photogrammetry_model", "cp_readings"]}},
                "rush": {"type": "boolean", "description": "slot needed within 3 business days"},
                "site_name": {"type": "string", "description": "short site description, e.g. 'Quincy marina, A dock'"},
            },
        },
    },
    {
        "name": "list_slots",
        "description": "List open dive slots for the next two weeks. Slots marked rush need a quote with rush=true.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "hold_slot",
        "description": "Hold a slot for this customer for 15 minutes while they pay the deposit.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["slot_id"],
                         "properties": {"slot_id": {"type": "string"}}},
    },
]


def business_days(n):
    d, out = dt.date.today(), []
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def all_slots():
    out = []
    for i, d in enumerate(business_days(10)):
        for hhmm in ("08:00", "13:00"):
            sid = f"{d.isoformat()}T{hhmm}"
            out.append({"slot_id": sid, "label": f"{d:%a %b} {d.day}, {hhmm}", "rush": i < 3,
                        "_taken_demo": (d.toordinal() + int(hhmm[:2])) % 3 == 0})  # looks like a real calendar
    return out


def slot_free(sid, session_id):
    held = HOLDS.get(sid)
    return sid not in BOOKED and (not held or held[0] == session_id or held[1] < time.time())


def open_slots(session_id):
    return [{k: v for k, v in s.items() if k != "_taken_demo"} for s in all_slots()
            if not s["_taken_demo"] and slot_free(s["slot_id"], session_id)]


def run_tool(sid, s, name, args):
    if name == "price_quote":
        q = pricing.quote(args)
        s["scope"], s["quote"] = args, {**q, "id": "Q-" + uuid.uuid4().hex[:8].upper()}
        if s["slot"] and not args["rush"] and s["slot"]["rush"]:
            s["slot"] = None
            q["note"] = "Held slot released: it needs rush=true."
        return q
    if name == "list_slots":
        return {"slots": open_slots(sid)[:8]}
    if name == "hold_slot":
        slot = next((x for x in open_slots(sid) if x["slot_id"] == args["slot_id"]), None)
        if not slot:
            return {"error": "slot not available; call list_slots again"}
        if not s["quote"]:
            return {"error": "call price_quote first"}
        if slot["rush"] and not s["scope"]["rush"]:
            return {"error": "this slot is within 3 business days: re-run price_quote with rush=true first"}
        HOLDS[slot["slot_id"]] = (sid, time.time() + 900)
        s["slot"] = slot
        return {"held": slot["label"], "deposit_due": s["quote"]["deposit"],
                "next": "The PayPal deposit button is now on the customer's job sheet."}
    return {"error": f"unknown tool {name}"}


def add_user(s, content):
    # after a capped tool loop the history already ends on a user turn; merge so roles keep alternating
    if s["messages"] and s["messages"][-1]["role"] == "user":
        s["messages"][-1]["content"].extend(content)
    else:
        s["messages"].append({"role": "user", "content": content})


def agent_turn(sid, s, content):
    add_user(s, content)
    used = []
    for _ in range(MAX_LOOP):
        count_call()
        r = client.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM, tools=TOOLS,
                                   output_config={"effort": "low"}, messages=s["messages"])
        s["messages"].append({"role": "assistant", "content": r.content})
        if r.stop_reason == "pause_turn":
            continue
        if r.stop_reason != "tool_use":
            break
        results = []
        for b in r.content:
            if b.type == "tool_use":
                used.append(b.name)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(run_tool(sid, s, b.name, b.input))})
        s["messages"].append({"role": "user", "content": results})
    if r.stop_reason == "refusal":
        return "Sorry, I can't help with that. I can scope and book an underwater inspection.", used
    text = "\n".join(b.text for b in r.content if b.type == "text").strip()
    return text or "Done. Check the job sheet on the right.", used


def count_call():
    today = dt.date.today()
    if DAILY["day"] != today:
        DAILY.update(day=today, n=0)
    if DAILY["n"] >= DAILY_CAP:
        raise HTTPException(429, "The demo hit its daily AI budget. Please try again tomorrow.")
    DAILY["n"] += 1


def rate_limit(request):
    ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    q, now = CALLS_BY_IP[ip], time.time()
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= PER_IP_HOURLY:
        raise HTTPException(429, "Too many messages from your address. Try again in an hour.")
    q.append(now)


def public_state(s):
    return {k: s[k] for k in ("quote", "scope", "slot", "booking")}


app = FastAPI()


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/config")
def config():
    return {"paypal_client_id": paypal.CLIENT_ID, "mock_paypal": paypal.MOCK, "mock_ai": MOCK_AI, "model": MODEL}


def session(body):
    sid = str(body.get("session_id", ""))[:64]
    if len(sid) < 8:
        raise HTTPException(400, "bad session_id")
    return sid, SESSIONS[sid]


@app.post("/api/chat")
def chat(body: dict, request: Request):
    rate_limit(request)
    sid, s = session(body)
    msg = str(body.get("message", "")).strip()[:2000]
    image = body.get("image") or ""
    if not msg and not image:
        raise HTTPException(400, "empty message")
    content = []
    if image:
        head, _, data = image.partition(",")
        if not head.startswith("data:image/jpeg;base64") or len(data) > 1_500_000:
            raise HTTPException(400, "photo must be a JPEG under ~1 MB (the page resizes it for you)")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    content.append({"type": "text", "text": msg or "Here's a photo of the site."})
    with s["lock"]:
        if s["turns"] >= MAX_USER_TURNS:
            raise HTTPException(429, "This chat is at its message limit. Reload the page to start a new job.")
        s["turns"] += 1
        reply, tools = agent_turn(sid, s, content)
        return {"reply": reply, "tools": tools, "state": public_state(s)}


@app.post("/api/order")
def order(body: dict):
    sid, s = session(body)
    with s["lock"]:
        q, slot = s["quote"], s["slot"]
        if not q or not slot or s["booking"]:
            raise HTTPException(400, "need a quote and a held slot first")
        if not slot_free(slot["slot_id"], sid):
            raise HTTPException(409, "that slot was just taken; ask the agent for another")
        HOLDS[slot["slot_id"]] = (sid, time.time() + 900)
        desc = f"30% deposit, ROV inspection: {s['scope']['site_name']} on {slot['label']} ({q['id']})"
        try:
            s["order_id"] = paypal.create_order(q["id"] + "-" + slot["slot_id"], q["deposit"], desc)
        except paypal.PayPalError as e:
            raise HTTPException(502, str(e))
        return {"id": s["order_id"]}


@app.post("/api/capture")
def capture(body: dict):
    sid, s = session(body)
    with s["lock"]:
        if not s["order_id"] or body.get("order_id") != s["order_id"]:
            raise HTTPException(400, "unknown order")
        if s["booking"]:
            return {"reply": "", "tools": [], "state": public_state(s)}
        q, slot = s["quote"], s["slot"]
        try:
            cap = paypal.capture(s["order_id"], q["deposit"])
        except paypal.PayPalError as e:
            raise HTTPException(402, str(e))
        # ponytail: if the 15-min hold lapsed and someone else paid for this slot first, both are booked; fine at demo volume
        bid = "PS-" + uuid.uuid4().hex[:6].upper()
        BOOKED[slot["slot_id"]] = bid
        s["booking"] = {"id": bid, "slot": slot["label"], "capture_id": cap["capture_id"],
                        "deposit": cap["amount"], "payer": cap["payer_name"], "order_id": s["order_id"]}
        try:
            s["booking"]["invoice_id"] = paypal.draft_balance_invoice(q, s["booking"], cap["payer_email"])
        except Exception as e:  # stretch feature: a failed draft must not lose a paid booking
            s["booking"]["invoice_error"] = str(e)[:200]
        event = (f"PAYMENT EVENT (from the server, not the customer): PayPal capture {cap['capture_id']} COMPLETED "
                 f"for ${cap['amount']}. Booking {bid} confirmed for {slot['label']}. "
                 f"Balance ${q['balance']:.2f} is due after the inspection"
                 + (f"; PayPal invoice draft {s['booking']['invoice_id']} prepared." if "invoice_id" in s["booking"] else ".")
                 + " Write the confirmation and the pre-dive plan.")
        try:
            reply, tools = agent_turn(sid, s, [{"type": "text", "text": event}])
        except Exception:
            reply, tools = f"Deposit received. Booking {bid} is confirmed for {slot['label']}.", []
        return {"reply": reply, "tools": tools, "state": public_state(s)}

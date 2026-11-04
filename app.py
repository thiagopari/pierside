"""Pierside: an AI agent that scopes an underwater ROV inspection, prices it from a fixed
rate card, takes a 30% deposit through PayPal, and books the dive slot.

The model gathers scope and talks to the customer. Money and bookings are deterministic
server code: prices come only from pricing.quote, and a slot is booked only after the
server has captured and verified the PayPal payment.
"""
import asyncio, datetime as dt, hmac, json, os, sqlite3, threading, time, uuid
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import jobs
import paypal
import precedent
import pricing
from jobs import MOCK_AI, client

MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
PER_IP_HOURLY = int(os.environ.get("PER_IP_HOURLY", "40"))
DAILY_CAP = int(os.environ.get("DAILY_MODEL_CALLS", "600"))
MAX_USER_TURNS = 25
MAX_LOOP = 6
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")  # the dive operator's key for /api/ops/*; unset = disabled
WORKFLOW = os.environ.get("WORKFLOW_SLUG", "")  # Render Workflows service; unset = jobs.py runs in-process
if WORKFLOW:
    from render import Render
    workflows = Render().workflows  # RENDER_API_KEY, or RENDER_USE_LOCAL_DEV=1 for `render workflows dev`

# Sessions, orders, bookings and webhook events live in SQLite (a Render disk in production), so a
# paid booking survives restarts and a buyer who closes the tab after approving still gets booked.
DB = sqlite3.connect(os.environ.get("DB_PATH", "pierside.db"), check_same_thread=False, isolation_level=None)
DB.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, session_id TEXT NOT NULL,
    order_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot ON bookings(slot_id) WHERE status = 'confirmed';
CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY, type TEXT, result TEXT, received REAL, body TEXT);
""")
DB_LOCK = threading.Lock()  # ponytail: one connection behind one lock; fine for a single instance
LOCKS = defaultdict(threading.Lock)  # per session
PAY_LOCK = threading.Lock()  # ponytail: serializes every capture+book; per-slot locks if volume ever matters
HOLDS = {}  # slot_id -> (session_id, expiry); 15-minute holds may be lost on restart, bookings are not


def db(sql, *args):
    with DB_LOCK:
        return DB.execute(sql, args).fetchall()


def load(sid):
    row = db("SELECT data FROM sessions WHERE id = ?", sid)
    return json.loads(row[0][0]) if row else {"messages": [], "history": [], "quote": None, "scope": None,
                                              "slot": None, "order_id": None, "booking": None, "turns": 0}


def save(sid, s):
    db("INSERT OR REPLACE INTO sessions VALUES (?, ?)", sid, json.dumps(s))


def booked_slots():
    return {r[0] for r in db("SELECT slot_id FROM bookings WHERE status = 'confirmed'")}


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
- With your first price_quote, also call search_similar_sites (same scope, plus the customer's own \
words about the site). If it finds a past job, mention the closest one in one sentence (where, and \
what the crew found) and say it is demo data. Past jobs never change the price.
- After the quote, call list_slots and offer two or three options. When the customer picks one, \
call hold_slot. Then tell them to pay the deposit with the PayPal button on the job sheet.
- You cannot take payment or confirm a booking. The server books the slot only after PayPal \
confirms the deposit, and then sends you a PAYMENT EVENT. When it asks for the pre-dive plan, \
write only the plan, as "-" bullets: site access, tide/current window, dive \
plan for this asset, safety, and the deliverables.
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
        "name": "search_similar_sites",
        "description": "Find up to 3 completed past jobs most like this one: same asset type, similar size, depth, "
                       "visibility and growth, and site notes that mean the same thing. Each has what the crew found "
                       "and the hours the dive took. Context for the customer only, never a price.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_type", "quantity", "max_depth_ft", "visibility", "fouling", "site_notes"],
            "properties": {
                "asset_type": {"type": "string", "enum": ["hull", "pilings", "seawall", "mooring"]},
                "quantity": {"type": "number"},
                "max_depth_ft": {"type": "number"},
                "visibility": {"type": "string", "enum": ["good", "fair", "poor"]},
                "fouling": {"type": "string", "enum": ["light", "moderate", "heavy", "unknown"]},
                "site_notes": {"type": "string", "description": "the customer's words about the site, its growth and their worries"},
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


def slot_free(sid, session_id, booked=None):
    held = HOLDS.get(sid)
    return sid not in (booked_slots() if booked is None else booked) and (not held or held[0] == session_id or held[1] < time.time())


def open_slots(session_id):
    booked = booked_slots()
    return [{k: v for k, v in s.items() if k != "_taken_demo"} for s in all_slots()
            if not s["_taken_demo"] and slot_free(s["slot_id"], session_id, booked)]


def run_tool(sid, s, name, args):
    if name == "price_quote":
        q = pricing.quote(args)
        s["scope"], s["quote"] = args, {**q, "id": "Q-" + uuid.uuid4().hex[:8].upper()}
        if s["slot"] and not args["rush"] and s["slot"]["rush"]:
            s["slot"] = None
            q["note"] = "Held slot released: it needs rush=true."
        return q
    if name == "search_similar_sites":
        s["precedent"] = precedent.search(**args)
        return s["precedent"]
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
    used, said = [], []
    for _ in range(MAX_LOOP):
        count_call()
        r = client.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM, tools=TOOLS,
                                   output_config={"effort": "low"}, messages=s["messages"])
        s["messages"].append({"role": "assistant", "content": [block(b) for b in r.content]})
        said += [b.text.strip() for b in r.content if b.type == "text" and b.text.strip()]  # text next to a tool call counts too
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
    return "\n\n".join(said) or "Done. Check the job sheet on the right.", used


def block(b):
    """SDK content block -> plain dict, so the conversation can be stored as JSON and sent back as-is."""
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return b.model_dump(exclude_none=True)


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
    return {k: s.get(k) for k in ("quote", "scope", "slot", "booking", "precedent")}


app = FastAPI()


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/config")
def config():
    return {"paypal_client_id": paypal.CLIENT_ID, "mock_paypal": paypal.MOCK, "mock_ai": MOCK_AI, "model": MODEL}


def session_id(body):
    sid = str(body.get("session_id", ""))[:64]
    if len(sid) < 8:
        raise HTTPException(400, "bad session_id")
    return sid


@app.get("/api/session")
def get_session(session_id: str):
    s = load(session_id[:64])
    return {"history": s["history"], "state": public_state(s)}


@app.post("/api/chat")
def chat(body: dict, request: Request):
    rate_limit(request)
    sid = session_id(body)
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
    with LOCKS[sid]:
        s = load(sid)
        if s["turns"] >= MAX_USER_TURNS:
            raise HTTPException(429, "This chat is at its message limit. Start a new job.")
        s["turns"] += 1
        s["history"].append({"who": "me", "text": msg or "(photo)"})
        reply, tools = agent_turn(sid, s, content)
        s["history"].append({"who": "bot", "text": reply, "tools": tools})
        save(sid, s)
        return {"reply": reply, "tools": tools, "state": public_state(s)}


@app.post("/api/order")
def order(body: dict):
    sid = session_id(body)
    with LOCKS[sid]:
        s = load(sid)
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
        db("INSERT OR REPLACE INTO orders VALUES (?, ?)", s["order_id"], sid)
        save(sid, s)
        return {"id": s["order_id"]}


def ack(b):
    return f"Deposit received: booking {b['id']} is confirmed for {b['slot']}. Your pre-dive plan will be on the job sheet in a moment."


def finalize(sid, s):
    """Capture, verify and book. Caller holds LOCKS[sid]. Idempotent: reached from the browser
    (onApprove) and from PayPal webhooks, in either order. The invoice draft and the pre-dive plan
    come after, from a Render Workflow, so the buyer and PayPal get an answer in a second or two."""
    if s["booking"]:  # the other path (browser or webhook) got here first
        return ack(s["booking"]), []
    q, slot = s["quote"], s["slot"]
    with PAY_LOCK:
        if slot["slot_id"] in booked_slots():
            # ponytail: if a webhook-only capture already took the money here, refund by hand in the PayPal dashboard
            raise HTTPException(409, "someone else booked that slot first. Ask the agent for another one.")
        try:
            cap = paypal.capture(s["order_id"], q["deposit"])
        except paypal.PayPalError as e:
            raise HTTPException(402, str(e))
        bid = "PS-" + uuid.uuid4().hex[:6].upper()
        b = s["booking"] = {"id": bid, "slot": slot["label"], "capture_id": cap["capture_id"], "status": "confirmed",
                            "deposit": cap["amount"], "payer": cap["payer_name"], "order_id": s["order_id"]}
        db("INSERT INTO bookings VALUES (?, ?, ?, ?, 'confirmed', ?, ?)",
           bid, slot["slot_id"], sid, s["order_id"], json.dumps(b), time.time())
        event = (f"PAYMENT EVENT (from the server, not the customer): PayPal capture {cap['capture_id']} COMPLETED "
                 f"for ${cap['amount']}. Booking {bid} confirmed for {slot['label']}. "
                 f"Balance ${q['balance']:.2f} is due after the inspection; PayPal will invoice it.")
        job = {"quote": q, "booking": dict(b), "payer_email": cap["payer_email"],
               "plan_request": plan_request(s, event + " Write the pre-dive plan."),
               "history": precedent.booking_doc(bid, s["scope"], q)}
        add_user(s, [{"type": "text", "text": event}])
        s["messages"].append({"role": "assistant", "content": [{"type": "text", "text": ack(b)}]})
        s["history"].append({"who": "bot", "text": ack(b), "tools": []})
        save(sid, s)  # the paid booking is on disk before any slower, optional step
    threading.Thread(target=after_payment, args=(sid, job), daemon=True).start()
    return ack(b), []


def plan_request(s, event):
    """messages.create kwargs for the pre-dive plan: the conversation so far plus the payment event.
    Photos become a note (task inputs travel as JSON through Render's API); tools stay defined but off."""
    msgs = [{**m, "content": [{"type": "text", "text": "(customer photo)"} if c["type"] == "image" else c
                              for c in m["content"]]} for m in s["messages"]]
    add_user({"messages": msgs}, [{"type": "text", "text": event}])
    return {"model": MODEL, "max_tokens": 2000, "system": SYSTEM, "tools": TOOLS, "tool_choice": {"type": "none"},
            "output_config": {"effort": "low"}, "messages": msgs}


def run_job(task, *args, key):
    """Run a jobs.py task on the Render Workflows service and wait for its result (in-process without one).
    The idempotency key makes a repeat call return the same run instead of doing the work twice."""
    if WORKFLOW:
        return workflows.run_task(f"{WORKFLOW}/{task.name}", list(args), idempotency_key=f"{task.name}-{key}").results[0]
    return asyncio.run(jobs.Inline().run(task, *args))


def after_payment(sid, job):
    """Thread: the after_payment workflow drafts the balance invoice and writes the plan; results land on the booking."""
    # ponytail: a restart mid-run drops the result; run_job with the same key (24 h) would fetch it if that matters
    try:
        out = run_job(jobs.after_payment, job, key=job["booking"]["id"])
    except Exception as e:  # the paid booking stands either way
        out = {"invoice_error": str(e)[:200]}
    with LOCKS[sid]:
        s = load(sid)
        b = s["booking"]
        b["plan"] = out.get("plan") or "The dive team will send your pre-dive plan before the dive."
        b.update({k: out[k] for k in ("invoice_id", "invoice_error") if out.get(k)})
        if out.get("plan"):
            add_user(s, [{"type": "text", "text": "SERVER NOTE: this pre-dive plan is now on the customer's job sheet:\n" + out["plan"]}])
        save(sid, s)


@app.post("/api/capture")
def capture(body: dict):
    sid = session_id(body)
    with LOCKS[sid]:
        s = load(sid)
        if not s["order_id"] or body.get("order_id") != s["order_id"]:
            raise HTTPException(400, "unknown order")
        reply, tools = finalize(sid, s)
        return {"reply": reply, "tools": tools, "state": public_state(s)}


@app.post("/api/paypal/webhook")
def webhook(event: dict, request: Request):
    """PayPal is the source of truth: an approved order gets captured and booked even if the
    buyer's tab closed, and refunds/reversals release the slot."""
    if not paypal.verify_webhook(request.headers, event):
        raise HTTPException(400, "webhook signature not verified")
    eid, kind, res = str(event.get("id", "")), event.get("event_type", ""), event.get("resource") or {}
    if db("SELECT 1 FROM webhook_events WHERE id = ?", eid):
        return {"ok": True, "result": "duplicate"}
    order_id = res.get("id") if kind.startswith("CHECKOUT.ORDER.") else \
        res.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
    row = db("SELECT session_id FROM orders WHERE order_id = ?", order_id or "")
    result = "ignored"
    if row:
        sid = row[0][0]
        with LOCKS[sid]:
            s = load(sid)
            if order_id != s["order_id"]:
                result = "stale order"  # the buyer opened a newer PayPal order; only that one books
            elif kind in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
                try:
                    finalize(sid, s)
                    result = "booked " + s["booking"]["id"]
                except HTTPException as e:
                    if e.status_code != 409:
                        raise  # non-2xx makes PayPal retry later (e.g. a capture still PENDING)
                    result = "slot taken"
            elif kind in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.DENIED") and s["booking"]:
                status = kind.rsplit(".", 1)[1].lower()
                s["booking"]["status"] = status
                db("UPDATE bookings SET status = ? WHERE order_id = ?", status, order_id)
                save(sid, s)
                result = status + ", slot released"
    db("INSERT OR IGNORE INTO webhook_events VALUES (?, ?, ?, ?, ?)", eid, kind, result, time.time(), json.dumps(event))
    return {"ok": True, "result": result}


@app.post("/api/ops/complete")
def complete_dive(body: dict, request: Request):
    """The operator marks a dive done; a workflow task emails the customer the PayPal balance invoice.
    Optional actual_hours and findings go to the job history that search_similar_sites ranks."""
    if not OPS_TOKEN or not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {OPS_TOKEN}".encode()):
        raise HTTPException(401, "operator token required")
    hours, findings = body.get("actual_hours"), str(body.get("findings") or "")[:1000]
    if hours is not None and not (isinstance(hours, (int, float)) and 0 < hours < 100):
        raise HTTPException(422, "actual_hours must be a number of hours, more than 0 and under 100")
    row = db("SELECT session_id FROM bookings WHERE id = ? AND status = 'confirmed'", str(body.get("booking_id", "")))
    if not row:
        raise HTTPException(404, "no confirmed booking with that id")
    sid = row[0][0]
    with LOCKS[sid]:
        s = load(sid)
        b = s["booking"]
        if not b.get("invoice_id"):
            raise HTTPException(409, "the balance invoice isn't drafted yet")
        try:
            b["balance_invoice"] = run_job(jobs.send_invoice, b["invoice_id"], key=b["invoice_id"])
        except Exception as e:
            raise HTTPException(502, str(e)[:200])
        b["dive"] = "complete"
        save(sid, s)
    try:  # history only: the invoice already went out
        precedent.index_job(b["id"], {"status": "completed", **({"actual_hours": float(hours)} if hours else {}),
                                      **({"findings": findings} if findings else {})})
    except Exception as e:
        print("job history not updated:", e, flush=True)
    return {"booking": b}

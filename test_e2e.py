"""End-to-end over HTTP with MOCK=1: chat -> quote -> slot hold -> PayPal order -> capture -> booked,
the same booking driven by a PayPal webhook instead (closed tab), bookings surviving a restart, the
operator's dive-complete call sending the balance invoice, and the operator's bookings list.
With ELASTIC_URL set (seeded: python precedent.py seed), the past-jobs search and the job history run
against Elasticsearch, and the test removes the bookings it indexed.
Run: .venv/bin/python test_e2e.py   (jobs.py tasks run in-process)
Against the real Workflows runtime: `render workflows dev --env-file <file with MOCK=1> -- .venv/bin/python jobs.py`,
then WORKFLOW_SLUG=pierside-jobs RENDER_USE_LOCAL_DEV=1 .venv/bin/python test_e2e.py"""
import json, os, subprocess, sys, tempfile, time, urllib.request, uuid

import precedent

PORT = 8765
DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")


def start():
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(PORT)],
                         env={**os.environ, "MOCK": "1", "DB_PATH": DB_PATH, "OPS_TOKEN": "test-ops"}, cwd=os.path.dirname(os.path.abspath(__file__)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            return p, json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config").read())
        except OSError:
            time.sleep(0.2)
    raise SystemExit(p.communicate()[1].decode()[-3000:])


def stop(p):
    p.terminate()
    err = p.communicate()[1].decode()
    if "Traceback" in err:
        print(err[-3000:])


def get(path, **headers):
    return json.loads(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers=headers)).read())


def booked(sid):
    """The plan and the invoice draft come from the after_payment workflow, a moment after the booking."""
    deadline = time.time() + 90
    while time.time() < deadline:
        s = get("/api/session?session_id=" + sid)
        if s["state"]["booking"].get("plan"):
            return s
        time.sleep(0.2)
    raise AssertionError(f"no pre-dive plan: {s}")


def scope_and_hold(sid):
    post("/api/chat", {"session_id": sid, "message": "12 pilings in Quincy, 25 ft"})
    return post("/api/chat", {"session_id": sid, "message": "first open weekday please"})


srv, cfg = start()


def post(path, body, **headers):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


try:
    assert cfg["mock_ai"] and cfg["mock_paypal"], cfg
    assert b"Job sheet" in urllib.request.urlopen(f"http://127.0.0.1:{PORT}/").read()
    assert b"agGrid.createGrid" in urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ops").read()
    sid = uuid.uuid4().hex

    r = post("/api/chat", {"session_id": sid, "message": "12 pilings in Quincy, 25 ft"})
    q = r["state"]["quote"]
    assert r["tools"] == ["price_quote", "search_similar_sites"] and q["deposit"] == round(q["total"] * 0.3, 2), r
    past = r["state"]["precedent"]
    assert len(past["jobs"]) == 3 and past["source"] == ("Elasticsearch" if precedent.URL else "local fallback (no Elastic configured)"), past
    assert all(j["asset_type"] == "pilings" and j["demo"] for j in past["jobs"]) and past["jobs"][0]["site"] in r["reply"], r

    r = post("/api/chat", {"session_id": sid, "message": "first open weekday please"})
    assert r["tools"] == ["list_slots", "hold_slot"] and r["state"]["slot"], r
    assert "first open weekday" in r["reply"] and "Held" in r["reply"], r  # text from every round of the tool loop

    order_id = post("/api/order", {"session_id": sid})["id"]
    r = post("/api/capture", {"session_id": sid, "order_id": order_id})
    b = r["state"]["booking"]
    assert b and b["deposit"] == f"{q['deposit']:.2f}" and b["id"] in r["reply"], r
    b = booked(sid)["state"]["booking"]
    assert "Pre-dive plan" in b["plan"] and b["invoice_id"].startswith("INV2-"), b

    # the held slot is now gone for everyone else; a forged order id is rejected
    other = uuid.uuid4().hex
    post("/api/chat", {"session_id": other, "message": "hi"})
    r2 = post("/api/chat", {"session_id": other, "message": "slot please"})
    assert r2["state"]["slot"]["slot_id"] != r["state"]["slot"]["slot_id"], r2
    try:
        post("/api/capture", {"session_id": other, "order_id": order_id})
        raise AssertionError("capture with someone else's order id must fail")
    except urllib.error.HTTPError as e:
        assert e.code == 400

    # closed tab: the buyer approved in PayPal but the page never called /api/capture; the webhook books it
    tab = uuid.uuid4().hex
    scope_and_hold(tab)
    tab_order = post("/api/order", {"session_id": tab})["id"]
    event = {"id": "WH-" + uuid.uuid4().hex, "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": tab_order}}
    assert post("/api/paypal/webhook", event)["result"].startswith("booked PS-")
    assert post("/api/paypal/webhook", event)["result"] == "duplicate"  # PayPal redelivers; nothing happens twice
    s = booked(tab)
    assert s["state"]["booking"]["order_id"] == tab_order and s["state"]["booking"]["id"] in s["history"][-1]["text"], s
    # the page comes back late and calls capture anyway: same booking, no second charge
    late = post("/api/capture", {"session_id": tab, "order_id": tab_order})
    assert late["state"]["booking"]["id"] == s["state"]["booking"]["id"], late
    unknown = {"id": "WH-" + uuid.uuid4().hex, "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "NOPE"}}
    assert post("/api/paypal/webhook", unknown)["result"] == "ignored"

    # restart: bookings, chat history and taken slots come back from SQLite
    stop(srv)
    srv, _ = start()
    s = get("/api/session?session_id=" + sid)
    assert s["state"]["booking"]["id"] == b["id"] and s["history"][0]["who"] == "me", s
    third = uuid.uuid4().hex
    r3 = scope_and_hold(third)
    assert r3["state"]["slot"]["slot_id"] not in (r["state"]["slot"]["slot_id"], late["state"]["slot"]["slot_id"]), r3

    # the operator marks the first dive complete: the balance invoice goes out (a workflow task), once
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        try:
            post("/api/ops/complete", {"booking_id": b["id"]}, **headers)
            raise AssertionError("ops call without the operator token must fail")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    ops = {"Authorization": "Bearer test-ops"}
    try:
        post("/api/ops/complete", {"booking_id": b["id"], "actual_hours": -2}, **ops)
        raise AssertionError("negative dive hours must be rejected")
    except urllib.error.HTTPError as e:
        assert e.code == 422
    done = post("/api/ops/complete", {"booking_id": b["id"], "actual_hours": 5.5,
                                      "findings": "Heavy mussels on the outer row; 3 ft visibility."}, **ops)["booking"]
    assert done["dive"] == "complete" and done["balance_invoice"]["status"] == "SENT" and done["balance_invoice"]["url"], done
    assert get("/api/session?session_id=" + sid)["state"]["booking"]["balance_invoice"] == done["balance_invoice"]
    try:
        post("/api/ops/complete", {"booking_id": "PS-NOPE"}, **ops)
        raise AssertionError("unknown booking must 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    # a refund releases the slot
    refund = {"id": "WH-" + uuid.uuid4().hex, "event_type": "PAYMENT.CAPTURE.REFUNDED",
              "resource": {"supplementary_data": {"related_ids": {"order_id": tab_order}}}}
    assert post("/api/paypal/webhook", refund)["result"] == "refunded, slot released"
    # the operator's grid (/ops) lists both bookings: one dive complete with its invoice sent, one refunded
    try:
        get("/api/ops/bookings")
        raise AssertionError("the bookings list without the operator token must fail")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    rows = {x["id"]: x for x in get("/api/ops/bookings", **ops)["bookings"]}
    assert rows[b["id"]]["state"] == "complete" and rows[b["id"]]["invoice"] == "SENT" and rows[late["state"]["booking"]["id"]]["state"] == "refunded", rows
    if precedent.URL:  # the booking joined the job history, then the dive results landed on it
        h = precedent._es("GET", f"/{precedent.INDEX}/_doc/{b['id']}")["_source"]
        assert h["status"] == "completed" and h["actual_hours"] == 5.5 and not h["demo"] and h["deposit"] == q["deposit"], h
        assert precedent._es("GET", f"/{precedent.INDEX}/_doc/{late['state']['booking']['id']}")["_source"]["status"] == "booked"
        for bid in (b["id"], late["state"]["booking"]["id"]):
            precedent._es("DELETE", f"/{precedent.INDEX}/_doc/{bid}?refresh=true")
    print("e2e ok:", b["id"], b["slot"], "deposit", b["deposit"], "of", q["total"], "| webhook booking + restart + refund + balance invoice + ops list ok | past jobs:", past["source"])
finally:
    stop(srv)

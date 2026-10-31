"""End-to-end over HTTP with MOCK=1: chat -> quote -> slot hold -> PayPal order -> capture -> booked.
Run: .venv/bin/python test_e2e.py"""
import json, os, subprocess, sys, time, urllib.request, uuid

PORT = 8765
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(PORT)],
                       env={**os.environ, "MOCK": "1"}, cwd=os.path.dirname(os.path.abspath(__file__)),
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


try:
    for _ in range(50):
        try:
            cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config").read())
            break
        except OSError:
            time.sleep(0.2)
    assert cfg["mock_ai"] and cfg["mock_paypal"], cfg
    assert b"Job sheet" in urllib.request.urlopen(f"http://127.0.0.1:{PORT}/").read()
    sid = uuid.uuid4().hex

    r = post("/api/chat", {"session_id": sid, "message": "12 pilings in Quincy, 25 ft"})
    q = r["state"]["quote"]
    assert r["tools"] == ["price_quote"] and q["deposit"] == round(q["total"] * 0.3, 2), r

    r = post("/api/chat", {"session_id": sid, "message": "first open weekday please"})
    assert r["tools"] == ["list_slots", "hold_slot"] and r["state"]["slot"], r

    order_id = post("/api/order", {"session_id": sid})["id"]
    r = post("/api/capture", {"session_id": sid, "order_id": order_id})
    b = r["state"]["booking"]
    assert b and b["deposit"] == f"{q['deposit']:.2f}" and b["invoice_id"].startswith("INV2-"), r
    assert "Pre-dive plan" in r["reply"], r

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
    print("e2e ok:", b["id"], b["slot"], "deposit", b["deposit"], "of", q["total"])
finally:
    srv.terminate()
    err = srv.communicate()[1].decode()
    if "Traceback" in err:
        print(err[-3000:])

"""PayPal Orders v2 + Invoicing v2 over plain REST (stdlib only).

MOCK=1 swaps the HTTP layer for an in-memory fake of the same endpoints, so every function
below runs the same code against mocks and against the real sandbox.
"""
import base64, json, os, time, uuid, urllib.error, urllib.request

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
MOCK = os.environ.get("MOCK", "auto") == "1" or (os.environ.get("MOCK", "auto") == "auto" and not (CLIENT_ID and SECRET))
if os.environ.get("MOCK") == "0" and not (CLIENT_ID and SECRET):
    raise SystemExit("MOCK=0 needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
_token = {"value": "", "exp": 0}
_fake_orders = {}


class PayPalError(Exception):
    pass


def _call(method, path, body=None, headers=None, form=None):
    if MOCK:
        return _fake(method, path, body)
    h = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if form is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        data = form.encode()
    else:
        h["Authorization"] = "Bearer " + token()
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise PayPalError(f"{method} {path} -> {e.code}: {e.read()[:500].decode(errors='replace')}")


def token():
    if time.time() < _token["exp"]:
        return _token["value"]
    auth = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    t = _call("POST", "/v1/oauth2/token", form="grant_type=client_credentials",
              headers={"Authorization": "Basic " + auth})
    _token.update(value=t["access_token"], exp=time.time() + t["expires_in"] - 60)
    return _token["value"]


def money(x):
    return {"currency_code": "USD", "value": f"{x:.2f}"}


def create_order(quote_id, deposit, description):
    o = _call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": quote_id,
            "description": description[:127],
            "amount": money(deposit),
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "Pierside", "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}}},
    }, headers={"PayPal-Request-Id": "create-" + quote_id})
    return o["id"]


def capture(order_id, expected_deposit):
    """Capture and verify. Returns {capture_id, amount, payer_email, payer_name}."""
    o = _call("POST", f"/v2/checkout/orders/{order_id}/capture", {},
              headers={"PayPal-Request-Id": "capture-" + order_id})
    cap = o["purchase_units"][0]["payments"]["captures"][0]
    # never trust the browser: book only on a COMPLETED capture for the exact deposit
    if o.get("status") != "COMPLETED" or cap["status"] != "COMPLETED" \
            or cap["amount"]["value"] != f"{expected_deposit:.2f}":
        raise PayPalError(f"capture not completed or amount mismatch: {o.get('status')} {cap['amount']}")
    payer = o.get("payer", {})
    name = payer.get("name", {})
    return {"capture_id": cap["id"], "amount": cap["amount"]["value"],
            "payer_email": payer.get("email_address", ""),
            "payer_name": f"{name.get('given_name', '')} {name.get('surname', '')}".strip()}


def draft_balance_invoice(quote, booking, payer_email):
    """Draft (not send) a PayPal invoice for the remaining balance. Returns the invoice id."""
    items = [{"name": l["label"][:200], "quantity": "1", "unit_amount": money(l["amount"])}
             for l in quote["lines"]]
    inv = _call("POST", "/v2/invoicing/invoices", {
        "detail": {"currency_code": "USD", "invoice_number": booking["id"],
                   "note": f"Deposit of ${quote['deposit']:.2f} received (PayPal capture {booking['capture_id']}). "
                           f"Balance due after the inspection on {booking['slot']}.",
                   "payment_term": {"term_type": "NET_10"}},
        "primary_recipients": [{"billing_info": {"email_address": payer_email}}] if payer_email else [],
        "items": items,
        "amount": {"breakdown": {"custom": {"label": "Deposit paid", "amount": money(-quote["deposit"])}}},
    })
    return inv.get("id") or inv.get("href", "").rsplit("/", 1)[-1]


def _fake(method, path, body):
    """In-memory stand-in for the sandbox endpoints used above (response shapes per PayPal docs)."""
    if path == "/v1/oauth2/token":
        return {"access_token": "MOCK-TOKEN", "expires_in": 32400}
    if method == "POST" and path == "/v2/checkout/orders":
        oid = "MOCK" + uuid.uuid4().hex[:13].upper()
        _fake_orders[oid] = {"id": oid, "status": "CREATED", "purchase_units": body["purchase_units"]}
        return {"id": oid, "status": "CREATED"}
    if method == "POST" and path.endswith("/capture"):
        o = _fake_orders.get(path.split("/")[4])
        if not o:
            raise PayPalError(f"POST {path} -> 404: RESOURCE_NOT_FOUND")
        amt = o["purchase_units"][0]["amount"]
        o["status"] = "COMPLETED"
        return {"id": o["id"], "status": "COMPLETED",
                "payer": {"email_address": "sb-buyer@personal.example.com",
                          "name": {"given_name": "Sandbox", "surname": "Buyer"}},
                "purchase_units": [{"payments": {"captures": [
                    {"id": "MOCKCAP" + o["id"][4:], "status": "COMPLETED", "amount": amt}]}}]}
    if method == "POST" and path == "/v2/invoicing/invoices":
        return {"rel": "self", "href": BASE + "/v2/invoicing/invoices/INV2-MOCK-" + uuid.uuid4().hex[:4].upper(), "method": "GET"}
    raise PayPalError(f"{method} {path} -> 404 (not faked)")

"""PayPal Orders v2 + Invoicing v2 over plain REST (stdlib only).

MOCK=1 swaps the HTTP layer for an in-memory fake of the same endpoints, so every function
below runs the same code against mocks and against the real sandbox.
"""
import base64, json, os, time, uuid, urllib.error, urllib.request

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
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
    """Capture if still APPROVED, then verify. Safe to repeat: the browser's onApprove and PayPal's
    webhooks both land here, and an already-captured order is just re-verified.
    Returns {capture_id, amount, payer_email, payer_name}."""
    o = _call("GET", f"/v2/checkout/orders/{order_id}")
    if o.get("status") == "APPROVED":
        o = _call("POST", f"/v2/checkout/orders/{order_id}/capture", {},
                  headers={"PayPal-Request-Id": "capture-" + order_id})
    caps = (o.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}]
    cap = caps[0]
    # never trust the browser: book only on a COMPLETED capture for the exact deposit
    if o.get("status") != "COMPLETED" or cap.get("status") != "COMPLETED" \
            or cap.get("amount", {}).get("value") != f"{expected_deposit:.2f}":
        raise PayPalError(f"capture not completed or amount mismatch: {o.get('status')} {cap.get('status')} {cap.get('amount')}")
    payer = o.get("payer", {})
    name = payer.get("name", {})
    return {"capture_id": cap["id"], "amount": cap["amount"]["value"],
            "payer_email": payer.get("email_address", ""),
            "payer_name": f"{name.get('given_name', '')} {name.get('surname', '')}".strip()}


def verify_webhook(headers, event):
    """Ask PayPal whether this webhook delivery is genuine (signature check against our webhook id)."""
    if MOCK:
        return True
    if not WEBHOOK_ID:
        return False
    r = _call("POST", "/v1/notifications/verify-webhook-signature", {
        "auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": WEBHOOK_ID, "webhook_event": event})
    return r.get("verification_status") == "SUCCESS"


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


def send_invoice(invoice_id):
    """Email the drafted balance invoice to the customer. Safe to retry: PayPal refuses a second
    send (422), so only a DRAFT is sent. Returns {status, url}; url is the customer's pay page."""
    inv = _call("GET", f"/v2/invoicing/invoices/{invoice_id}")
    if inv["status"] == "DRAFT":
        _call("POST", f"/v2/invoicing/invoices/{invoice_id}/send", {"send_to_recipient": True, "send_to_invoicer": False})
        inv["status"] = "SENT"
    return {"status": inv["status"], "url": inv["detail"]["metadata"]["recipient_view_url"]}


def _fake(method, path, body):
    """In-memory stand-in for the sandbox endpoints used above (response shapes per PayPal docs)."""
    if path == "/v1/oauth2/token":
        return {"access_token": "MOCK-TOKEN", "expires_in": 32400}
    if method == "POST" and path == "/v2/checkout/orders":
        oid = "MOCK" + uuid.uuid4().hex[:13].upper()
        _fake_orders[oid] = {"id": oid, "status": "APPROVED", "purchase_units": body["purchase_units"]}  # no buyer step in mock
        return {"id": oid, "status": "CREATED"}
    o = _fake_orders.get(path.split("/")[4]) if path.startswith("/v2/checkout/orders/") else None
    if path.startswith("/v2/checkout/orders/") and not o:
        raise PayPalError(f"{method} {path} -> 404: RESOURCE_NOT_FOUND")
    if method == "GET" and o:
        return o
    if method == "POST" and path.endswith("/capture"):
        amt = o["purchase_units"][0]["amount"]
        o.update(status="COMPLETED",
                 payer={"email_address": "sb-buyer@personal.example.com",
                        "name": {"given_name": "Sandbox", "surname": "Buyer"}},
                 purchase_units=[{**o["purchase_units"][0], "payments": {"captures": [
                     {"id": "MOCKCAP" + o["id"][4:], "status": "COMPLETED", "amount": amt}]}}])
        return o
    if method == "POST" and path == "/v2/invoicing/invoices":
        return {"rel": "self", "href": BASE + "/v2/invoicing/invoices/INV2-MOCK-" + uuid.uuid4().hex[:4].upper(), "method": "GET"}
    if path.startswith("/v2/invoicing/invoices/INV2-MOCK-"):  # stateless: workflow tasks may run in separate processes
        if method == "POST":
            return {"rel": "payer-view", "href": "https://www.sandbox.paypal.com/invoice/p/#MOCK", "method": "GET"}
        return {"id": path.split("/")[4], "status": "DRAFT",
                "detail": {"metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#MOCK"}}}
    raise PayPalError(f"{method} {path} -> 404 (not faked)")

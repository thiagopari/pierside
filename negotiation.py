"""Negotiation policy. Like pricing.py, this is deterministic code: the model can ask what a price is made
of and which options exist, and it can accept an option by id, but it never names a number or a discount.

Three read/write steps, all called through tools in app.py:
  explain()  what each charge is for and whether it can move (read-only)
  offers()   the options open to this customer right now, each priced by the rate card (read-only)
  apply()    turns an accepted option into a new scope (+ optional courtesy) that pricing.quote then prices

Two kinds of option:
  scope options     choose a standard slot instead of rush, drop an add-on. They remove a service, so they are
                    not discounts and are not capped by the floor.
  a courtesy        a small, conditional reduction of the mobilization charge for booking ahead. It is capped
                    (FLOOR_RATIO), allowed once per quote, and tied to the slot: if the customer then holds an
                    earlier slot, app.py drops it and re-quotes.

POLICY BELOW IS PLACEHOLDER. The numbers are illustrative defaults, like the rate card. The operator should
set them from real margins before using this for real jobs.
"""
import datetime as dt

import pricing

# ---- placeholder policy: the operator's to set -----------------------------------------------------------
ADVANCE_MIN_BUSINESS_DAYS = 5     # a slot at least this many business days out earns the courtesy
ADVANCE_MOBILIZATION_PCT = 0.15   # courtesy: this share off the mobilization charge
FLOOR_RATIO = 0.90                # courtesies can never take a job below 90% of the rate-card price
MAX_APPLIED = 3                   # options applied per chat; stops repeated re-negotiation
# -----------------------------------------------------------------------------------------------------------

NOTE = ("Offer only what this list contains, with these exact numbers. Never promise anything else, never "
        "invent urgency or scarcity, and say plainly which charges are firm.")

UNIT_NOUN = {"hull": "ft of hull length", "pilings": "pile", "seawall": "ft of wall", "mooring": "mooring"}
WHY_PREFIXES = ("Mobilization", "ROV survey", "Video + inspection report", "Rush scheduling")


def business_day(today, n):
    """The nth business day after `today` (n >= 1)."""
    d, count = today, 0
    while count < n:
        d += dt.timedelta(days=1)
        count += d.weekday() < 5
    return d


def min_slot_date(today):
    return business_day(today, ADVANCE_MIN_BUSINESS_DAYS)


def slot_qualifies(courtesy, slot):
    """True when no courtesy is active, or the slot is on or after the date the courtesy needs."""
    return not courtesy or slot["slot_id"][:10] >= courtesy["min_slot_date"]


def _q(scope, courtesy=None):
    return pricing.quote(scope, mobilization_courtesy=courtesy["pct"] if courtesy else 0.0)


def explain(scope, quote, courtesy=None):
    """What each charge is for, in the rate card's own numbers, and whether it can move."""
    name, unit, setup, per_unit = pricing.ASSETS[scope["asset_type"]]
    qty, depth = float(scope["quantity"]), float(scope["max_depth_ft"])
    dist = float(scope["distance_miles"])
    extra = max(dist - pricing.FREE_MILES, 0)
    addon_labels = {label for label, _ in pricing.ADDONS.values()}
    lines = []
    for l in quote["lines"]:
        label, amt = l["label"], l["amount"]
        if label.startswith("Mobilization"):
            why = (f"Getting the crew, boat and ROV to your site. The ${pricing.MOBILIZATION:,.0f} base covers the "
                   f"first {pricing.FREE_MILES} miles; beyond that it is ${pricing.PER_MILE:.2f} per mile. Your site is "
                   f"{dist:g} miles away" + (f", {extra:g} beyond the free distance." if extra else ", inside the free distance."))
            if courtesy:
                why += f" An advance-booking courtesy of {courtesy['pct']:.0%} is included."
            kind = "fixed"
        elif label.startswith("ROV survey"):
            noun = UNIT_NOUN[scope["asset_type"]]
            parts = [f"{setup:g} h setup + {per_unit:.3g} h per {noun} x {qty:g}",
                     f"x {pricing.VISIBILITY[scope['visibility']]:g} for {scope['visibility']} visibility",
                     f"x {pricing.FOULING[scope['fouling']]:g} for {scope['fouling']} marine growth"
                     + (" (assumed, since it is unknown)" if scope["fouling"] == "unknown" else "")]
            why = (f"{quote['rov_hours']:g} hours of ROV time at ${pricing.ROV_HOURLY:,.0f} per hour. The hours come from "
                   f"the job: {', '.join(parts)}, rounded to quarter hours (minimum {pricing.MIN_ROV_HOURS:g} h).")
            df = pricing.depth_factor(depth)
            if df != 1.0:
                why += f" Depth over {30 if df == 1.15 else 60} ft multiplies the ROV time cost by {df:g}."
            kind = "depends_on_facts"
        elif label.startswith("Video + inspection report"):
            why = "The recorded video and written report you keep. One flat price at any job size."
            kind = "fixed"
        elif label in addon_labels:
            why = "An optional extra you chose."
            kind = "your_choice"
        elif label.startswith("Rush scheduling"):
            why = "Slots within 3 business days mean moving other jobs around. Standard slots have no surcharge."
            kind = "your_choice"
        else:
            why, kind = "", "fixed"
        lines.append({"label": label, "amount": amt, "why": why, "can_change": kind})
    fixed = round(sum(l["amount"] for l in lines if l["can_change"] == "fixed"), 2)
    out = {
        "lines": lines,
        "fixed_total": fixed,
        "fixed_share": round(fixed / quote["total"], 3) if quote["total"] else 0,
        "deposit_note": (f"The {pricing.DEPOSIT_RATE:.0%} deposit (${quote['deposit']:.2f}) commits a crew and ROV to "
                         f"your date and is credited against the total. The balance (${quote['balance']:.2f}) is "
                         f"invoiced after the dive."),
        "can_change_meaning": {
            "fixed": "firm: the same whatever the job size or conditions (apart from a listed courtesy)",
            "depends_on_facts": "moves only if the facts change (what is on the hull, water clarity, depth), not by discount",
            "your_choice": "the customer decides: remove it by changing the booking",
        },
    }
    if scope["fouling"] == "unknown":  # the one input the rate card assumes for the customer
        out["if_confirmed"] = []
        for f in ("light", "moderate", "heavy"):
            q2 = _q({**scope, "fouling": f}, courtesy)
            out["if_confirmed"].append({"marine_growth": f, "total": q2["total"], "change": round(q2["total"] - quote["total"], 2)})
        out["if_confirmed_note"] = ("Marine growth is assumed because it is unknown. If the customer knows, or sends a "
                                    "photo, re-quote with what is true. It can go down or up.")
    return out


def offers(scope, quote, courtesy, slots, today, applied=0):
    """Options open now. `slots` = the customer's open slots (app.open_slots). Each is priced by the rate card."""
    if applied >= MAX_APPLIED:
        return []
    out = []

    def add(oid, kind, title, detail, q2, **extra):
        saving = round(quote["total"] - q2["total"], 2)
        if saving > 0:
            out.append({"id": oid, "kind": kind, "title": title, "detail": detail, "new_total": q2["total"],
                        "new_deposit": q2["deposit"], "saving": saving, **extra})

    if scope["rush"]:
        add("std_slot", "scope", "Choose a standard slot instead of a rush slot",
            "Standard slots (more than 3 business days out) carry no rush surcharge.", _q({**scope, "rush": False}, courtesy))
    for a in dict.fromkeys(scope["addons"]):
        label = pricing.ADDONS[a][0]
        add(f"drop_addon:{a}", "scope", f"Drop the add-on: {label}", "Removes the extra and its charge.",
            _q({**scope, "addons": [x for x in scope["addons"] if x != a]}, courtesy))
    if not scope["rush"] and not courtesy:
        day = min_slot_date(today).isoformat()
        qual = [s for s in slots if not s["rush"] and s["slot_id"][:10] >= day]
        c = {"pct": ADVANCE_MOBILIZATION_PCT}
        q2 = _q(scope, c)
        if qual and q2["total"] >= _q(scope)["total"] * FLOOR_RATIO:
            add("advance_booking", "courtesy", "Advance-booking courtesy",
                f"{ADVANCE_MOBILIZATION_PCT:.0%} off the mobilization charge when the dive is booked on or after "
                f"{min_slot_date(today):%a %b} {min_slot_date(today).day}, which lets the crew plan routes. It ends if an earlier slot is held.",
                q2, condition={"slot_on_or_after": day}, example_slots=[s["label"] for s in qual[:3]])
    return out


def apply(scope, courtesy, offer_id, today):
    """New (scope, courtesy) for an accepted option, or {"error"}. The caller has already checked the id is on
    the list offers() just returned; this re-checks what must hold anyway."""
    if offer_id == "std_slot" and scope["rush"]:
        return {"scope": {**scope, "rush": False}, "courtesy": courtesy}
    if offer_id.startswith("drop_addon:"):
        a = offer_id.split(":", 1)[1]
        if a in scope["addons"]:
            return {"scope": {**scope, "addons": [x for x in scope["addons"] if x != a]}, "courtesy": courtesy}
    if offer_id == "advance_booking" and not scope["rush"] and not courtesy:
        c = {"id": "advance_booking", "pct": ADVANCE_MOBILIZATION_PCT, "min_slot_date": min_slot_date(today).isoformat()}
        if _q(scope, c)["total"] >= _q(scope)["total"] * FLOOR_RATIO:  # the floor, checked again on apply
            return {"scope": scope, "courtesy": c}
    return {"error": "that option is not available for this quote"}


if __name__ == "__main__":
    base = dict(asset_type="hull", quantity=48, max_depth_ft=10, visibility="fair", fouling="unknown",
                distance_miles=15, addons=["photogrammetry_model"], rush=True, site_name="x")
    q = pricing.quote(base)
    mon = dt.date(2026, 11, 9)  # a Monday
    assert business_day(mon, 5) == dt.date(2026, 11, 16) and business_day(dt.date(2026, 11, 13), 1) == dt.date(2026, 11, 16)
    slots = [{"slot_id": f"{d}T08:00", "label": str(d), "rush": i < 3} for i, d in enumerate(
        [business_day(mon, n) for n in range(1, 8)])]
    ex = explain(base, q)
    assert [l["can_change"] for l in ex["lines"]] == ["fixed", "depends_on_facts", "fixed", "your_choice", "your_choice"], ex
    assert ex["fixed_total"] == 450 + 350 and len(ex["if_confirmed"]) == 3
    assert ex["if_confirmed"][0]["change"] < 0 < ex["if_confirmed"][2]["change"]  # light is cheaper, heavy dearer
    o = {x["id"]: x for x in offers(base, q, None, slots, mon)}
    assert set(o) == {"std_slot", "drop_addon:photogrammetry_model"}, o  # rush: no courtesy
    assert o["std_slot"]["new_total"] == pricing.quote({**base, "rush": False})["total"]
    assert o["drop_addon:photogrammetry_model"]["saving"] == round(600 * 1.2, 2)  # the rush surcharge was on it too
    std = {**base, "rush": False, "addons": []}
    qs = pricing.quote(std)
    o = {x["id"]: x for x in offers(std, qs, None, slots, mon)}
    assert set(o) == {"advance_booking"} and o["advance_booking"]["saving"] == round(450 * 0.15, 2) == 67.5, o
    assert o["advance_booking"]["condition"] == {"slot_on_or_after": "2026-11-16"}
    got = apply(std, None, "advance_booking", mon)
    assert got["courtesy"]["min_slot_date"] == "2026-11-16" and got["scope"] == std
    qc = _q(std, got["courtesy"])
    assert round(qs["total"] - qc["total"], 2) == 67.5 and qc["deposit"] + qc["balance"] == qc["total"]
    assert qc["total"] >= qs["total"] * FLOOR_RATIO
    assert not offers(std, qc, got["courtesy"], slots, mon)  # once per quote
    assert apply(std, got["courtesy"], "advance_booking", mon).get("error")
    assert not offers(std, qs, None, [s for s in slots if s["rush"]], mon)  # no qualifying slot, no courtesy
    assert not offers(std, qs, None, slots, mon, applied=MAX_APPLIED)
    assert apply(std, None, "made_up_50_percent", mon).get("error") and apply(std, None, "std_slot", mon).get("error")
    assert slot_qualifies(None, slots[0]) and not slot_qualifies(got["courtesy"], slots[0]) and slot_qualifies(got["courtesy"], slots[-1])
    print("negotiation ok: courtesy saves", o["advance_booking"]["saving"], "of", qs["total"])

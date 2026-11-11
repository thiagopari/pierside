"""Deterministic rate card. The LLM gathers scope; only this code sets prices.

Illustrative demo numbers, not a real contractor's price list.
"""

DEPOSIT_RATE = 0.30
MAX_SESSION_HOURS = 4.0  # a single dive session tops out around 4 hours (battery, air, crew fatigue)

ROV_HOURLY = 275.0
MIN_ROV_HOURS = 2.0
MOBILIZATION = 450.0
FREE_MILES = 25
PER_MILE = 3.5
REPORT = 350.0
ADDONS = {
    "photogrammetry_model": ("3D photogrammetry model", 600.0),
    "cp_readings": ("Cathodic-protection readings", 250.0),
}

# survey hours per unit of quantity, plus fixed setup hours
ASSETS = {
    "hull": ("Vessel hull", "ft of hull length", 1.0, 1 / 40),
    "pilings": ("Pier pilings", "piles", 0.5, 0.25),
    "seawall": ("Seawall / bulkhead", "ft of wall", 0.5, 1 / 150),
    "mooring": ("Mooring", "moorings", 0.0, 0.75),
}
VISIBILITY = {"good": 1.0, "fair": 1.1, "poor": 1.25}
FOULING = {"light": 1.0, "moderate": 1.1, "heavy": 1.2, "unknown": 1.1}


def depth_factor(max_depth_ft):
    if max_depth_ft > 60:
        return 1.3
    if max_depth_ft > 30:
        return 1.15
    return 1.0


def quote(s):
    """s: scope dict as validated by the price_quote tool schema."""
    name, unit, setup, per_unit = ASSETS[s["asset_type"]]
    qty = max(float(s["quantity"]), 0)
    hours = (setup + per_unit * qty) * VISIBILITY[s["visibility"]] * FOULING[s["fouling"]]
    hours = max(round(hours * 4) / 4, MIN_ROV_HOURS)  # bill in quarter hours
    rov = hours * ROV_HOURLY * depth_factor(float(s["max_depth_ft"]))

    lines = [
        ("Mobilization", MOBILIZATION + max(float(s["distance_miles"]) - FREE_MILES, 0) * PER_MILE),
        (f"ROV survey: {name}, {qty:g} {unit} ({hours:g} h at max depth {float(s['max_depth_ft']):g} ft)", rov),
        ("Video + inspection report", REPORT),
    ]
    lines += [ADDONS[a] for a in dict.fromkeys(s["addons"]) if a in ADDONS]
    lines = [(label, round(v, 2)) for label, v in lines]
    if s["rush"]:
        lines.append(("Rush scheduling (+20%)", round(sum(v for _, v in lines) * 0.2, 2)))
    total = round(sum(v for _, v in lines), 2)
    deposit = round(total * DEPOSIT_RATE, 2)
    import math
    sessions = math.ceil(hours / MAX_SESSION_HOURS)
    return {
        "lines": [{"label": l, "amount": v} for l, v in lines],
        "rov_hours": hours,
        "estimated_sessions": sessions,
        "total": total,
        "deposit": deposit,
        "balance": round(total - deposit, 2),
    }


FLEET_PARTNER_DISCOUNT = 0.12
BATCH_MOBILIZATION_DISCOUNT = 0.50
MAX_SELF_SERVE_DISCOUNT = 0.15


def evaluate_objection(objection_type, scope, current_quote=None):
    """Return a rationale, available discounts the client can accept, and escalation info."""
    q = current_quote or (quote(scope) if scope else None)
    total = q["total"] if q else 0
    result = {"objection_type": objection_type, "available_discounts": [], "rationale": ""}

    if objection_type == "mobilization":
        dist = float(scope.get("distance_miles", 0)) if scope else 0
        result["rationale"] = (
            f"Our base mobilization fee covers crew transit, secure ROV transport, and pre-dive safety checks. "
            f"The first {FREE_MILES} miles are included at no extra charge."
        )
        if dist > FREE_MILES:
            overage = (dist - FREE_MILES) * PER_MILE
            save = round(overage * BATCH_MOBILIZATION_DISCOUNT, 2)
            result["available_discounts"].append({
                "discount_id": "batch_mobilization",
                "label": f"Batch with a nearby job (50% off mileage overage)",
                "savings": save, "new_total": round(total - save, 2),
            })
        else:
            result["rationale"] += f" Your site is {dist:g} miles out, already within the free radius."

    elif objection_type == "rush":
        result["rationale"] = (
            "Next-day deployments require reassigning scheduled maintenance rotations and incur "
            "overtime for our safety tenders and pilots. The 20% surcharge covers those real costs."
        )
        if scope and scope.get("rush"):
            rush_line = next((l["amount"] for l in (q["lines"] if q else []) if "Rush" in l["label"]), 0)
            result["available_discounts"].append({
                "discount_id": "drop_rush",
                "label": "Switch to a standard mid-week slot (removes rush surcharge entirely)",
                "savings": rush_line, "new_total": round(total - rush_line, 2),
                "note": "Requires re-quoting with rush=false and picking a non-rush slot.",
            })
        else:
            result["rationale"] = "No rush surcharge is currently applied to your quote."

    elif objection_type == "addons":
        result["rationale"] = (
            "Each add-on (photogrammetry, CP readings) requires specialized equipment and post-dive processing time."
        )
        if scope:
            for addon_key in scope.get("addons", []):
                if addon_key in ADDONS:
                    label, price = ADDONS[addon_key]
                    result["available_discounts"].append({
                        "discount_id": f"drop_{addon_key}",
                        "label": f"Remove {label} (saves ${price:.2f})",
                        "savings": price, "new_total": round(total - price, 2),
                        "note": f"Requires re-quoting without {addon_key}.",
                    })

    else:  # general discount
        result["rationale"] = (
            f"Our rate of ${ROV_HOURLY}/hr covers a mandatory two-person safety crew, maritime liability "
            f"insurance, equipment maintenance, and regulatory compliance. These are operational floors, not margins."
        )

    # Fleet Partner is always available as a minor discount
    fleet_save = round(total * FLEET_PARTNER_DISCOUNT, 2)
    result["available_discounts"].append({
        "discount_id": "fleet_partner",
        "label": f"Fleet Partner enrollment ({FLEET_PARTNER_DISCOUNT:.0%} off this and all future jobs)",
        "savings": fleet_save, "new_total": round(total - fleet_save, 2),
    })

    result["escalation"] = {
        "threshold": f"Discounts above {MAX_SELF_SERVE_DISCOUNT:.0%} (${round(total * MAX_SELF_SERVE_DISCOUNT, 2):.2f}) require operator approval.",
        "action": "If the customer needs a larger adjustment, use contact_operator to send their request with the quote and their reasoning.",
    }
    return result


def apply_discount(discount_id, scope, current_quote):
    """Apply an approved discount and return the adjusted quote. Returns None if invalid."""
    total = current_quote["total"]

    if discount_id == "fleet_partner":
        save = round(total * FLEET_PARTNER_DISCOUNT, 2)
        label = f"Fleet Partner discount ({FLEET_PARTNER_DISCOUNT:.0%})"
    elif discount_id == "batch_mobilization":
        dist = float(scope.get("distance_miles", 0))
        overage = max(dist - FREE_MILES, 0) * PER_MILE
        save = round(overage * BATCH_MOBILIZATION_DISCOUNT, 2)
        label = "Batch mobilization discount (50% mileage overage)"
    elif discount_id == "drop_rush":
        return None  # handled by re-quoting with rush=false
    elif discount_id.startswith("drop_"):
        return None  # handled by re-quoting without the addon
    else:
        return None

    if save <= 0:
        return None

    new_total = round(total - save, 2)
    new_deposit = round(new_total * DEPOSIT_RATE, 2)
    new_lines = list(current_quote["lines"]) + [{"label": label, "amount": -save}]
    return {
        **current_quote,
        "lines": new_lines,
        "total": new_total,
        "deposit": new_deposit,
        "balance": round(new_total - new_deposit, 2),
        "discount_applied": discount_id,
    }


def draft_operator_email(scope, current_quote, client_reasoning):
    """Draft an email to the operator requesting a custom price adjustment."""
    q = current_quote
    name, unit, _, _ = ASSETS[scope["asset_type"]]
    return {
        "to": "operator@pierside.com",
        "subject": f"Price adjustment request: {scope.get('site_name', 'unknown site')}",
        "body": (
            f"A client is requesting a price adjustment beyond the self-serve threshold "
            f"({MAX_SELF_SERVE_DISCOUNT:.0%}).\n\n"
            f"JOB SCOPE\n"
            f"  Asset: {name}, {scope['quantity']:g} {unit}\n"
            f"  Depth: {scope['max_depth_ft']:g} ft | Visibility: {scope['visibility']} | Fouling: {scope['fouling']}\n"
            f"  Distance: {scope['distance_miles']:g} mi | Rush: {'yes' if scope.get('rush') else 'no'}\n"
            f"  Add-ons: {', '.join(scope.get('addons', [])) or 'none'}\n\n"
            f"CURRENT QUOTE\n"
            + "\n".join(f"  {l['label']}: ${l['amount']:.2f}" for l in q["lines"])
            + f"\n  Total: ${q['total']:.2f} | Deposit (30%): ${q['deposit']:.2f}\n\n"
            f"CLIENT'S REASONING\n"
            f"  {client_reasoning}\n\n"
            f"Please reply with an approved adjustment or contact the client directly."
        ),
        "status": "drafted",
    }


if __name__ == "__main__":
    base = dict(asset_type="pilings", quantity=12, max_depth_ft=25, visibility="fair",
                fouling="moderate", distance_miles=10, addons=[], rush=False)
    q = quote(base)
    # 0.5 + 12*0.25 = 3.5 h * 1.1 * 1.1 = 4.235 -> 4.25 h
    assert q["rov_hours"] == 4.25, q
    assert q["total"] == 450 + 4.25 * 275 + 350, q
    assert q["deposit"] == round(q["total"] * 0.3, 2) and q["deposit"] + q["balance"] == q["total"]
    assert quote(dict(base, quantity=1))["rov_hours"] == MIN_ROV_HOURS
    deep = quote(dict(base, max_depth_ft=70, rush=True, addons=["cp_readings", "cp_readings"]))
    assert abs(deep["total"] - (450 + 4.25 * 275 * 1.3 + 350 + 250) * 1.2) < 0.02, deep
    assert deep["total"] == round(sum(l["amount"] for l in deep["lines"]), 2)
    print("pricing ok", q["total"], q["deposit"])

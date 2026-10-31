"""Deterministic rate card. The LLM gathers scope; only this code sets prices.

Illustrative demo numbers, not a real contractor's price list.
"""

DEPOSIT_RATE = 0.30

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
    return {
        "lines": [{"label": l, "amount": v} for l, v in lines],
        "rov_hours": hours,
        "total": total,
        "deposit": deposit,
        "balance": round(total - deposit, 2),
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

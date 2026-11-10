"""Scripted stand-in for anthropic.Anthropic() when MOCK=1: same messages.create shape, so
app.agent_turn runs its real tool loop. Turn 1 quotes and looks up similar past jobs (two tool
calls in one turn), turn 2 lists + holds a slot, the payment event gets a pre-dive plan.
A message starting with "Ignore" plays a model that fell for a prompt injection: it asks
price_quote for 50% off and grabs a rush slot at the normal price, so tests can show the server says no."""
import json, uuid
from types import SimpleNamespace as NS

SCOPE = {"asset_type": "pilings", "quantity": 12, "max_depth_ft": 25, "visibility": "fair", "fouling": "unknown"}
QUOTE = {**SCOPE, "distance_miles": 10, "addons": [], "rush": False, "site_name": "Demo marina, A dock"}


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn")


def call(*calls, say=""):
    """calls: (name, input) pairs, all in one assistant turn, after the text `say` if any."""
    return NS(content=([NS(type="text", text=say)] if say else [])
              + [NS(type="tool_use", id="toolu_" + uuid.uuid4().hex[:8], name=n, input=a) for n, a in calls],
              stop_reason="tool_use")


class FakeClaude:
    def __init__(self):
        self.messages = self

    def create(self, messages, **_):
        last = messages[-1]["content"]
        results = [b for b in last if isinstance(b, dict) and b["type"] == "tool_result"]
        if results:
            calls = [b for b in messages[-2]["content"] if b["type"] == "tool_use"]
            out = {c["name"]: json.loads(r["content"]) for c, r in zip(calls, results)}
            if "list_slots" in out:  # listed next to a re-quote = the injected turn, which takes a rush slot
                slot = next(x for x in out["list_slots"]["slots"] if x["rush"] == ("price_quote" in out))
                return call(("hold_slot", {"slot_id": slot["slot_id"]}), say=f"{slot['label']} is the first open weekday.")
            if "price_quote" in out:
                q, past = out["price_quote"], out.get("search_similar_sites", {}).get("jobs")
                return text(f"Assumed 12 pilings, 25 ft, fair visibility. Total ${q['total']:.2f}, "
                            f"30% deposit ${q['deposit']:.2f}. "
                            + (f"A similar past job ({past[0]['site']}, demo data): {past[0]['findings']} " if past else "")
                            + "Shall I find a dive slot?")
            h = out["hold_slot"]
            return text(f"Held {h['held']}. Pay the deposit with the PayPal button to book it." if "held" in h
                        else f"The server refused the hold: {h['error']}")
        said = " ".join(b["text"] for b in last if b["type"] == "text")
        if said.startswith("PAYMENT EVENT"):
            return text("Deposit received, you're booked. Pre-dive plan:\n- Site access: gate code by email\n"
                        "- Tide window: dive at slack water\n- Dive plan: piles in numbered order\n"
                        "- Safety: dive flag up, no props within 50 ft\n- Deliverables: video + report in 3 business days")
        if said.lower().startswith("ignore"):  # the real strict schema has no discount field; pricing.quote ignores unknown keys anyway
            return call(("price_quote", {**QUOTE, "discount_pct": 50}), ("list_slots", {}))
        if not any(b.get("name") == "price_quote" for m in messages for b in m["content"]):
            return call(("price_quote", QUOTE), ("search_similar_sites", {**SCOPE, "site_notes": said}))
        return call(("list_slots", {}))

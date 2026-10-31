"""Scripted stand-in for anthropic.Anthropic() when MOCK=1: same messages.create shape, so
app.agent_turn runs its real tool loop. Turn 1 quotes, turn 2 lists + holds a slot, the
payment event gets a pre-dive plan."""
import json, uuid
from types import SimpleNamespace as NS


def text(t):
    return NS(content=[NS(type="text", text="[mock AI] " + t)], stop_reason="end_turn")


def call(name, args):
    return NS(content=[NS(type="tool_use", id="toolu_" + uuid.uuid4().hex[:8], name=name, input=args)],
              stop_reason="tool_use")


class FakeClaude:
    def __init__(self):
        self.messages = self

    def create(self, messages, **_):
        last = messages[-1]["content"]
        results = [b for b in last if isinstance(b, dict) and b["type"] == "tool_result"]
        if results:
            prev = next(b for b in messages[-2]["content"] if b.type == "tool_use")
            out = json.loads(results[-1]["content"])
            if prev.name == "price_quote":
                return text(f"Assumed 12 pilings, 25 ft, fair visibility. Total ${out['total']:.2f}, "
                            f"30% deposit ${out['deposit']:.2f}. Shall I find a dive slot?")
            if prev.name == "list_slots":
                slot = next(x for x in out["slots"] if not x["rush"])
                return call("hold_slot", {"slot_id": slot["slot_id"]})
            return text(f"Held {out.get('held')}. Pay the deposit with the PayPal button to book it.")
        said = " ".join(b["text"] for b in last if b["type"] == "text")
        if said.startswith("PAYMENT EVENT"):
            return text("Deposit received, you're booked. Pre-dive plan:\n- Site access: gate code by email\n"
                        "- Tide window: dive at slack water\n- Dive plan: piles in numbered order\n"
                        "- Safety: dive flag up, no props within 50 ft\n- Deliverables: video + report in 3 business days")
        if not any(getattr(b, "name", "") == "price_quote" for m in messages for b in m["content"] if not isinstance(b, dict)):
            return call("price_quote", {"asset_type": "pilings", "quantity": 12, "max_depth_ft": 25,
                                        "visibility": "fair", "fouling": "unknown", "distance_miles": 10,
                                        "addons": [], "rush": False, "site_name": "Demo marina, A dock"})
        return call("list_slots", {})

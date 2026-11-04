"""Render Workflows: the slow, retryable work around a booking, off the request path.

after_payment fans out three tasks in parallel, each on its own Render compute with retries:
draft the PayPal balance invoice, write the pre-dive plan, and add the booking to the job history
in Elasticsearch (a no-op without ELASTIC_URL). send_invoice emails the
customer that invoice once the operator marks the dive complete.

On Render this file is the workflow service (python jobs.py). app.py starts the tasks by
slug through the Render API, or runs the same functions in-process (Inline) when no
workflow service is configured: MOCK mode, plain local runs and the tests.
"""
import asyncio, inspect, os

from render.workflows import Retry, Workflows

import paypal
import precedent

MOCK_AI = paypal.MOCK if os.environ.get("MOCK", "auto") != "auto" else not os.environ.get("ANTHROPIC_API_KEY")
if os.environ.get("MOCK") == "0" and not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("MOCK=0 needs ANTHROPIC_API_KEY")
if MOCK_AI:
    from fake_claude import FakeClaude
    client = FakeClaude()
else:
    import anthropic
    client = anthropic.Anthropic()

app = Workflows(default_retry=Retry(max_retries=3, wait_duration_ms=2000, backoff_scaling=2),
                default_timeout=120, default_plan="starter")


@app.task
def draft_invoice(ctx, quote, booking, payer_email):
    # ponytail: a retry after a lost response hits DUPLICATE_INVOICE_NUMBER; look it up with search-invoices if that shows up
    return paypal.draft_balance_invoice(quote, booking, payer_email)


@app.task
def pre_dive_plan(ctx, request):
    """request = the messages.create kwargs app.py built (conversation + payment event, tools off)."""
    r = client.messages.create(**request)
    text = "\n".join(b.text for b in r.content if b.type == "text").strip()
    if r.stop_reason == "refusal" or not text:
        raise RuntimeError(f"no plan written (stop_reason {r.stop_reason})")
    return text


@app.task(retry=Retry(max_retries=1, wait_duration_ms=1000), timeout_seconds=40)  # short: the plan waits on it
def index_job(ctx, doc_id, doc):
    precedent.index_job(doc_id, doc)


@app.task(retry=Retry(max_retries=0, wait_duration_ms=0))  # its subtasks retry on their own
async def after_payment(ctx, job):
    inv, plan, _ = await asyncio.gather(  # a failed index_job is ignored: history is not the booking
        ctx.run(draft_invoice, job["quote"], job["booking"], job["payer_email"]),
        ctx.run(pre_dive_plan, job["plan_request"]),
        ctx.run(index_job, job["booking"]["id"], job["history"]), return_exceptions=True)
    failed = lambda r: isinstance(r, BaseException)
    return {"invoice_id": None if failed(inv) else inv, "invoice_error": str(inv)[:200] if failed(inv) else None,
            "plan": None if failed(plan) else plan}


@app.task
def send_invoice(ctx, invoice_id):
    return paypal.send_invoice(invoice_id)


class Inline:
    """Stands in for Render's task context when the tasks run in this process."""
    async def run(self, task, *args):
        r = task.func(self, *args)
        return await r if inspect.isawaitable(r) else r


if __name__ == "__main__":
    app.start()

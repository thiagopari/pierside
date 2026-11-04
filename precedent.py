"""Similar past jobs for the agent, from Elasticsearch, with a local fallback.

The past jobs are SEEDED DEMO DATA, not real customers: demo_jobs() generates them from the rate
card with a fixed seed, so Elastic and the fallback rank the same jobs. Real bookings join them in
the index (demo: false) through the after_payment workflow, and the operator's actual dive hours
and findings land on them at dive complete. Precedent is context for the customer; prices still
come only from pricing.quote.

Elastic: set ELASTIC_URL (and ELASTIC_API_KEY), then `python precedent.py seed` creates the index
and loads the demo jobs; `python precedent.py report` prints the ES|QL dashboard panels as tables.
Without ELASTIC_URL, or if Elastic fails, search() ranks the demo jobs in Python instead.
"""
import datetime as dt, json, os, random, re, sys, urllib.error, urllib.request

import pricing

URL = os.environ.get("ELASTIC_URL", "").rstrip("/")
KEY = os.environ.get("ELASTIC_API_KEY", "")
INFERENCE_ID = os.environ.get("ELASTIC_INFERENCE_ID", "")  # empty = the cluster's default embedding endpoint
INDEX = "pierside-job-history"
DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elastic", "dashboard.json")
MAPPING = {"properties": {
    "job_id": {"type": "keyword"}, "date": {"type": "date"}, "site": {"type": "text"}, "demo": {"type": "boolean"},
    "status": {"type": "keyword"}, "asset_type": {"type": "keyword"}, "quantity": {"type": "float"},
    "max_depth_ft": {"type": "float"}, "visibility": {"type": "keyword"}, "fouling": {"type": "keyword"},
    "rush": {"type": "boolean"}, "est_hours": {"type": "float"}, "actual_hours": {"type": "float"},
    "total": {"type": "scaled_float", "scaling_factor": 100}, "deposit": {"type": "scaled_float", "scaling_factor": 100},
    "findings": {"type": "text", "analyzer": "english", "copy_to": "findings_semantic"},  # keyword (BM25) side of the hybrid search
    "findings_semantic": {"type": "semantic_text", **({"inference_id": INFERENCE_ID} if INFERENCE_ID else {})},
}}
FIELDS = ["job_id", "site", "date", "demo", "asset_type", "quantity", "max_depth_ft", "visibility", "fouling",
          "est_hours", "actual_hours", "findings"]
NOTE = ("Jobs marked demo are seeded demo data, not real customers: say so if you mention one. "
        "Context only; the price comes from price_quote.")


def demo_jobs(n=80):
    """Past jobs generated from the rate card with a fixed seed. The rate card underestimates murky,
    heavily fouled and deep dives a little, which is what the dashboard's hours panel surfaces."""
    rnd = random.Random(7)
    towns = ["Quincy", "Hingham", "Hull", "Winthrop", "Weymouth", "Charlestown", "East Boston", "Dorchester",
             "Lynn", "Salem", "Marblehead", "Scituate", "Cohasset", "Gloucester", "Plymouth", "Duxbury"]
    kinds = {"pilings": ["marina, A dock", "town pier", "ferry landing", "yacht club fuel dock", "fish pier"],
             "hull": ["sloop", "trawler", "catamaran", "lobster boat", "passenger ferry"],
             "seawall": ["harborwalk seawall", "condo bulkhead", "boatyard bulkhead"],
             "mooring": ["harbor mooring field", "yacht club moorings"]}
    sizes = {"pilings": [6, 8, 10, 12, 16, 20, 24, 30, 40], "hull": [32, 38, 45, 52, 65, 80, 110],
             "seawall": [150, 300, 450, 600, 900], "mooring": [1, 2, 4, 6, 10]}
    depths = {"pilings": (8, 40), "hull": (5, 14), "seawall": (6, 25), "mooring": (10, 60)}
    growth = {"light": ["light slime only", "a thin film of algae, easy to see through", "minimal growth"],
              "moderate": ["moderate barnacles and weed on the upper third", "mixed mussels and weed, test patches cleaned by hand"],
              "heavy": ["dense blue mussels and kelp on the outer row", "heavy barnacle crust, windows scraped before every reading",
                        "thick growth hid the surface, most of the dive spent cleaning"]}
    water = {"good": ["clear water, about 10 ft visibility", "good visibility after a dry week"],
             "fair": ["about 4 ft visibility on the flood tide", "some silt from the thrusters, workable"],
             "poor": ["under 2 ft visibility, worked mostly by sonar", "storm runoff turned the water brown, waited out the plume twice",
                      "river outflow made it murky, the ROV crept along the structure"]}
    defects = {"pilings": ["section loss at the splash zone on two piles", "a cracked concrete jacket on one pile",
                           "shipworm damage at the mudline on a timber pile", "no structural defects"],
               "hull": ["two missing zinc anodes", "a fouled line on the prop shaft", "blistering on the rudder", "coating in good shape"],
               "seawall": ["a scour hole at the toe of the wall", "spalling on the cap with exposed rebar",
                           "a void behind one panel joint", "the wall in good condition"],
               "mooring": ["chain wear past 20% at the shackle", "a buried block that needs relocating",
                           "a seized swivel on one mooring", "all tackle within limits"]}
    crew = ["Launched from the float with a two-person crew.", "Worked from the pier deck at low tide.",
            "Boat traffic paused the dive twice.", "Finished inside one tide window.", "Tether snagged once on old fender chain.",
            "Owner booked a follow-up in 12 months.", "Report sent the next morning.", "Cold northeast wind, the launch took two tries."]
    notes = random.Random(8)  # separate stream: the crew notes don't shift the jobs' numbers
    end, jobs = dt.date(2026, 10, 30), []
    for i in range(1, n + 1):
        a = rnd.choices(list(kinds), [45, 25, 15, 15])[0]
        qty, town = rnd.choice(sizes[a]), rnd.choice(towns)
        vis = rnd.choices(["good", "fair", "poor"], [30, 45, 25])[0]
        foul = rnd.choices(["light", "moderate", "heavy"], [25, 45, 30])[0]
        depth = rnd.randint(*depths[a])
        scope = {"asset_type": a, "quantity": qty, "max_depth_ft": depth, "visibility": vis, "fouling": foul,
                 "distance_miles": rnd.randint(0, 45), "rush": rnd.random() < 0.15,
                 "addons": [x for x, p in (("photogrammetry_model", 0.2), ("cp_readings", 0.15)) if rnd.random() < p]}
        q = pricing.quote(scope)
        site = f"{town} {rnd.choice(kinds[a])}" if a != "hull" else f"{qty} ft {rnd.choice(kinds[a])}, {town}"
        job = {"job_id": f"DEMO-{i:03d}", "date": (end - dt.timedelta(days=rnd.randint(0, 540))).isoformat(), "site": site,
               "demo": True, "status": "quoted", **{k: scope[k] for k in ("asset_type", "quantity", "max_depth_ft",
                                                                          "visibility", "fouling", "rush")},
               "est_hours": q["rov_hours"], "total": q["total"]}
        if rnd.random() < 0.72:  # the rest were quoted but never paid a deposit
            drift = 1 + 0.2 * (vis == "poor") + 0.1 * (foul == "heavy") + 0.1 * (depth > 30) \
                - 0.08 * (vis == "good" and foul == "light") + rnd.gauss(0, 0.06)
            found = f"{rnd.choice(growth[foul]).capitalize()}; {rnd.choice(water[vis])}. Found {rnd.choice(defects[a])}. {notes.choice(crew)}"
            job.update(status="completed", deposit=q["deposit"], actual_hours=max(round(q["rov_hours"] * drift * 4) / 4, 1.0),
                       findings=found + (" Strong current at mid-flood, dived at slack water." if depth > 30 else ""))
        jobs.append(job)
    return jobs


JOBS = demo_jobs()


def search(asset_type, quantity, max_depth_ft, visibility, fouling, site_notes):
    """Up to 3 completed jobs most like this one. Never raises: Elastic trouble falls back to local ranking."""
    text = site_notes.strip() or f"{fouling} marine growth, {visibility} visibility"
    source = "local fallback (no Elastic configured)"
    if URL:
        try:
            only = [{"term": {"asset_type": asset_type}}, {"term": {"status": "completed"}}]
            alike = {"function_score": {  # same conditions, close in size and depth, matching words
                "query": {"bool": {"filter": only, "should": [{"term": {"visibility": visibility}},
                                                              {"term": {"fouling": fouling}}, {"match": {"findings": text}}]}},
                "functions": [{"gauss": {"quantity": {"origin": quantity, "scale": max(quantity / 2, 1)}}},
                              {"gauss": {"max_depth_ft": {"origin": max_depth_ft, "scale": 10}}}],
                "score_mode": "multiply", "boost_mode": "sum"}}
            meaning = {"bool": {"filter": only, "must": {"semantic": {"field": "findings_semantic", "query": text}}}}
            hits = _es("POST", f"/{INDEX}/_search", {"size": 3, "_source": FIELDS, "retriever": {"rrf": {"rank_window_size": 20, "retrievers": [
                {"standard": {"query": alike}}, {"standard": {"query": meaning}}]}}})["hits"]["hits"]
            return {"source": "Elasticsearch", "note": NOTE, "jobs": [h["_source"] for h in hits]}
        except Exception as e:  # the quote never depends on precedent
            source = f"local fallback (Elastic error: {str(e)[:80]})"
    words = set(re.findall(r"[a-z]+", text.lower()))

    def score(j):
        return (-abs(j["quantity"] - quantity) / max(quantity, 1) - abs(j["max_depth_ft"] - max_depth_ft) / 20
                + (j["visibility"] == visibility) + (j["fouling"] == fouling)
                + len(words & set(re.findall(r"[a-z]+", j["findings"].lower()))) / 4)
    done = [j for j in JOBS if j["asset_type"] == asset_type and j["status"] == "completed"]
    return {"source": source, "note": NOTE, "jobs": [{k: j[k] for k in FIELDS} for j in sorted(done, key=score, reverse=True)[:3]]}


def booking_doc(booking_id, scope, quote):
    # ponytail: only paid bookings join the index, so live traffic nudges the dashboard's conversion up;
    # index each price_quote too if live conversion matters
    return {"job_id": booking_id, "date": dt.date.today().isoformat(), "site": scope["site_name"], "demo": False,
            "status": "booked", **{k: scope[k] for k in ("asset_type", "quantity", "max_depth_ft", "visibility", "fouling", "rush")},
            "est_hours": quote["rov_hours"], "total": quote["total"], "deposit": quote["deposit"]}


def index_job(job_id, doc):
    """Upsert a live job: the booking first, then its actual hours and findings at dive complete. No-op without Elastic."""
    if URL:
        _es("POST", f"/{INDEX}/_update/{job_id}?refresh=true", {"doc": doc, "doc_as_upsert": True}, timeout=15)


def _es(method, path, body=None, timeout=5):
    ndjson = isinstance(body, bytes)
    h = {"Content-Type": "application/x-ndjson" if ndjson else "application/json"}
    if KEY:
        h["Authorization"] = "ApiKey " + KEY
    data = body if ndjson else json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, data=data, headers=h, method=method), timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:300].decode(errors='replace')}")


def seed():
    """Create the index (kept if it exists, with any live jobs in it) and (re)load the demo jobs."""
    try:
        _es("PUT", f"/{INDEX}", {"mappings": MAPPING}, timeout=60)
    except RuntimeError as e:
        if "resource_already_exists" not in str(e):
            raise
    lines = "".join(json.dumps({"index": {"_index": INDEX, "_id": j["job_id"]}}) + "\n" + json.dumps(j) + "\n" for j in JOBS)
    r = _es("POST", "/_bulk?refresh=true", lines.encode(), timeout=600)
    errors = [i["index"]["error"] for i in r["items"] if "error" in i["index"]]
    print(f"seeded {len(JOBS) - len(errors)}/{len(JOBS)} demo jobs into {INDEX}", errors[:1] or "")


def report():
    """Run each ES|QL panel of elastic/dashboard.json and print it as a table."""
    for p in json.load(open(DASHBOARD))["panels"]:
        c = p["config"]
        src = c.get("data_source") or (c.get("layers") or [{}])[0].get("data_source")
        if src:
            print(f"\n## {c['title']}\n{_es('POST', '/_query?format=txt', {'query': src['query']}, timeout=30)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        seed()
    elif sys.argv[1:] == ["report"]:
        report()
    else:  # self-check of the local ranking (no Elastic needed)
        done = [j for j in JOBS if j["status"] == "completed"]
        assert JOBS == demo_jobs() and len({j["job_id"] for j in JOBS}) == len(JOBS) and 40 < len(done) < len(JOBS)
        assert len({j["findings"] for j in done}) == len(done)  # no two past jobs read the same on the job sheet
        assert all(pricing.quote({**j, "distance_miles": 0, "addons": []})["rov_hours"] == j["est_hours"] for j in JOBS)
        URL = ""
        r = search("pilings", 12, 25, "poor", "heavy", "murky water, lots of mussels")
        assert r["source"].startswith("local") and len(r["jobs"]) == 3, r
        assert all(j["asset_type"] == "pilings" and j["actual_hours"] for j in r["jobs"]), r
        assert r["jobs"][0]["visibility"] == "poor" and r["jobs"][0]["fouling"] == "heavy", r["jobs"][0]
        print("precedent ok:", r["jobs"][0]["site"], r["jobs"][0]["est_hours"], "->", r["jobs"][0]["actual_hours"], "h")

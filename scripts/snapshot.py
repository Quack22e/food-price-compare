"""Real-data snapshot for the static price board (board/index.html).

Runs a few bounded searches with the app's own Service (same robots.txt
rules, rate limits, barrier handling and extraction as the live app) and
writes live-verification/snapshot.json:

  * observations: public menu prices per US store, in the app's import
    schema (so the board ranks them with the shipped engine),
  * coverage: what each search discovered, checked, skipped and why,
  * barriers: every refusal, challenge or block, as reported,
  * grubhub_menus: Grubhub menu prices found through MenuPages (Grubhub-owned),
    accepted only when the MenuPages page links to the same Grubhub ID.

Nothing here is a guess: a store with no published price contributes no
price, and searches that hit their budget say so. Run from GitHub Actions
(the build sandbox cannot reach these sites):
    python scripts/snapshot.py --out live-verification
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.adapters import extract_grubhub  # noqa: E402
from server.app import Service  # noqa: E402
from server.config import Config  # noqa: E402
from server.http_client import utcnow_iso  # noqa: E402

# Seeds are pages observed in earlier live runs (live-verification/report.json);
# searches without a seed use the platform's robots.txt-declared sitemaps.
SEARCHES = [
    {"label": "McDonald's 10 pc. Chicken McNuggets (Uber Eats, nationwide)",
     "request": {"order": {"chain": "McDonald's", "items": [{"name": "10 pc. Chicken McNuggets"}]},
                 "platforms": ["ubereats"], "seed_urls": ["https://www.ubereats.com/brand/mcdonalds"],
                 "max_stores_per_platform": 24}},
    {"label": "Lou Malnati's Pizzeria (Uber Eats, Chicago first)",
     "request": {"order": {"chain": "Lou Malnati's Pizzeria", "items": [{"name": "Small Deep Dish Margherita"}]},
                 "platforms": ["ubereats"], "max_stores_per_platform": 10,
                 "prioritize": {"state": "IL", "city": "Chicago"}}},
    {"label": "Taco Bell Crunchwrap Supreme (Uber Eats, nationwide)",
     "request": {"order": {"chain": "Taco Bell", "items": [{"name": "Crunchwrap Supreme"}]},
                 "platforms": ["ubereats"], "max_stores_per_platform": 12}},
]
# Grubhub listings whose URL was observed on a Grubhub-owned page (MenuPages /
# AllMenus links recorded in live-verification/report.json).
GRUBHUB_URLS = [
    "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510",
    "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712",
]
ITEM_KEYS = ("item_id", "name", "variant", "section", "price_cents", "currency", "availability", "addons",
             "required_options", "price_note")
OBS_KEYS = ("platform", "store_id", "store_name", "store_url", "fetched_url", "address", "observed_at", "order_mode",
            "mode_basis", "price_context", "currency", "extraction", "menu_item_count", "fetch_via", "duplicate_of")


def slim(obs: dict) -> dict:
    out = {k: obs.get(k) for k in OBS_KEYS if obs.get(k) is not None}
    out["items"] = [{k: it.get(k) for k in ITEM_KEYS if it.get(k) not in (None, [], "")} for it in obs.get("items", [])]
    return out


def run_search(svc: Service, spec: dict, fetch_mode: str) -> dict:
    req = dict(spec["request"], order_mode="delivery", fetch_mode=fetch_mode)
    entry = {"label": spec["label"], "request": {k: v for k, v in req.items() if k != "prioritize"},
             "prioritize": req.get("prioritize")}
    started = time.time()
    try:
        job = svc.jobs.start(req)
        deadline = time.time() + 300
        while time.time() < deadline and job.thread and job.thread.is_alive():
            time.sleep(1)
        if job.thread and job.thread.is_alive():
            job.cancel()
            job.thread.join(60)
        s = job.summary()
        entry["status"] = s["status"]
        entry["coverage"] = {p: v["counters"] for p, v in s["platforms"].items()}
        entry["platform_state"] = {p: {"state": v["state"], "stop_reason": v.get("stop_reason")} for p, v in s["platforms"].items()}
        entry["barriers"] = [e["barrier"] for e in job.state["events"] if e["type"] == "barrier"]
        entry["stores"] = [{k: st.get(k) for k in ("store_id", "canonical", "source", "status", "detail", "checked_at")}
                           for ps in job.state["platforms"].values() for st in ps["stores"].values()
                           if st.get("status") not in ("queued", "non_us")]
        entry["observations"] = [slim(o) for o in job.state["observations"]]
    except Exception as exc:  # recorded, never hidden
        entry["error"] = repr(exc)
    entry["seconds"] = round(time.time() - started, 1)
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="live-verification")
    args = ap.parse_args()
    cfg = Config.from_env()
    svc = Service(cfg)
    snap = {"generated_at": utcnow_iso(), "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_sha": os.environ.get("GITHUB_SHA"), "user_agent": cfg.user_agent,
            "price_basis": "Public menu prices as published on each store page, anonymous session, no delivery address; "
                           "before tax, fees, tip and promotions.",
            "searches": [], "grubhub_menus": []}
    for spec in SEARCHES:
        snap["searches"].append(run_search(svc, spec, "http"))
    for url in GRUBHUB_URLS:
        try:
            r = extract_grubhub(svc.client, url, cfg.platform_bases["grubhub"])
            snap["grubhub_menus"].append({"grubhub_url": url, "ok": bool(r.get("grubhub_menu")), "fields": r.get("fields"),
                                          "notes": r.get("notes"), "barriers": r.get("barriers"),
                                          "menu": r.get("grubhub_menu")})
        except Exception as exc:
            snap["grubhub_menus"].append({"grubhub_url": url, "ok": False, "error": repr(exc)})
    snap["finished_at"] = utcnow_iso()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshot.json").write_text(json.dumps(snap, indent=1, default=str))
    print(json.dumps({"searches": [{"label": s["label"], "status": s.get("status"), "coverage": s.get("coverage"),
                                    "observations": len(s.get("observations") or []), "barriers": len(s.get("barriers") or []),
                                    "error": s.get("error")} for s in snap["searches"]],
                      "grubhub_menus": [{"url": g["grubhub_url"], "ok": g["ok"],
                                         "items": len((g.get("menu") or {}).get("items") or [])} for g in snap["grubhub_menus"]]},
                     indent=1))


if __name__ == "__main__":
    main()

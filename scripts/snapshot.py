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
    # Live checks of engine rules on real pages (see README "Live checks"):
    # a two-item basket that must come from one branch, and the same item with
    # Uber Eats' pickup mode requested, never mixed with delivery prices.
    {"label": "Taco Bell basket: Crunchwrap Supreme + Chalupa Supreme (Uber Eats)",
     "request": {"order": {"chain": "Taco Bell", "items": [{"name": "Crunchwrap Supreme"}, {"name": "Chalupa Supreme"}]},
                 "platforms": ["ubereats"], "max_stores_per_platform": 8}},
    {"label": "Taco Bell Crunchwrap Supreme, pickup (Uber Eats)",
     "request": {"order": {"chain": "Taco Bell", "items": [{"name": "Crunchwrap Supreme"}]},
                 "platforms": ["ubereats"], "max_stores_per_platform": 8, "order_mode": "pickup"}},
    # More pickup evidence: the same stores as the delivery searches above, so
    # rank_snapshot.mjs can compare the two modes branch by branch.
    {"label": "McDonald's 10 pc. Chicken McNuggets, pickup (Uber Eats)",
     "request": {"order": {"chain": "McDonald's", "items": [{"name": "10 pc. Chicken McNuggets"}]},
                 "platforms": ["ubereats"], "seed_urls": ["https://www.ubereats.com/brand/mcdonalds"],
                 "max_stores_per_platform": 12, "order_mode": "pickup"}},
    {"label": "Lou Malnati's Pizzeria, pickup (Uber Eats, Chicago first)",
     "request": {"order": {"chain": "Lou Malnati's Pizzeria", "items": [{"name": "Small Deep Dish Margherita"}]},
                 "platforms": ["ubereats"], "max_stores_per_platform": 8, "order_mode": "pickup",
                 "prioritize": {"state": "IL", "city": "Chicago"}}},
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
    req = dict({"order_mode": "delivery"}, **spec["request"], fetch_mode=fetch_mode)
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


def check_cancel_resume(cfg: Config) -> dict:
    """Cancel a real search mid-run, then resume it from its checkpoint in a
    NEW Service (as after a server restart) and check nothing is refetched."""
    out = {"label": "Cancel and resume (Taco Bell Crunchwrap Supreme, Uber Eats)"}
    req = {"order": {"chain": "Taco Bell", "items": [{"name": "Crunchwrap Supreme"}]}, "platforms": ["ubereats"],
           "max_stores_per_platform": 20, "order_mode": "delivery", "fetch_mode": "http"}
    try:
        svc = Service(cfg)
        job = svc.jobs.start(req)
        deadline = time.time() + 240
        while time.time() < deadline and job.thread.is_alive():
            # Cancel as soon as the second store page has been handled (the
            # store table is updated under the job lock as each store finishes).
            with job.lock:
                handled = sum(1 for st in job.state["platforms"].get("ubereats", {}).get("stores", {}).values()
                              if st.get("status") not in ("queued", "in_progress", "non_us"))
            if handled >= 2:
                break
            time.sleep(0.1)
        job.cancel()
        job.thread.join(120)
        before = job.summary()
        done_before = {k: st.get("checked_at") for k, st in job.state["platforms"]["ubereats"]["stores"].items()
                       if st.get("status") not in ("queued", "non_us")}
        out["after_cancel"] = {"status": before["status"], "counters": before["platforms"]["ubereats"]["counters"]}
        svc2 = Service(cfg)                       # fresh manager: the job is loaded from its checkpoint file
        job2 = svc2.jobs.resume(job.id)
        deadline = time.time() + 300
        while time.time() < deadline and job2.thread.is_alive():
            time.sleep(1)
        after = job2.summary()
        stores = job2.state["platforms"]["ubereats"]["stores"]
        fetch_events = {}
        for e in job2.state["events"]:
            if e["type"] == "store" and e.get("store_key"):
                fetch_events[e["store_key"]] = fetch_events.get(e["store_key"], 0) + 1
        out["after_resume"] = {"status": after["status"], "counters": after["platforms"]["ubereats"]["counters"]}
        out["stores_done_before_cancel"] = len(done_before)
        out["refetched_after_resume"] = sorted(k for k, n in fetch_events.items() if n > 1)
        out["kept_checked_at"] = all(stores[k].get("checked_at") == t for k, t in done_before.items())
        out["new_stores_after_resume"] = sum(1 for k, st in stores.items()
                                             if k not in done_before and st.get("status") not in ("queued", "non_us"))
        out["observations"] = len(job2.state["observations"])
        out["cancelled_mid_run"] = before["status"] == "cancelled"
        out["passed"] = out["cancelled_mid_run"] and bool(done_before) and not out["refetched_after_resume"] \
            and out["kept_checked_at"] and out["new_stores_after_resume"] > 0
    except Exception as exc:  # recorded, never hidden
        out["error"] = repr(exc)
        out["passed"] = False
    return out


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
    snap["checks"] = {"cancel_resume": check_cancel_resume(cfg)}
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

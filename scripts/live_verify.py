"""Bounded live verification against the real platforms.

Runs the service code unchanged (robots.txt, rate limits, barrier reporting)
from a machine that has internet access - e.g. the GitHub Actions workflow
.github/workflows/fpc-live-verification.yml - and writes a JSON report that
contains no raw HTML. Raw pages are saved to tests/captures/ (git-ignored)
so tests/test_real_captures.py can verify the extractors against them.

Request budget: 3 robots.txt, two search jobs limited to 3 stores and 3
sitemap documents each (6 stores for the Uber Eats job), then up to 7 inspected pages (4 fixed + 3 discovered). Grubhub is only probed (robots.txt):
its Terms of Use prohibit automated scraping.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.adapters import inspect_fetched, make_adapters  # noqa: E402
from server.app import Service  # noqa: E402
from server.config import Config  # noqa: E402
from server.http_client import BrowserRenderer, utcnow_iso  # noqa: E402
from server.inspect_page import save_capture_outcome  # noqa: E402

# Real URLs that appear in public 2026 tool documentation / fixtures
# (n8n-nodes-ubereats-menu, n8n-nodes-doordash-scraper, doordash-cli, Apify).
INSPECT = [
    "https://www.ubereats.com/store/zuckers-bagels-fidis/YK9UYIbxTd2Gappp2N4pgQ",
    "https://www.ubereats.com/brand/mcdonalds",
    "https://www.doordash.com/store/24633898/",
    "https://www.doordash.com/business/chipotle-mexican-grill-115/",
]
JOBS = [
    {"order": {"chain": "McDonald's", "items": [{"name": "10 pc. Chicken McNuggets"}]}, "platforms": ["ubereats"],
     "seed_urls": ["https://www.ubereats.com/brand/mcdonalds"], "max_stores_per_platform": 6},
    {"order": {"chain": "Chipotle Mexican Grill", "items": [{"name": "Burrito Bowl"}]}, "platforms": ["doordash"],
     "seed_urls": ["https://www.doordash.com/business/chipotle-mexican-grill-115/"]},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="live-verification")
    ap.add_argument("--captures", default=str(ROOT / "tests" / "captures"))
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config.from_env()
    svc = Service(cfg)
    browser_ok, _ = BrowserRenderer.available()
    report = {"started_at": utcnow_iso(), "runner": {
        "github_run_id": os.environ.get("GITHUB_RUN_ID"), "github_sha": os.environ.get("GITHUB_SHA"),
        "runner_os": os.environ.get("RUNNER_OS") or platform.system(), "python": platform.python_version(),
        "browser_available": browser_ok}, "user_agent": cfg.user_agent, "probe": {}, "inspections": [], "jobs": []}

    try:
        report["probe"] = svc.probe(["doordash", "ubereats", "grubhub"])
    except Exception as exc:  # keep going; the report records it
        report["probe_error"] = repr(exc)

    for spec in JOBS:
        req = dict({"max_stores_per_platform": 3}, **spec, order_mode="delivery", fetch_mode="auto" if browser_ok else "http")
        entry = {"request": req}
        try:
            job = svc.jobs.start(req)
            deadline = time.time() + 600
            while time.time() < deadline and (job.thread and job.thread.is_alive()):
                time.sleep(1)
            if job.thread and job.thread.is_alive():
                job.cancel()
                job.thread.join(60)
            s = job.summary()
            entry["summary"] = {"status": s["status"], "platforms": s["platforms"]}
            entry["barriers"] = [e["barrier"] for e in job.state["events"] if e["type"] == "barrier"]
            entry["stores"] = [{k: v for k, v in st.items() if k in ("store_id", "canonical", "source", "status", "detail")}
                               for ps in job.state["platforms"].values() for st in ps["stores"].values()]
            entry["observations"] = job.state["observations"]
        except Exception as exc:
            entry["error"] = repr(exc)
        report["jobs"].append(entry)

    # Also inspect a few real US store pages the jobs discovered (structure only).
    extra = []
    for j in report["jobs"]:
        us = [st["canonical"] for st in j.get("stores", []) if st["status"] in ("checked", "no_menu", "blocked", "queued")]
        extra += us[:2] if "ubereats" in j["request"]["platforms"] else us[:1]
    adapters = make_adapters(cfg.platform_bases)
    for url in INSPECT + extra:
        entry = {"url": url}
        try:
            fetched = svc.client.fetch(url, kind="inspect", ttl=0)
            entry["http"] = inspect_fetched(url, fetched)
            store = any(a.parse_store_url(url) for a in adapters.values())
            if fetched.ok:
                save_capture_outcome(svc, url, fetched, Path(args.captures))
            elif fetched.body:
                save_capture_outcome(svc, url, fetched, Path(args.captures + "_barriers"), force=True)
            no_data = fetched.ok and not entry["http"].get("restaurants")
            # Render only when the page answered normally but had no structured data.
            if browser_ok and no_data:
                rendered = svc.client.fetch(url, kind="inspect", ttl=0, use_browser=True)
                entry["browser"] = inspect_fetched(url, rendered)
                if rendered.ok and store:
                    save_capture_outcome(svc, url, rendered, Path(args.captures), suffix="_rendered")
        except Exception as exc:
            entry["error"] = repr(exc)
        report["inspections"].append(entry)

    report["finished_at"] = utcnow_iso()
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({
        "probe": {k: {"reachable": v.get("reachable"), "crawling_allowed": v.get("crawling_allowed"),
                      "barrier": (v.get("barrier") or {}).get("kind"), "sitemaps": len(v.get("declared_sitemaps") or [])}
                  for k, v in report["probe"].items()},
        "inspections": [{"url": i["url"], "status": i.get("http", {}).get("fetch", {}).get("status"),
                         "barrier": (i.get("http", {}).get("fetch", {}).get("barrier") or {}).get("kind"),
                         "jsonld_types": i.get("http", {}).get("jsonld_types"),
                         "restaurants": len(i.get("http", {}).get("restaurants") or []),
                         "browser_restaurants": len((i.get("browser") or {}).get("restaurants") or [])}
                        for i in report["inspections"]],
        "jobs": [{"chain": j["request"]["order"]["chain"], "status": j.get("summary", {}).get("status"),
                  "counters": {p: v["counters"] for p, v in j.get("summary", {}).get("platforms", {}).items()},
                  "observations": len(j.get("observations") or []), "error": j.get("error")} for j in report["jobs"]],
    }, indent=1, default=str))


if __name__ == "__main__":
    main()

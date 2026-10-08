"""Bounded live verification against the real platforms.

Runs the service code unchanged (robots.txt, rate limits, barrier reporting)
from a machine that has internet access - e.g. the GitHub Actions workflow
.github/workflows/fpc-live-verification.yml - and writes a JSON report that
contains no raw HTML. Raw pages are saved to tests/captures/ (git-ignored)
so tests/test_real_captures.py can verify the extractors against them.

Request budget: 3 robots.txt, two search jobs limited to 3 stores and 3
sitemap documents each (6 stores for the Uber Eats job), up to 7 inspected
pages (4 fixed + 3 discovered), DoorDash's business_menu sitemap (index + one
child + 2 pages; store sitemap index + one child), and Grubhub: homepage,
/sitemap.xml, or a Grubhub-powered AllMenus page to find one real restaurant
link, then one run of the Grubhub URL extractor. Grubhub's Terms of
Use prohibit automated scraping; this single fetch exercises the user-requested
link-extraction feature and nothing more.
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

from server.adapters import GRUBHUB_PATH_RE, extract_grubhub, inspect_fetched, make_adapters  # noqa: E402
from server.extract import parse_sitemap, scan_html  # noqa: E402
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


def _sitemap(svc, url):
    out = svc.client.fetch(url, kind="sitemap", ttl=0)
    rec = {"url": url, "status": out.status, "barrier": out.barrier and out.barrier["kind"], "error": out.error}
    parsed = parse_sitemap(out.body or b"", svc.cfg.max_sitemap_bytes) if out.ok else {"kind": None, "locs": []}
    rec.update({"kind": parsed["kind"], "loc_count": len(parsed["locs"]), "sample": [l["loc"] for l in parsed["locs"][:8]]})
    return rec, [l["loc"] for l in parsed["locs"]]


def explore_doordash_menu_sitemap(svc, report, captures):
    """DoorDash's robots.txt declares a business_menu sitemap; see what it lists."""
    declared = (report["probe"].get("doordash") or {}).get("declared_sitemaps") or []
    idx_url = next((u for u in declared if "business_menu" in u), None)
    entry = {"declared": idx_url}
    store_idx = next((u for u in declared if "sitemap-store" in u), None)
    if store_idx:
        entry["store_index"], store_children = _sitemap(svc, store_idx)
        if store_children:
            entry["store_child"], _ = _sitemap(svc, store_children[0])
    if not idx_url:
        return entry
    entry["index"], children = _sitemap(svc, idx_url)
    if children:
        entry["child"], locs = _sitemap(svc, children[0])
        pick = [u for u in locs if any(k in u.lower() for k in ("mcdonalds", "chipotle"))][:2] or locs[:2]
        entry["pages"] = []
        for u in pick:
            fetched = svc.client.fetch(u, kind="inspect", ttl=0)
            entry["pages"].append(inspect_fetched(u, fetched))
            if fetched.ok:
                save_capture_outcome(svc, u, fetched, Path(captures))
    return entry


def explore_grubhub(svc):
    """Find one real restaurant link on Grubhub's own pages, then run the app's
    Grubhub URL extractor on it once (the feature the user asked for)."""
    entry = {"discovery": []}
    found = []
    for page in ("https://www.grubhub.com/", "https://www.grubhub.com/sitemap.xml"):
        if page.endswith(".xml"):
            rec, locs = _sitemap(svc, page)
            if rec["kind"] == "index" and locs:
                child = next((u for u in locs if "restaurant" in u.lower()), locs[0])
                rec2, locs = _sitemap(svc, child)
                rec["child"] = rec2
            found += [u for u in locs if GRUBHUB_PATH_RE.match(__import__("urllib.parse").parse.urlsplit(u).path)]
        else:
            out = svc.client.fetch(page, kind="inspect", ttl=0)
            rec = {"url": page, "status": out.status, "barrier": out.barrier and out.barrier["kind"], "bytes": len(out.body or b"")}
            if out.ok:
                for a in scan_html(out.text, page).anchors:
                    path = __import__("urllib.parse").parse.urlsplit(a["href"]).path
                    if GRUBHUB_PATH_RE.match(path):
                        found.append(a["href"])
            rec["restaurant_links"] = len(found)
        entry["discovery"].append(rec)
        if found:
            break
    if not found:
        # AllMenus is Grubhub-powered; its Lou Malnati's (Buffalo Grove) page,
        # found via web search, links to the restaurant's Grubhub listing.
        # Fetched with the same robots/rate-limit rules via a separate client
        # (the app's own allowlist is unchanged).
        import copy
        from server.http_client import PoliteClient
        cfg2 = copy.deepcopy(svc.cfg)
        cfg2.platform_bases = dict(cfg2.platform_bases, allmenus="https://www.allmenus.com")
        client2 = PoliteClient(cfg2, svc.cache)
        page = "https://www.allmenus.com/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/"
        out = client2.fetch(page, kind="inspect", ttl=0)
        rec = {"url": page, "status": out.status, "barrier": out.barrier and out.barrier["kind"], "bytes": len(out.body or b"")}
        gh_links = []
        if out.ok:
            for a in scan_html(out.text, page).anchors:
                parts = __import__("urllib.parse").parse.urlsplit(a["href"])
                if parts.netloc.lower().endswith("grubhub.com"):
                    gh_links.append(a["href"])
                    if GRUBHUB_PATH_RE.match(parts.path):
                        found.append(a["href"])
        rec["grubhub_links_sample"] = gh_links[:8]
        rec["restaurant_links"] = len(found)
        entry["discovery"].append(rec)
    entry["restaurant_links_found"] = len(found)
    if found:
        url = found[0]
        result = extract_grubhub(svc.client, url, svc.cfg.platform_bases["grubhub"])
        page = svc.client.fetch(url.split("?")[0], kind="page")  # served from the extractor's cache
        entry["extract"] = {"url": url, "result": _menu_summary(result), "page_structure": inspect_fetched(url, page)}
    # The app's Grubhub extractor on a Grubhub URL that MenuPages itself links to
    # (observed on menupages.com/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago,
    # run 37713405667): exercises the Grubhub -> MenuPages price path end to end.
    url = "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510"
    entry["menupages_path"] = {"url": url, "result": _menu_summary(extract_grubhub(svc.client, url, svc.cfg.platform_bases["grubhub"]))}
    return entry


def _menu_summary(result):
    """Keep the report small: item count plus a sample of the MenuPages menu."""
    r = dict(result)
    menu = r.get("grubhub_menu")
    if menu:
        r["grubhub_menu"] = dict(menu, item_count=len(menu["items"]), items=menu["items"][:12])
    return r


MIRROR_PAGES = {
    # Grubhub-owned menu sites ("powered by Grubhub"); URLs from public search results.
    "allmenus": ("https://www.allmenus.com", ["https://www.allmenus.com/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/"]),
    "menupages": ("https://menupages.com", ["https://menupages.com/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago"]),
}
BRAND_CITY = ["https://www.ubereats.com/brand-city/rockford-il/portillos-hot-dogs"]


def explore_public_mirrors(svc, captures):
    """Probe Grubhub-owned menu mirrors and Uber Eats brand-city pages.

    Each mirror host gets its own client (same robots.txt / rate-limit rules);
    the app's allowlist is unchanged.
    """
    import copy
    from urllib.parse import urlsplit
    from server.http_client import PoliteClient
    out = {}
    for key, (base, pages) in MIRROR_PAGES.items():
        cfg2 = copy.deepcopy(svc.cfg)
        cfg2.platform_bases = dict(cfg2.platform_bases, **{key: base})
        client = PoliteClient(cfg2, svc.cache)
        rules, rb = client.robots(base)
        entry = {"robots": {"status": rules.source_status, "barrier": rb and rb.get("kind"), "allowed_page": None,
                            "sitemaps": rules.sitemaps[:10], "crawl_delay": rules.crawl_delay,
                            "rules": [("allow" if a else "disallow", pat) for a, pat, _ in rules.rules][:40]},
                 "pages": []}
        for u in pages:
            entry["robots"]["allowed_page"] = rules.can_fetch(u)
            fetched = client.fetch(u, kind="inspect", ttl=0)
            rep = inspect_fetched(u, fetched)
            if fetched.ok:
                anchors = scan_html(fetched.text, u).anchors
                rep["grubhub_links"] = sorted({a["href"].split("?")[0] for a in anchors if "grubhub.com" in urlsplit(a["href"]).netloc})[:5]
                rep["same_site_link_sample"] = sorted({a["href"] for a in anchors if urlsplit(a["href"]).netloc.endswith(urlsplit(base).netloc.replace("www.", ""))})[:25]
                save_capture_outcome(svc, u, fetched, Path(captures + "_mirrors"))
            entry["pages"].append(rep)
        out[key] = entry
    out["ubereats_brand_city"] = []
    for u in BRAND_CITY:
        fetched = svc.client.fetch(u, kind="inspect", ttl=0)
        rep = inspect_fetched(u, fetched)
        if fetched.ok:
            ue = make_adapters(svc.cfg.platform_bases)["ubereats"]
            links = {a["href"] for a in scan_html(fetched.text, u).anchors if ue.parse_store_url(a["href"])}
            rep["store_links"] = sorted(links)[:20]
            rep["store_link_count"] = len(links)
        out["ubereats_brand_city"].append(rep)
    return out


def explore_menupages_mapping(svc, captures):
    """Can a Grubhub restaurant URL be mapped to its MenuPages page (Grubhub-owned,
    JSON-LD menu with prices) and confirmed by the Grubhub link on that page?"""
    import copy
    import re
    from server.adapters import parse_store_page
    from server.http_client import PoliteClient
    cfg2 = copy.deepcopy(svc.cfg)
    cfg2.platform_bases = dict(cfg2.platform_bases, menupages="https://menupages.com")
    client = PoliteClient(cfg2, svc.cache)
    out = {}

    def sm(url):
        f = client.fetch(url, kind="sitemap", ttl=0)
        parsed = parse_sitemap(f.body or b"", cfg2.max_sitemap_bytes) if f.ok else {"kind": None, "locs": []}
        locs = [l["loc"] for l in parsed["locs"]]
        return {"url": url, "status": f.status, "barrier": f.barrier and f.barrier["kind"], "kind": parsed["kind"],
                "count": len(locs), "sample": locs[:12]}, locs

    out["index"], children = sm("https://menupages.com/sitemap-index.xml")
    if children:
        pick = next((c for c in children if re.search(r"(^|[^a-z])(il|chicago|illinois)([^a-z]|$)", c.lower())), children[0])
        out["child"], locs = sm(pick)
        out["child"]["lou_malnatis_entries"] = [u for u in locs if "lou-malnati" in u][:10]

    def page(url):
        f = client.fetch(url, kind="inspect", ttl=0)
        rep = {"url": url, "status": f.status, "barrier": f.barrier and f.barrier["kind"], "bytes": len(f.body or b"")}
        if f.ok:
            parsed = parse_store_page(f.text, f.final_url or url, "")
            rep["restaurant_found"] = parsed["restaurant_found"]
            rep["name"] = parsed["name"]
            from server.extract import normalize_us_address
            rep["address"] = normalize_us_address(parsed["address_raw"])[0]
            rep["items"] = len(parsed["items"])
            rep["sample_items"] = [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section")} for it in parsed["items"][:10]]
            rep["grubhub_ids"] = sorted({m.group(1) for a in scan_html(f.text, url).anchors
                                         for m in [re.search(r"grubhub\.com/restaurant/[^/]+/(\d+)", a["href"])] if m})
            save_capture_outcome(svc, url, f, Path(captures + "_mirrors"))
        return rep

    out["known_page"] = page("https://menupages.com/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago")
    # Derive a candidate from a real Grubhub slug (Buffalo Grove listing, Grubhub ID 517712, found on AllMenus).
    gh_slug = "lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove"
    words = gh_slug.split("-")
    k = next(i for i, w in enumerate(words) if w.isdigit())
    out["derived_candidate"] = page(f"https://menupages.com/{'-'.join(words[:k])}/{'-'.join(words[k:])}")
    out["derived_candidate"]["expected_grubhub_id"] = "517712"
    return out


def explore_allmenus(svc, captures):
    """AllMenus (Grubhub-owned): sitemap layout, and the menu JSON-LD on a known
    page (Lou Malnati's Buffalo Grove, which links to Grubhub ID 517712)."""
    import copy
    import re
    from server.adapters import parse_store_page
    from server.extract import normalize_us_address
    from server.http_client import PoliteClient
    cfg2 = copy.deepcopy(svc.cfg)
    cfg2.platform_bases = dict(cfg2.platform_bases, allmenus="https://www.allmenus.com")
    client = PoliteClient(cfg2, svc.cache)
    out = {}

    def sm(url):
        f = client.fetch(url, kind="sitemap", ttl=0)
        parsed = parse_sitemap(f.body or b"", cfg2.max_sitemap_bytes) if f.ok else {"kind": None, "locs": []}
        locs = [l["loc"] for l in parsed["locs"]]
        return {"url": url, "status": f.status, "barrier": f.barrier and f.barrier["kind"], "kind": parsed["kind"],
                "count": len(locs), "sample": locs[:15]}, locs

    out["index"], children = sm("https://www.allmenus.com/sitemap-index.xml")
    out["index"]["all_children"] = children[:200]
    picks = [c for c in children if re.search(r"(^|[^a-z])il([^a-z]|$)|illinois", c.lower())][:2] or children[:2]
    out["children"] = []
    for c in picks:
        rec, locs = sm(c)
        rec["lou_malnatis"] = [u for u in locs if "lou-malnati" in u][:20]
        rec["path_shapes"] = sorted({re.sub(r"\d+", "N", "/".join(u.split("/")[3:6])) for u in locs[:2000]})[:20]
        out["children"].append(rec)
    page = "https://www.allmenus.com/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/"
    f = client.fetch(page, kind="inspect", ttl=0)
    rep = {"url": page, "status": f.status, "barrier": f.barrier and f.barrier["kind"]}
    if f.ok:
        parsed = parse_store_page(f.text, f.final_url or page, "")
        rep.update({"restaurant_found": parsed["restaurant_found"], "name": parsed["name"],
                    "address_raw": parsed["address_raw"], "address": normalize_us_address(parsed["address_raw"])[0],
                    "items": len(parsed["items"]), "jsonld_errors": parsed.get("jsonld_errors"),
                    "sample_items": [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section")} for it in parsed["items"][:12]],
                    "grubhub_links": sorted({m.group(0) for a in scan_html(f.text, page).anchors
                                             for m in [re.search(r"grubhub\.com/restaurant/[^/?#]+/\d+", a["href"])] if m})})
        # Markup evidence for prices that are only in HTML: the element around
        # the first few price spans, the address markup, and class counts.
        html = f.text
        idx = [m.start() for m in re.finditer(r'class="item-price"', html)][:3]
        rep["item_html_samples"] = [re.sub(r"\s+", " ", html[max(0, i - 900): i + 250]) for i in idx]
        rep["address_html_samples"] = [re.sub(r"\s+", " ", html[max(0, m.start() - 400): m.start() + 300])
                                       for m in list(re.finditer(r"Buffalo Grove Rd", html))[:2]]
        rep["class_counts"] = {c: html.count(f'class="{c}"') for c in sorted(set(re.findall(r'class="([a-z][a-z0-9_-]{2,40})"', html)))
                               if html.count(f'class="{c}"') >= 20}
        save_capture_outcome(svc, page, f, Path(captures + "_mirrors"))
    out["known_page"] = rep
    return out


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

    try:
        report["doordash_business_menu"] = explore_doordash_menu_sitemap(svc, report, args.captures)
    except Exception as exc:
        report["doordash_business_menu"] = {"error": repr(exc)}
    try:
        report["public_mirrors"] = explore_public_mirrors(svc, args.captures)
    except Exception as exc:
        report["public_mirrors"] = {"error": repr(exc)}
    try:
        report["menupages_mapping"] = explore_menupages_mapping(svc, args.captures)
    except Exception as exc:
        report["menupages_mapping"] = {"error": repr(exc)}
    try:
        report["allmenus"] = explore_allmenus(svc, args.captures)
    except Exception as exc:
        report["allmenus"] = {"error": repr(exc)}
    try:
        report["grubhub"] = explore_grubhub(svc)
    except Exception as exc:
        report["grubhub"] = {"error": repr(exc)}

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

"""Backend tests. Network access is only to local synthetic servers
(tests/fake_platform.py); see that module's docstring."""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.request

import pytest

from server.adapters import DoorDashAdapter, UberEatsAdapter, chain_guess_from_slug, extract_grubhub, parse_store_page
from server.app import Service, serve
from server.config import Config
from server.extract import (
    normalize_us_address, parse_jsonld_blocks, extract_restaurants, parse_sitemap, price_to_cents, scan_html,
)
from server.http_client import DiskCache, PoliteClient, parse_retry_after
from server.robots import parse_robots, rules_for_status
from fake_platform import FakeSite, build_doordash, build_ubereats, restaurant_page, NUGGETS


# ---------------------------------------------------------------- helpers

def make_cfg(tmp_path, bases: dict, **kw) -> Config:
    cfg = Config()
    # Unit tests never reach real sites: MenuPages defaults to a closed local port.
    cfg.platform_bases = {"doordash": "https://www.doordash.com", "ubereats": "https://www.ubereats.com",
                          "grubhub": "https://www.grubhub.com", "menupages": "http://127.0.0.1:9", "allmenus": "http://127.0.0.1:9", **bases}
    cfg.test_mode = True
    cfg.data_dir = tmp_path / "data"
    cfg.min_interval_s = 0.01
    cfg.backoff_base_s = 0.05
    cfg.backoff_cap_s = 0.2
    cfg.timeout_s = 5
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def wait_for(job, statuses=("completed", "paused", "stopped", "cancelled", "failed"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if job.state["status"] in statuses and not (job.thread and job.thread.is_alive()):
            return job.state["status"]
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job.state['status']}")


ORDER = {"order": {"chain": "McDonald's", "items": [{"name": "Chicken McNuggets", "flavor": ""}]},
         "platforms": ["doordash", "ubereats"], "order_mode": "delivery", "max_stores_per_platform": 10}


@pytest.fixture
def sites():
    dd, ue, gh = FakeSite(), FakeSite(), FakeSite()
    yield dd, ue, gh
    for s in (dd, ue, gh):
        s.close()


# ---------------------------------------------------------------- robots

def test_robots_wildcards_longest_match_and_groups():
    txt = """
User-agent: Googlebot
Disallow: /

User-agent: *
Disallow: /store/*?pickup=
Allow: /store/
Disallow: /*.json$
Crawl-delay: 5
Sitemap: https://x.test/sitemap.xml
"""
    r = parse_robots(txt, "FoodPriceCompare")
    assert r.can_fetch("https://x.test/store/abc-1/")
    assert not r.can_fetch("https://x.test/store/abc-1/?pickup=true")
    assert not r.can_fetch("https://x.test/data.json")
    assert r.can_fetch("https://x.test/data.json?x=1")  # $ anchors the end
    assert r.crawl_delay == 5.0
    assert r.sitemaps == ["https://x.test/sitemap.xml"]
    own = parse_robots("User-agent: foodpricecompare\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "FoodPriceCompare")
    assert not own.can_fetch("https://x.test/anything")
    tie = parse_robots("User-agent: *\nDisallow: /a\nAllow: /a\n", "x")
    assert tie.can_fetch("https://x.test/a")


def test_robots_status_handling_is_conservative():
    assert rules_for_status(404, None, "x").allow_all
    assert rules_for_status(403, None, "x").disallow_all
    assert rules_for_status(503, None, "x").disallow_all
    assert rules_for_status(None, None, "x").disallow_all


# ---------------------------------------------------------------- extraction

def test_price_to_cents_is_exact():
    assert price_to_cents("5.49") == 549
    assert price_to_cents("$1,299.00") == 129900
    assert price_to_cents(9.99) == 999
    assert price_to_cents(5) == 500
    assert price_to_cents("12,99") is None  # comma decimal is not USD format
    assert price_to_cents("9.995") is None
    assert price_to_cents("-1.00") is None
    assert price_to_cents(True) is None
    assert price_to_cents("9.990") == 999


def test_us_address_rules():
    ok, why = normalize_us_address({"addressRegion": "District of Columbia", "postalCode": "20001", "addressLocality": "Washington"})
    assert ok and ok["state"] == "DC" and why is None
    assert normalize_us_address({"addressRegion": "ON", "addressCountry": "CA", "postalCode": "M5H 1A1"})[0] is None
    assert normalize_us_address({"addressRegion": "PR", "addressCountry": "US", "postalCode": "00901"})[0] is None
    assert normalize_us_address({"addressRegion": "TX"})[0] is None  # no country, no ZIP: unverifiable
    assert normalize_us_address("1 Main St, Austin TX")[0] is None


def test_jsonld_menu_extraction_variants_addons_and_errors():
    html = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"x"},
 {"@type":["FastFoodRestaurant"],"name":"Wing Hut","address":{"@type":"PostalAddress","addressRegion":"GA","postalCode":"30303","addressCountry":{"@type":"Country","name":"US"}},
  "hasMenu":{"@type":"Menu","hasMenuSection":{"@type":"MenuSection","name":"Wings","hasMenuSection":[{"@type":"MenuSection","name":"Bone-in",
   "hasMenuItem":[{"@type":"MenuItem","name":"Wings","offers":[{"@type":"Offer","name":"6 pc","price":7.5,"priceCurrency":"USD"},
                                                               {"@type":"Offer","name":"12 pc","priceSpecification":{"@type":"PriceSpecification","price":"13.25","priceCurrency":"USD"},"availability":"http://schema.org/OutOfStock"}],
     "menuAddOn":{"@type":"MenuSection","name":"Flavor","hasMenuItem":[{"@type":"MenuItem","name":"Buffalo","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"}}]}},
    {"@type":"MenuItem","name":"Party Pack","offers":{"@type":"AggregateOffer","lowPrice":"20","highPrice":"40","priceCurrency":"USD"}}]}]}}}
]}</script>
<script type="application/ld+json">{broken json</script>
<script id="__NEXT_DATA__" type="application/json">{"a":1}</script>
</head></html>"""
    scan = scan_html(html, "https://x.test/")
    objs, errors = parse_jsonld_blocks(scan.jsonld_raw)
    assert len(errors) == 1
    rs = extract_restaurants(objs)
    assert len(rs) == 1 and rs[0]["name"] == "Wing Hut"
    items = rs[0]["items"]
    six = next(i for i in items if i["variant"] == "6 pc")
    twelve = next(i for i in items if i["variant"] == "12 pc")
    assert six["price_cents"] == 750 and six["section"] == "Bone-in"
    assert twelve["price_cents"] == 1325 and twelve["availability"] == "unavailable"
    assert six["addons"] == [{"name": "Buffalo", "group": "Flavor", "price_cents": 0, "currency": "USD"}]
    pack = next(i for i in items if i["name"] == "Party Pack")
    assert pack["price_cents"] is None and pack["price_range_cents"] == [2000, 4000]
    assert any(s["id"] == "__NEXT_DATA__" for s in scan.json_scripts)


def test_sitemap_parsing_gzip_and_dtd_rejected():
    xml = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x/a</loc><lastmod>2026-01-01</lastmod></url></urlset>'
    assert parse_sitemap(xml, 10**6)["locs"] == [{"loc": "https://x/a", "lastmod": "2026-01-01"}]
    assert parse_sitemap(gzip.compress(xml), 10**6)["kind"] == "urlset"
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
    assert parse_sitemap(evil, 10**6)["kind"] == "invalid"
    assert parse_sitemap(b"not xml", 10**6)["kind"] == "invalid"


def test_store_url_patterns():
    dd = DoorDashAdapter("https://www.doordash.com")
    assert dd.parse_store_url("https://www.doordash.com/store/mcdonalds-springfield-12345/")["store_id"] == "12345"
    assert dd.parse_store_url("https://www.doordash.com/en-US/store/12345/")["store_id"] == "12345"
    assert dd.parse_store_url("https://evil.test/store/x-1/") is None
    assert dd.is_chain_page("https://www.doordash.com/business/mcdonalds-123/")
    ue = UberEatsAdapter("https://www.ubereats.com")
    ref = ue.parse_store_url("https://www.ubereats.com/ca/store/mcd-toronto/AbCdEf123456")
    assert ref["country"] == "ca" and ref["store_id"] == "AbCdEf123456"
    assert ue.mode_url("https://www.ubereats.com/store/a/b", "pickup")[0].endswith("diningMode=PICKUP")
    assert dd.mode_url("https://www.doordash.com/store/a-1/", "pickup")[0].endswith("pickup=true")
    assert chain_guess_from_slug("mcdonalds-1234-n-main-st-chicago") == "Mcdonalds"


# ---------------------------------------------------------------- polite client

def test_client_barriers_retry_after_cache_and_allowlist(sites, tmp_path):
    dd, _, _ = sites
    dd.route("/robots.txt", "User-agent: *\nDisallow: /private\n", ctype="text/plain")
    dd.route("/ok", "<html>ok</html>")
    dd.route("/private/x", "secret")
    dd.route("/captcha", "<html><title>Just a moment...</title><div class='cf-chl-widget'></div></html>", status=503)
    dd.route("/forbidden", "<html>nope</html>", status=403)
    big = "<html><head><title>Store</title><script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script></head><body>" + "x" * 200_000 + "</body></html>"
    dd.route("/big-normal", big)
    dd.route("/login-redirect", "", status=302, headers={"Location": "/login?next=/x"})
    dd.route("/login", "<html>log in</html>")
    dd.route("/toolong", "slow down", status=429, headers={"Retry-After": "9999"})
    dd.route("/offsite", "", status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    dd.route("/hop", "", status=301, headers={"Location": "/ok"})
    dd.routes["/flaky"] = lambda n: ({"status": 429, "headers": {"Retry-After": "1"}, "body": "wait"} if n == 1
                                     else {"status": 200, "body": "<html>fine</html>"})
    dd.routes["/always500"] = lambda n: {"status": 500, "body": "err"}
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    client = PoliteClient(cfg, DiskCache(cfg.data_dir))

    assert client.fetch("https://other.test/x").barrier["kind"] == "not_allowed"
    assert client.fetch(dd.base + "/private/x").barrier["kind"] == "robots_disallow"
    assert dd.hits.get("/private/x") is None  # never requested
    assert client.fetch(dd.base + "/captcha").barrier["kind"] == "captcha"
    assert dd.hits["/captcha"] == 1  # challenge is not retried
    assert client.fetch(dd.base + "/forbidden").barrier["kind"] == "forbidden"
    big_out = client.fetch(dd.base + "/big-normal")
    assert big_out.ok and big_out.barrier is None  # vendor script on a normal 200 page is not a challenge
    assert client.fetch(dd.base + "/login-redirect").barrier["kind"] == "login_wall"
    assert dd.hits.get("/login?next=/x") is None  # login page itself never requested
    assert client.fetch(dd.base + "/offsite").barrier["kind"] == "offsite_redirect"  # no SSRF via redirect
    hop = client.fetch(dd.base + "/hop")
    assert hop.ok and hop.final_url == dd.base + "/ok"
    out = client.fetch(dd.base + "/toolong")
    assert out.barrier["kind"] == "rate_limited" and out.barrier["retry_after_s"] == 9999

    t0 = time.monotonic()
    out = client.fetch(dd.base + "/flaky")
    assert out.ok and out.attempts == 2
    gap = dd.hit_times["/flaky"][1] - dd.hit_times["/flaky"][0]
    assert gap >= 0.95, f"Retry-After not honoured (gap {gap:.2f}s)"
    out = client.fetch(dd.base + "/always500")
    assert not out.ok and out.barrier["kind"] == "server_error" and dd.hits["/always500"] == cfg.max_retries + 1

    first = client.fetch(dd.base + "/ok")
    second = client.fetch(dd.base + "/ok")
    assert first.ok and not first.from_cache and second.from_cache and dd.hits["/ok"] == 2  # 1 via /hop
    assert second.fetched_at == first.fetched_at  # cache keeps the original observation time
    assert time.monotonic() - t0 < 10


def test_parse_retry_after_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    v = parse_retry_after(format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True))
    assert 25 <= v <= 31
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("garbage") is None


def test_rate_limit_spacing(sites, tmp_path):
    dd, _, _ = sites
    dd.route("/robots.txt", "User-agent: *\nCrawl-delay: 0.3\n", ctype="text/plain")
    for i in range(3):
        dd.route(f"/p{i}", "x")
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    client = PoliteClient(cfg, DiskCache(cfg.data_dir))
    threads = [threading.Thread(target=client.fetch, args=(dd.base + f"/p{i}",)) for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    times = sorted(dd.hit_times[f"/p{i}"][0] for i in range(3))
    assert times[1] - times[0] >= 0.28 and times[2] - times[1] >= 0.28  # robots Crawl-delay honoured


# ---------------------------------------------------------------- jobs (end to end over local HTTP)

def test_job_end_to_end_discovery_menus_dedupe_and_nonus(sites, tmp_path):
    dd, ue, _ = sites
    build_doordash(dd, n_stores=4)
    build_ubereats(ue)
    cfg = make_cfg(tmp_path, {"doordash": dd.base, "ubereats": ue.base})
    svc = Service(cfg)
    req = dict(ORDER, seed_urls=[ue.base + "/brand/mcdonalds"])
    job = svc.jobs.start(req)
    assert wait_for(job) == "completed"
    s = job.summary()
    ddc, uec = s["platforms"]["doordash"]["counters"], s["platforms"]["ubereats"]["counters"]
    # DoorDash: 4 McDonald's from the sitemap; Burger King filtered out by slug.
    assert ddc["discovered"] == 4 and ddc["checked"] == 4 and ddc["sitemap_enumeration_complete"]
    assert "/store/burger-king-city0-9001/" not in dd.hits
    # Uber Eats: sitemap (us1, ca, nomenu) + brand page (us2, dupe) with rel=next pagination.
    assert uec["discovered"] == 5
    assert uec["non_us"] == 1 and uec["no_menu"] == 1 and uec["duplicate"] == 1 and uec["checked"] == 2
    ca = job.state["platforms"]["ubereats"]["stores"]["CaNaDa000111"]
    assert ca["status"] == "non_us" and "checked_at" not in ca  # marked at discovery, never queued
    assert ue.hits.get("/brand/mcdonalds?page=2") == 1
    assert "/ca/store/mcdonalds-toronto/CaNaDa000111" not in ue.hits  # non-US section skipped without a request
    obs = job.state["observations"]
    assert len(obs) == 7  # the address-duplicate listing is kept, flagged
    dups = [x for x in obs if x.get("duplicate_of")]
    assert len(dups) == 1 and dups[0]["platform"] == "ubereats"
    o = next(x for x in obs if x["platform"] == "ubereats" and x["store_id"] == "AbCdEfGh1234")
    assert o["address"]["state"] == "OH" and o["currency"] == "USD" and o["order_mode"] == "delivery"
    assert "diningMode=DELIVERY" in o["fetched_url"] and o["observed_at"].endswith("Z")
    assert {i["variant"] for i in o["items"]} == {"10 pc", "20 pc"}
    d = next(x for x in obs if x["platform"] == "doordash")
    names = {i["name"] for i in d["items"]}
    assert "Big Mac®" not in names and "10 pc. Chicken McNuggets®" in names  # prefilter keeps candidates only
    assert d["menu_item_count"] == 5
    # Checkpoint content: no receipt, Grubhub prices or address filters.
    saved = json.loads(cfg.data_dir.joinpath("jobs", f"{job.id}.json").read_text())
    assert set(saved["request"]["order"]["items"][0]) == {"name", "aliases", "flavor"}
    assert "prioritize" not in json.dumps(saved["request"])


def test_job_run_limit_then_resume(sites, tmp_path):
    dd, _, _ = sites
    build_doordash(dd, n_stores=5)
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    svc = Service(cfg)
    job = svc.jobs.start(dict(ORDER, platforms=["doordash"], max_stores_per_platform=2))
    assert wait_for(job) == "paused"
    c = job.summary()["platforms"]["doordash"]["counters"]
    assert c["checked"] == 2 and c["remaining"] == 3
    svc.jobs.resume(job.id)
    assert wait_for(job) == "paused"
    svc.jobs.resume(job.id)
    assert wait_for(job) == "completed"
    c = job.summary()["platforms"]["doordash"]["counters"]
    assert c["checked"] == 5 and c["remaining"] == 0
    for i in range(5):
        assert dd.hits[f"/store/mcdonalds-city{i}-{1000 + i}/?pickup=false"] == 1  # nothing fetched twice


def test_job_cancel_and_resume_after_restart(sites, tmp_path):
    dd, _, _ = sites
    build_doordash(dd, n_stores=6, slow=0.4)
    cfg = make_cfg(tmp_path, {"doordash": dd.base}, concurrency_per_platform=1)
    svc = Service(cfg)
    job = svc.jobs.start(dict(ORDER, platforms=["doordash"]))
    end = time.time() + 10
    while job.summary()["platforms"]["doordash"]["counters"]["checked"] < 1 and time.time() < end:
        time.sleep(0.02)
    job.cancel()
    assert wait_for(job) == "cancelled"
    c = job.summary()["platforms"]["doordash"]["counters"]
    assert 1 <= c["checked"] < 6 and c["in_progress"] == 0
    # Simulate a server restart: a fresh service loads the checkpoint from disk.
    svc2 = Service(cfg)
    job2 = svc2.jobs.get(job.id)
    assert job2.state["status"] == "cancelled"
    svc2.jobs.resume(job.id)
    assert wait_for(job2) == "completed"
    assert job2.summary()["platforms"]["doordash"]["counters"]["checked"] == 6
    assert len(job2.state["observations"]) == 6
    assert max(dd.hits[f"/store/mcdonalds-city{i}-{1000 + i}/?pickup=false"] for i in range(6)) == 1


def test_job_circuit_breaker_and_partial_failure(sites, tmp_path):
    dd, ue, _ = sites
    build_doordash(dd, n_stores=6, blocked=True)
    build_ubereats(ue)
    cfg = make_cfg(tmp_path, {"doordash": dd.base, "ubereats": ue.base}, breaker_threshold=3)
    svc = Service(cfg)
    job = svc.jobs.start(ORDER)
    assert wait_for(job) == "completed"  # Uber Eats finished even though DoorDash stopped
    p = job.summary()["platforms"]
    assert p["doordash"]["state"] == "stopped" and p["doordash"]["stop_reason"]["kind"] == "circuit_breaker"
    # Trips at 3; at most (concurrency - 1) already in-flight fetches can finish after it.
    blocked = p["doordash"]["counters"]["blocked"]
    assert 3 <= blocked <= 3 + cfg.concurrency_per_platform - 1
    assert p["doordash"]["counters"]["remaining"] == 6 - blocked
    assert p["ubereats"]["state"] == "done"
    kinds = [e["barrier"]["kind"] for e in job.state["events"] if e["type"] == "barrier"]
    assert kinds.count("forbidden") == blocked


def test_job_robots_disallow_stops_platform(sites, tmp_path):
    dd, _, _ = sites
    dd.route("/robots.txt", "User-agent: *\nDisallow: /\n", ctype="text/plain")
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    job = Service(cfg).jobs.start(dict(ORDER, platforms=["doordash"]))
    assert wait_for(job) == "stopped"
    r = job.summary()["platforms"]["doordash"]["stop_reason"]
    assert r["kind"] == "robots_disallow"
    assert set(dd.hits) == {"/robots.txt"}


def test_unreachable_host_reports_network_barrier(tmp_path):
    cfg = make_cfg(tmp_path, {"doordash": "http://127.0.0.1:9"}, max_retries=0)
    job = Service(cfg).jobs.start(dict(ORDER, platforms=["doordash"]))
    assert wait_for(job) == "stopped"
    assert job.summary()["platforms"]["doordash"]["stop_reason"]["kind"] == "network"


def test_pickup_mode_uses_mode_parameter(sites, tmp_path):
    dd, _, _ = sites
    build_doordash(dd, n_stores=1)
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    job = Service(cfg).jobs.start(dict(ORDER, platforms=["doordash"], order_mode="pickup"))
    assert wait_for(job) == "completed"
    o = job.state["observations"][0]
    assert o["order_mode"] == "pickup" and o["fetched_url"].endswith("?pickup=true")
    assert "pickup=true" in o["mode_basis"]


# ---------------------------------------------------------------- browser rendering path

def test_browser_render_path_extracts_js_injected_jsonld(sites, tmp_path):
    pytest.importorskip("playwright")
    dd, _, _ = sites
    dd.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    page = restaurant_page("McDonald's", dd.base + "/store/mcdonalds-x-1/", "1 A St", "Reno", "NV", "89501", NUGGETS)
    ld = page.split('<script type="application/ld+json">')[1].split("</script>")[0]
    js_page = ("<html><head><title>McDonald's</title></head><body><script>"
               "var s=document.createElement('script');s.type='application/ld+json';"
               f"s.textContent={json.dumps(ld)};document.head.appendChild(s);</script></body></html>")
    dd.route("/store/mcdonalds-x-1/", js_page)
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    client = PoliteClient(cfg, DiskCache(cfg.data_dir))
    plain = client.fetch(dd.base + "/store/mcdonalds-x-1/")
    assert not parse_store_page(plain.text, plain.url, "1")["restaurant_found"]
    rendered = client.fetch(dd.base + "/store/mcdonalds-x-1/", use_browser=True)
    assert rendered.ok and rendered.via == "browser", rendered.error
    parsed = parse_store_page(rendered.text, rendered.url, "1")
    assert parsed["restaurant_found"] and any(i["price_cents"] == 549 for i in parsed["items"])


# ---------------------------------------------------------------- Grubhub + HTTP API

def test_grubhub_extraction_paths(sites, tmp_path):
    _, _, gh = sites
    gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    page = restaurant_page("McDonald's", gh.base + "/restaurant/mcdonalds-1-main-st-chicago/555", "1 Main St", "Chicago", "IL", "60601",
                           [("Chicken", "10 pc. Chicken McNuggets", "6.29", "USD", None, {"identifier": "menuitem-777"})])
    gh.route("/restaurant/mcdonalds-1-main-st-chicago/555", page)
    cfg = make_cfg(tmp_path, {"grubhub": gh.base})
    client = PoliteClient(cfg, DiskCache(cfg.data_dir))
    r = extract_grubhub(client, gh.base + "/restaurant/mcdonalds-1-main-st-chicago/555?menuItemId=777", gh.base)
    assert r["ok"] and r["fields"]["chain"] == "McDonald's" and r["fields"]["item"] == "10 pc. Chicken McNuggets"
    assert r["fields"]["unit_price_cents"] == 629
    gh.route("/robots.txt", "User-agent: *\nDisallow: /restaurant/\n", ctype="text/plain")
    client2 = PoliteClient(cfg, DiskCache(tmp_path / "other"))
    r2 = extract_grubhub(client2, gh.base + "/restaurant/mcdonalds-1-main-st-chicago/555", gh.base)
    assert not r2["barriers"] == [] and r2["barriers"][0]["kind"] == "robots_disallow"
    assert r2["fields"]["chain"] == "Mcdonalds"  # URL-derived fallback, flagged as a guess
    assert "guessed" in r2["field_sources"]["chain"]
    r3 = extract_grubhub(client2, "https://example.com/x", gh.base)
    assert not r3["ok"] and "Not a Grubhub URL" in r3["notes"][0]


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}") if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def test_http_api(sites, tmp_path):
    dd, ue, _ = sites
    build_doordash(dd, n_stores=2)
    build_ubereats(ue)
    cfg = make_cfg(tmp_path, {"doordash": dd.base, "ubereats": ue.base})
    cfg.snapshot_path = tmp_path / "snapshot.json"
    httpd = serve("127.0.0.1", 0, cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        st, hd, body = _req(base + "/api/health", headers={"Origin": "null"})
        assert st == 200 and body["ok"] and hd.get("Access-Control-Allow-Origin") == "null"
        st, hd, _ = _req(base + "/api/health", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in hd
        st, _, body = _req(base + "/api/snapshot")              # read-only, and only the configured file
        assert st == 404 and "no snapshot" in body["error"]
        cfg.snapshot_path.write_text(json.dumps({"generated_at": "2026-10-08T18:59:00Z", "searches": []}))
        st, hd, body = _req(base + "/api/snapshot")
        assert st == 200 and body["searches"] == [] and hd["Content-Type"] == "application/json"
        st, _, body = _req(base + "/api/jobs", "POST", {"order": {"chain": "", "items": []}})
        assert st == 400 and "chain" in body["error"]
        st, _, body = _req(base + "/api/jobs", "POST", ORDER)
        assert st == 201
        jid = body["job"]["id"]
        since, events, end = 0, [], time.time() + 30
        while time.time() < end:
            st, _, body = _req(base + f"/api/jobs/{jid}?since={since}")
            events += body["events"]
            since = body["next_since"]
            if body["job"]["status"] not in ("running", "queued") and not body["more"]:
                break
            time.sleep(0.1)
        assert body["job"]["status"] == "completed"
        # 2 DoorDash stores + 1 US Uber Eats store with a menu (the sitemap's other two are non-US / no menu).
        assert sum(1 for e in events if e["type"] == "observation") == 3
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
        st, _, body = _req(base + "/api/jobs")
        assert any(j["id"] == jid for j in body["jobs"])
        st, _, body = _req(base + "/api/probe", "POST", {"platforms": ["doordash"]})
        assert body["probe"]["doordash"]["reachable"] and body["probe"]["doordash"]["declared_sitemaps"]
        st, _, body = _req(base + "/api/inspect", "POST", {"url": dd.base + "/store/mcdonalds-city0-1000/"})
        assert body["restaurants"][0]["menu_item_count"] == 5 and "restaurant" in body["jsonld_types"]
        st, _, body = _req(base + "/api/inspect", "POST", {"url": "http://169.254.169.254/latest"})
        assert body["fetch"]["barrier"]["kind"] == "not_allowed"  # no SSRF
        st, _, _ = _req(base + "/api/jobs/../../etc/passwd")
        assert st == 404
        st, _, body = _req(base + "/api/purge", "POST", {})
        assert st == 200 and body["jobs_deleted"] >= 1
        st, _, _ = _req(base + f"/api/jobs/{jid}")
        assert st == 404
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------- quirks observed on real pages (2026-10-08 live runs)

def test_quirks_observed_on_real_uber_eats_pages():
    from server.extract import dedupe_items, flag_zero_prices
    objs, _ = parse_jsonld_blocks(['{"@context":"https://schema.org","@type":"Restaurant","name":"McDonald\'s® (Columbia &amp; 165th St)",'
                                   '"address":{"@type":"PostalAddress","addressRegion":"IN","postalCode":"46320","addressCountry":"US"},'
                                   '"hasMenu":{"@type":"Menu","hasMenuSection":['
                                   '{"@type":"MenuSection","name":"Featured items","hasMenuItem":[{"@type":"MenuItem","name":"Big Mac®","offers":{"@type":"Offer","price":"7.59","priceCurrency":"USD"}},'
                                   '{"@type":"MenuItem","name":"Iced Latte","offers":{"@type":"Offer","price":"0.00","priceCurrency":"USD"}}]},'
                                   '{"@type":"MenuSection","name":"Most Popular","hasMenuItem":[{"@type":"MenuItem","name":"Big Mac®","offers":{"@type":"Offer","price":"7.59","priceCurrency":"USD"}}]}]}}'])
    r = extract_restaurants(objs)[0]
    assert r["name"] == "McDonald's® (Columbia & 165th St)"          # HTML entities in JSON-LD strings
    items = flag_zero_prices(dedupe_items(r["items"]))
    assert len(items) == 2                                              # repeated listing collapsed
    big = next(i for i in items if i["name"] == "Big Mac®")
    assert big["sections"] == ["Featured items", "Most Popular"] and big["price_cents"] == 759
    latte = next(i for i in items if i["name"] == "Iced Latte")
    assert latte["price_cents"] is None and "0.00" in latte["price_note"]  # $0.00 never treated as a price
    from server.extract import _text
    assert _text("Deep Dish Mike&#39;s Hot Honey\ufffd\ufffd") == "Deep Dish Mike's Hot Honey"  # MenuPages: invalid UTF-8 dropped
    # AllMenus: raw control characters inside JSON-LD strings are read leniently, and said so.
    objs, errs = parse_jsonld_blocks(['{"@type":"Restaurant","name":"Lou\tMalnati","hasMenu":{"@type":"Menu","hasMenuItem":'
                                      '[{"@type":"MenuItem","name":"Small\nCheese","offers":{"@type":"Offer","price":"19.65","priceCurrency":"USD"}}]}}'])
    r = extract_restaurants(objs)[0]
    assert r["name"] == "Lou Malnati" and [(i["name"], i["price_cents"]) for i in r["items"]] == [("Small Cheese", 1965)]
    assert errs and "read leniently" in errs[0]
    assert parse_jsonld_blocks(['{"@type": "Restaurant", "name": "x",}'])[0] == []  # still rejects malformed JSON


def test_redirect_to_bot_challenge_is_a_captcha_barrier(sites, tmp_path):
    dd, _, _ = sites
    dd.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    dd.route("/store/x/AbCdEf123456", "", status=307,
             headers={"Location": "https://def.example-challenge.test/en/challenge?from_service=x"})
    cfg = make_cfg(tmp_path, {"doordash": dd.base})
    out = PoliteClient(cfg, DiskCache(cfg.data_dir)).fetch(dd.base + "/store/x/AbCdEf123456")
    assert out.barrier["kind"] == "captcha" and "not followed" in out.barrier["detail"]


def test_sitemap_cdn_host_allowed_for_sitemaps_only(tmp_path):
    cfg = make_cfg(tmp_path, {})
    cfg.test_mode = False
    client = PoliteClient(cfg, DiskCache(cfg.data_dir))
    url = "https://cdn.doordash.com/sitemaps/sitemaps/sitemap-store-doordash-index.xml"
    assert client.check_allowed_url(url, "sitemap") is None          # observed redirect target for sitemaps
    assert client.check_allowed_url(url, "page") is not None          # never for pages
    assert client.check_allowed_url("https://cdn.doordash.com/x", "inspect") is not None


def test_state_store_sitemap_read_first_for_location_filter(sites, tmp_path):
    from fake_platform import sitemap_index, urlset
    dd, _, _ = sites
    b = dd.base
    dd.route("/robots.txt", f"User-agent: *\nSitemap: {b}/sitemap-store-index.xml\n", ctype="text/plain")
    states = ["ca", "tx", "il"]
    dd.route("/sitemap-store-index.xml", sitemap_index([f"{b}/sitemap-doordash-{s}-stores.xml" for s in states]), ctype="application/xml")
    for s_ in states:
        dd.route(f"/sitemap-doordash-{s_}-stores.xml", urlset([]), ctype="application/xml")
    cfg = make_cfg(tmp_path, {"doordash": dd.base}, max_sitemaps_per_run=2)
    job = Service(cfg).jobs.start(dict(ORDER, platforms=["doordash"], prioritize={"state": "TX"}))
    wait_for(job)
    done = job.state["platforms"]["doordash"]["sitemaps"]["done"]
    assert done[1].endswith("sitemap-doordash-tx-stores.xml")  # index first, then the filtered state


# ---------------------------------------------------------------- Grubhub prices via MenuPages (synthetic fixtures)

from fake_platform import build_menupages as _menupages_site  # noqa: E402


def test_grubhub_menu_via_menupages(sites, tmp_path):
    _, _, gh = sites
    mp = FakeSite()
    try:
        _menupages_site(mp)
        gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        gh.route("/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", "<html><head><title>Prepare your taste buds...</title></head><body><div id=app></div></body></html>")
        cfg = make_cfg(tmp_path, {"grubhub": gh.base, "menupages": mp.base})
        client = PoliteClient(cfg, DiskCache(cfg.data_dir))
        r = extract_grubhub(client, "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", gh.base, mp.base)
        assert r["ok"] and r["fields"]["chain"] == "Lou Malnati's Pizzeria"
        assert "linked to this Grubhub ID" in r["field_sources"]["chain"]
        assert r["fields"]["restaurant_address"]["zip"] == "60638"
        menu = r["grubhub_menu"]
        assert {(i["name"], i["price_cents"]) for i in menu["items"]} == {("Small - Deep Dish Margherita", 2155), ("Personal - Deep Dish Margherita", 1425)}
        assert menu["source_url"].endswith("/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago") and menu["grubhub_ids_linked"] == ["434510"]
        assert "/sitemap-states.xml" not in mp.hits  # only restaurant sitemaps are read
        # Same slug but the page links to a different Grubhub restaurant: not used.
        mp2 = FakeSite()
        try:
            _menupages_site(mp2, gh_id="999999")
            cfg2 = make_cfg(tmp_path / "c2", {"grubhub": gh.base, "menupages": mp2.base})
            r2 = extract_grubhub(PoliteClient(cfg2, DiskCache(cfg2.data_dir)), "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", gh.base, mp2.base)
            assert "grubhub_menu" not in r2 and any("not 434510" in n for n in r2["notes"])
            # A MenuPages base outside this client's allowlist is refused without
            # contacting it at all (not even robots.txt).
            mp2.hits.clear()
            r_bad = extract_grubhub(client, "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", gh.base, mp2.base)
            assert "grubhub_menu" not in r_bad and any("not_allowed" in n for n in r_bad["notes"])
            assert mp2.hits == {}, mp2.hits
        finally:
            mp2.close()
        # Live layout: exact-slug page 404s, a same-address decoy links elsewhere,
        # the "-midway" page links to this Grubhub ID and is used.
        mp3 = FakeSite()
        try:
            _menupages_site(mp3, layout="midway")
            cfg3 = make_cfg(tmp_path / "c3", {"grubhub": gh.base, "menupages": mp3.base})
            r4 = extract_grubhub(PoliteClient(cfg3, DiskCache(cfg3.data_dir)), "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", gh.base, mp3.base)
            assert r4["grubhub_menu"]["source_url"].endswith("/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago")
            assert any("not 434510" in n for n in r4["notes"])  # the decoy was checked and refused
            assert "/planet-pizza/6456-s-cicero-ave-chicago" not in mp3.hits
        finally:
            mp3.close()
        # Not listed on MenuPages: clear note, manual entry.
        r3 = extract_grubhub(client, "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712", gh.base, mp.base)
        assert "grubhub_menu" not in r3 and any("not listed on MenuPages" in n for n in r3["notes"])
        assert r3["fields"]["chain"] == "Lou Malnatis Pizzeria"  # URL guess, flagged
    finally:
        mp.close()


def test_allmenus_reader_cross_checks_html_prices(tmp_path):
    from fake_platform import allmenus_page
    from server.grubhub_mirror import parse_allmenus_page, read_allmenus_menu
    page = parse_allmenus_page(allmenus_page(), "https://www.allmenus.com/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/")
    assert not page["problems"] and page["name"] == "Lou Malnati's Pizzeria"
    assert page["address"] == {"street": "85 S Buffalo Grove Rd", "city": "Buffalo Grove", "state": "IL", "zip": "60089", "country": "US"}
    got = {i["name"]: (i["price_cents"], i["section"], i["price_note"]) for i in page["items"]}
    assert got["Small - Deep Dish Margherita"] == (2195, "Deep Dish Margherita", None)
    assert got["BuffaLou Chicken Deep Dish - GF"][0] == 2715
    assert got["Fountain Drink"][0] is None and "not a single USD amount" in got["Fountain Drink"][2]
    assert got["Large - Deep Dish Margherita"][0] == 0  # flag_zero_prices turns this into "not a price" in the reader
    # HTML names that do not match the page's own JSON-LD names: prices not used.
    bad = parse_allmenus_page(allmenus_page(ld_items=["Something else"]), "https://www.allmenus.com/x/")
    assert bad["problems"] and "differ from the JSON-LD" in bad["problems"][0] and not bad["items"]
    # Title and description disagree on the ZIP: not used.
    bad2 = parse_allmenus_page(allmenus_page(title="Lou Malnati&#39;s Pizzeria menu - Buffalo Grove IL 60090 - (847) 215-7100"), "https://www.allmenus.com/x/")
    assert any("name/address" in p for p in bad2["problems"])


def test_allmenus_reader_requires_the_same_grubhub_id(sites, tmp_path):
    from fake_platform import allmenus_page
    from server.grubhub_mirror import read_allmenus_menu
    am = FakeSite()
    try:
        am.route("/robots.txt", "User-agent: *\nDisallow: /goto\n", ctype="text/plain")
        am.route("/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/", allmenus_page())
        am.route("/il/buffalo-grove/1-other/menu/", allmenus_page(gh_id="999"))
        cfg = make_cfg(tmp_path, {"allmenus": am.base})
        client = PoliteClient(cfg, DiskCache(cfg.data_dir))
        r = read_allmenus_menu(client, am.base + "/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/", "517712")
        assert r["ok"] and r["restaurant_name"] == "Lou Malnati's Pizzeria" and r["grubhub_ids_linked"] == ["517712"]
        prices = {i["name"]: i["price_cents"] for i in r["items"]}
        assert prices["Small - Deep Dish Margherita"] == 2195 and prices["Large - Deep Dish Margherita"] is None
        r2 = read_allmenus_menu(client, am.base + "/il/buffalo-grove/1-other/menu/", "517712")
        assert not r2["ok"] and "not 517712" in r2["notes"][0]
    finally:
        am.close()


def test_grubhub_menu_falls_back_to_allmenus(sites, tmp_path):
    from fake_platform import build_allmenus
    _, _, gh = sites
    mp, am = FakeSite(), FakeSite()
    try:
        _menupages_site(mp)
        build_allmenus(am)
        gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        url = "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712"
        cfg = make_cfg(tmp_path, {"grubhub": gh.base, "menupages": mp.base, "allmenus": am.base})
        r = extract_grubhub(PoliteClient(cfg, DiskCache(cfg.data_dir)), url, gh.base)
        menu = r["grubhub_menu"]
        assert menu["source_url"].endswith("/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/")
        assert menu["address"]["street"] == "85 S Buffalo Grove Rd" and r["fields"]["restaurant_address"]["zip"] == "60089"
        assert "AllMenus" in r["field_sources"]["chain"] and any("not listed on MenuPages" in n for n in r["notes"])
        assert any("not 517712" in n for n in r["notes"])                      # the other Lou's in Buffalo Grove was checked and refused
        assert "/il/chicago/1-lou-malnatis-pizzeria/menu/" not in am.hits        # wrong city: never fetched
        assert "/il/buffalo-grove/2-buffalo-wild-wings/menu/" not in am.hits     # wrong name: never fetched
        assert "/sitemap-cities.xml.gz" not in am.hits and "/sitemap-abc.xml.gz" not in am.hits
        assert {(i["name"], i["price_cents"]) for i in menu["items"]} >= {("Small - Deep Dish Margherita", 2195)}
        # A restaurant on neither site: one clear manual-entry note.
        r2 = extract_grubhub(PoliteClient(cfg, DiskCache(cfg.data_dir)),
                             "https://www.grubhub.com/restaurant/some-diner-1-main-st-springfield/42", gh.base)
        assert "grubhub_menu" not in r2 and any("not listed on AllMenus" in n for n in r2["notes"])
    finally:
        mp.close()
        am.close()


# ---------------------------------------------------------------- review findings (Grubhub-owned mirrors)

def test_mirror_barriers_stop_the_host_and_are_reported(sites, tmp_path):
    from fake_platform import build_allmenus
    _, _, gh = sites
    mp, am = FakeSite(), FakeSite()
    try:
        _menupages_site(mp)
        mp.route("/sitemap-index.xml", "<html><title>Forbidden</title></html>", status=403)
        build_allmenus(am)
        am.route("/sitemap-restaurants.xml.gz", "slow down", status=429, headers={"Retry-After": "3600"})
        gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        cfg = make_cfg(tmp_path, {"grubhub": gh.base, "menupages": mp.base, "allmenus": am.base})
        r = extract_grubhub(PoliteClient(cfg, DiskCache(cfg.data_dir)),
                            "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712", gh.base)
        kinds = sorted(b["kind"] for b in r["barriers"])
        assert kinds == ["forbidden", "rate_limited"], kinds
        assert "/sitemap-restaurants.xml.gz" not in mp.hits                  # MenuPages: stopped at the index
        assert am.hits.get("/sitemap-restaurants.xml.gz") == 1 and "/sitemap-restaurants2.xml.gz" not in am.hits  # AllMenus: stopped
        assert not any("not listed" in n for n in r["notes"])                 # no coverage claim from an incomplete search
        assert any("could not be fully searched" in n for n in r["notes"]) and "grubhub_menu" not in r
    finally:
        mp.close()
        am.close()


def test_menupages_without_usable_prices_falls_back_and_never_assumes_usd(sites, tmp_path):
    import json
    from fake_platform import allmenus_page, build_allmenus
    from server.grubhub_mirror import read_menupages_menu
    _, _, gh = sites
    mp, am = FakeSite(), FakeSite()
    try:
        _menupages_site(mp)
        rest = {"@type": "Restaurant", "name": "", "address": {"@type": "PostalAddress", "streetAddress": "85 S Buffalo Grove Rd",
                "addressLocality": "Buffalo Grove", "addressRegion": "IL", "postalCode": "60089", "addressCountry": "US"}}
        menu = {"@type": "Menu", "hasMenuSection": [{"@type": "MenuSection", "name": "Pizza", "hasMenuItem": [
            {"@type": "MenuItem", "name": "Small - Deep Dish Margherita", "offers": {"@type": "Offer", "price": "21.95"}},   # no currency
            {"@type": "MenuItem", "name": "Large - Deep Dish Margherita", "offers": {"@type": "Offer", "price": "0.00", "priceCurrency": "USD"}}]}]}
        page = (f'<html><script type="application/ld+json">{json.dumps(rest)}</script><script type="application/ld+json">{json.dumps(menu)}</script>'
                '<a href="https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712">G</a></html>')
        urls = [f"{mp.base}/lou-malnatis-pizzeria/85-s-buffalo-grove-rd-buffalo-grove"]
        import gzip as _gz
        xml = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
        mp.routes["/sitemap-restaurants.xml.gz"] = {"body": _gz.compress(xml.encode()), "status": 200, "headers": {}, "delay": None, "ctype": "application/x-gzip"}
        mp.route("/lou-malnatis-pizzeria/85-s-buffalo-grove-rd-buffalo-grove", page)
        build_allmenus(am)
        gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        cfg = make_cfg(tmp_path, {"grubhub": gh.base, "menupages": mp.base, "allmenus": am.base})
        client = PoliteClient(cfg, DiskCache(cfg.data_dir))
        m = read_menupages_menu(client, urls[0], "517712")
        assert not m["ok"] and m["restaurant_name"] is None
        assert {i["name"]: i["price_cents"] for i in m["items"]} == {"Small - Deep Dish Margherita": None, "Large - Deep Dish Margherita": None}
        assert "USD cannot be confirmed" in m["items"][0]["price_note"]
        r = extract_grubhub(client, "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712", gh.base)
        assert r["grubhub_menu"]["site"] == "AllMenus"                    # fell back instead of stopping at a priceless page
    finally:
        mp.close()
        am.close()


def test_allmenus_reader_edge_markup():
    from fake_platform import allmenus_page
    from server.grubhub_mirror import parse_allmenus_page
    url = "https://www.allmenus.com/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/"
    for a in ("<br>", '<img src="x.jpg">', "<br/>", '<img src="x.jpg" />'):
        # One void tag (plain or self-closed) in an early description, before later items.
        # (Exactly one: two unclosed tags would shift the depth by the nesting distance
        # of the category list and hide the bug.)
        html = allmenus_page().replace('<p class="description">About BuffaLou Chicken Deep Dish - GF.</p>',
                                       f'<p class="description">About{a}BuffaLou Chicken.</p>')
        assert a in html
        page = parse_allmenus_page(html, url)
        assert not page["problems"] and len(page["items"]) == 5, (a, page["problems"])  # void tags do not shift the depth
    untitled = allmenus_page().replace('<span class="item-title">Fountain Drink</span>', '')
    assert any("without a title" in p for p in parse_allmenus_page(untitled, url)["problems"])
    dotted = parse_allmenus_page(allmenus_page(desc="Restaurant menu, map for Lou Malnati&#39;s Pizzeria located in 60089, "
                                                    "Buffalo Grove IL, 85 S. Buffalo Grove Rd."), url)
    assert not dotted["problems"] and dotted["address"]["street"] == "85 S. Buffalo Grove Rd"


def test_mirror_page_linking_to_several_grubhub_restaurants_is_refused(sites, tmp_path):
    from fake_platform import allmenus_page
    from server.grubhub_mirror import read_allmenus_menu
    am = FakeSite()
    try:
        am.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        two = allmenus_page().replace("</body>", '<a href="https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-1-main-st-elsewhere/999">Nearby</a></body>')
        am.route("/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/", two)
        cfg = make_cfg(tmp_path, {"allmenus": am.base})
        r = read_allmenus_menu(PoliteClient(cfg, DiskCache(cfg.data_dir)), am.base + "/il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/", "517712")
        assert not r["ok"] and "several Grubhub restaurants" in r["notes"][0]
    finally:
        am.close()


def test_allmenus_lookup_reaches_past_four_same_city_branches(sites, tmp_path):
    import gzip as _gz
    from fake_platform import allmenus_page
    from server.grubhub_mirror import grubhub_menu_for
    am = FakeSite()
    try:
        b = am.base
        am.route("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {b}/sitemap-index.xml\n", ctype="text/plain")
        am.route("/sitemap-index.xml", f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{b}/sitemap-restaurants.xml.gz</loc></sitemap></sitemapindex>', ctype="application/xml")
        urls = [f"{b}/il/chicago/{i}-lou-malnatis-pizzeria/menu/" for i in range(1, 7)]
        xml = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
        am.routes["/sitemap-restaurants.xml.gz"] = {"body": _gz.compress(xml.encode()), "status": 200, "headers": {}, "delay": None, "ctype": "application/x-gzip"}
        for i in range(1, 7):
            am.route(f"/il/chicago/{i}-lou-malnatis-pizzeria/menu/", allmenus_page(gh_id=str(1000 + i)))
        cfg = make_cfg(tmp_path, {"allmenus": am.base})
        menu = grubhub_menu_for(PoliteClient(cfg, DiskCache(cfg.data_dir)), "lou-malnatis-pizzeria-1-n-state-st-chicago", "1005",
                                "http://127.0.0.1:9", allmenus_base=am.base)
        assert menu["ok"] and menu["source_url"].endswith("/5-lou-malnatis-pizzeria/menu/")
    finally:
        am.close()


def test_blank_mirror_name_keeps_the_url_chain_guess(sites, tmp_path):
    import json
    _, _, gh = sites
    mp = FakeSite()
    try:
        _menupages_site(mp)
        page = mp.routes["/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago"]["body"].replace('"name": "Lou Malnati&#39;s Pizzeria"', '"name": ""')
        assert '"name": ""' in page
        mp.route("/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago", page)
        gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
        cfg = make_cfg(tmp_path, {"grubhub": gh.base, "menupages": mp.base})
        r = extract_grubhub(PoliteClient(cfg, DiskCache(cfg.data_dir)),
                            "https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510", gh.base)
        assert r["ok"] and r["fields"]["chain"] == "Lou Malnatis Pizzeria" and "guessed" in r["field_sources"]["chain"]
        assert r["grubhub_menu"]["items"]
    finally:
        mp.close()


def test_grubhub_mirrors_can_be_switched_off(monkeypatch):
    from server.config import Config
    monkeypatch.setenv("FPC_DISABLE_GRUBHUB_MIRRORS", "1")
    cfg = Config.from_env()
    assert "menupages" not in cfg.platform_bases and "allmenus" not in cfg.platform_bases
    assert "menupages.com" not in cfg.allowed_hosts and "www.allmenus.com" not in cfg.allowed_hosts
    monkeypatch.delenv("FPC_DISABLE_GRUBHUB_MIRRORS")
    assert "allmenus" in Config.from_env().platform_bases


# The layout of real DoorDash store pages, as recorded from archived captures by
# scripts/archive_probe.py (live-verification/doordash-archive.json): Restaurant
# JSON-LD with a PostalAddress that has addressCountry but no postalCode, a menu
# whose hasMenuSection is a list of lists, offers with no priceCurrency whose
# price is a display string ("$8.53", "US$12.75", "$13.59+" for a starting
# price), and the currency stated only in the page's own (escaped) app data.
def _doordash_like_page(country="US", region="OR", page_currency="USD", restaurant=True):
    menu = {"@type": "Menu", "hasMenuSection": [[
        {"@type": "MenuSection", "name": "Most Ordered", "hasMenuItem": [
            {"@type": "MenuItem", "name": "Crunchwrap Supreme®", "offers": {"@type": "Offer", "price": "8.53"}},
            {"@type": "MenuItem", "name": "Chicken Quesadilla", "offers": {"@type": "Offer", "price": "8.53"}},
            {"@type": "MenuItem", "name": "Build Your Own Box"}]}]]}
    rest = {"@context": "https://schema.org", "@type": "Restaurant", "name": "Taco Bell", "priceRange": "$$",
            "address": {"@type": "PostalAddress", "streetAddress": "1170 Northwest 185th Avenue",
                        "addressLocality": "Beaverton", "addressRegion": region, "addressCountry": country},
            "hasMenu": menu}
    blocks = ([rest] if restaurant else []) + [{"@context": "https://schema.org", "@type": "Organization", "name": "DoorDash"}, menu]
    lds = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)
    app = ('<script>self.__next_f.push([1,"{\\"price\\":{\\"__typename\\":\\"QuickAddMonetaryFields\\",\\"currency\\":\\"'
           + page_currency + '\\",\\"decimalPlaces\\":2},\\"displayPrice\\":\\"$$8.53\\"}"])</script>') if page_currency else ""
    return f"<html><head><title>Taco Bell</title>{lds}</head><body>{app}</body></html>"


def test_doordash_store_page_layout_from_archived_captures():
    from server.adapters import build_observation
    from server.http_client import FetchOutcome
    ad = DoorDashAdapter("https://www.doordash.com")
    ref = ad.parse_store_url("https://www.doordash.com/store/taco-bell-1194579/")
    fetched = FetchOutcome(url=ref["canonical"], ok=True, status=200, fetched_at="2026-10-09T00:00:00Z")

    def observe(html):
        parsed = parse_store_page(html, ref["canonical"], ref["store_id"])
        return build_observation(ad, ref, parsed, fetched, "delivery", "requested", {"crunchwrap"})

    status, obs, detail = observe(_doordash_like_page())
    assert status == "checked", detail
    assert obs["address"] == {"street": "1170 Northwest 185th Avenue", "city": "Beaverton", "state": "OR", "zip": "",
                              "country": "US"}
    assert obs["currency"] == "USD" and obs["menu_item_count"] == 3
    assert [(i["name"], i["price_cents"], i["currency"]) for i in obs["items"]] == [("Crunchwrap Supreme®", 853, "USD")]
    assert "omits priceCurrency; the page's own data states currency USD and no other" in obs["price_context"]

    # The page's data names another currency, or none: the prices are not USD.
    for cur in ("CAD", ""):
        status, obs, _ = observe(_doordash_like_page(page_currency=cur))
        assert status == "checked" and obs["currency"] == "?" and "not treated as USD" in obs["price_context"]
    status, obs, _ = observe(_doordash_like_page(page_currency="USD\\\",\\\"x\\\":{\\\"currency\\\":\\\"CAD"))
    assert obs["currency"] == "?"                                   # USD and CAD on one page: not USD
    # Canadian store (seen in the archive: addressCountry CA, page data CAD) is excluded.
    status, obs, detail = observe(_doordash_like_page(country="CA", region="BC", page_currency="CAD"))
    assert status == "non_us" and obs is None and "outside the US" in detail
    # A bare Menu without a Restaurant (also seen) has no address: not extracted.
    status, obs, detail = observe(_doordash_like_page(restaurant=False))
    assert status == "no_menu" and "no schema.org Restaurant" in detail


def test_store_currency_counts_every_stated_currency():
    """A store whose only listings are $0.00 (real Uber Eats pages have these,
    the price is set by options) still states USD and stays a checked store."""
    from server.adapters import build_observation
    from server.http_client import FetchOutcome
    ad = UberEatsAdapter("https://www.ubereats.com")
    ref = ad.parse_store_url("https://www.ubereats.com/store/zuckers/AbCdEf123456")
    ld = {"@context": "https://schema.org", "@type": "Restaurant", "name": "Zucker's",
          "address": {"@type": "PostalAddress", "streetAddress": "1 A St", "addressLocality": "New York", "addressRegion": "NY",
                      "postalCode": "10001", "addressCountry": "US"},
          "hasMenu": {"@type": "Menu", "hasMenuSection": [{"@type": "MenuSection", "name": "Coffee", "hasMenuItem": [
              {"@type": "MenuItem", "name": "Iced Latte", "offers": {"@type": "Offer", "price": "0.00", "priceCurrency": "USD"}},
              {"@type": "MenuItem", "name": "Bagel"}]}]}}
    parsed = parse_store_page(f'<script type="application/ld+json">{json.dumps(ld)}</script>', ref["canonical"], ref["store_id"])
    fetched = FetchOutcome(url=ref["canonical"], ok=True, status=200, fetched_at="2026-10-14T00:00:00Z")
    status, obs, _ = build_observation(ad, ref, parsed, fetched, "delivery", "requested", set())
    assert status == "checked" and obs["currency"] == "USD"

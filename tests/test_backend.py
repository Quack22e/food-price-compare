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
    cfg.platform_bases = {"doordash": "https://www.doordash.com", "ubereats": "https://www.ubereats.com",
                          "grubhub": "https://www.grubhub.com", **bases}
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
    httpd = serve("127.0.0.1", 0, cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        st, hd, body = _req(base + "/api/health", headers={"Origin": "null"})
        assert st == 200 and body["ok"] and hd.get("Access-Control-Allow-Origin") == "null"
        st, hd, _ = _req(base + "/api/health", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in hd
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

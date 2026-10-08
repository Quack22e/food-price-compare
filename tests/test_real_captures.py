"""Verify the extractors against REAL captured platform pages.

Pages are added with `python -m server.inspect_page --save tests/captures URL`
(each .html has a .json provenance file). With no captures present the test is
skipped and says so; it never substitutes synthetic pages.
"""
import json
from pathlib import Path

import pytest

from server.adapters import DoorDashAdapter, UberEatsAdapter, parse_store_page
from server.extract import normalize_us_address

CAPTURES = sorted(Path(__file__).parent.joinpath("captures").glob("*.html"))
ADAPTERS = {"www.doordash.com": DoorDashAdapter("https://www.doordash.com"),
            "www.ubereats.com": UberEatsAdapter("https://www.ubereats.com")}


@pytest.mark.skipif(not CAPTURES, reason="no real page captures available: the build environment's egress "
                                         "policy blocked doordash.com / ubereats.com / grubhub.com")
@pytest.mark.parametrize("html_path", CAPTURES, ids=[p.stem for p in CAPTURES])
def test_extraction_on_real_capture(html_path):
    meta = json.loads(html_path.with_suffix(".json").read_text())
    from urllib.parse import urlsplit
    host = urlsplit(meta["url"]).netloc
    adapter = ADAPTERS.get(host)
    ref = adapter.parse_store_url(meta["url"]) if adapter else None
    parsed = parse_store_page(html_path.read_text(errors="replace"), meta.get("final_url") or meta["url"],
                              ref["store_id"] if ref else "")
    summary = {"url": meta["url"], "fetched_at": meta["fetched_at"], "jsonld_types": parsed["jsonld_types"],
               "restaurant_found": parsed["restaurant_found"], "items": len(parsed["items"]),
               "json_script_ids": parsed["json_script_ids"]}
    print(json.dumps(summary))
    if adapter and ref:
        # A store page: the adapter's evidence path must hold on the real page.
        assert parsed["restaurant_found"], f"no schema.org Restaurant JSON-LD on real page: {summary}"
        addr, why = normalize_us_address(parsed["address_raw"])
        assert addr or why, summary
        priced = [i for i in parsed["items"] if i.get("price_cents") is not None]
        assert priced, f"menu published without prices: {summary}"
        assert all(i["currency"] in ("USD", None) for i in priced) or not addr, summary


# Grubhub-owned menu sites (MenuPages, AllMenus), saved by scripts/live_verify.py.
MIRRORS = sorted(Path(__file__).parent.joinpath("captures_mirrors").glob("*.html"))


@pytest.mark.skipif(not MIRRORS, reason="no real MenuPages/AllMenus captures available (saved only by the live run)")
@pytest.mark.parametrize("html_path", MIRRORS, ids=[p.stem for p in MIRRORS])
def test_grubhub_mirror_reader_on_real_capture(html_path):
    import re
    from urllib.parse import urlsplit
    from server.extract import scan_html
    from server.grubhub_mirror import GRUBHUB_LINK_RE, parse_allmenus_page
    meta = json.loads(html_path.with_suffix(".json").read_text())
    html = html_path.read_text(errors="replace")
    host = urlsplit(meta["url"]).netloc
    linked = sorted({m.group(2) for a in scan_html(html, meta["url"]).anchors for m in [GRUBHUB_LINK_RE.search(a["href"])] if m})
    if host.endswith("allmenus.com") and re.search(r"/\d+-[^/]+/menu/?$", urlsplit(meta["url"]).path):
        page = parse_allmenus_page(html, meta.get("final_url") or meta["url"])
        priced = [i for i in page["items"] if i["price_cents"]]
        print(json.dumps({"url": meta["url"], "problems": page["problems"], "checks": page["checks"], "name": page["name"],
                          "address": page["address"], "items": len(page["items"]), "priced": len(priced), "grubhub": linked,
                          "sample": [(i["name"], i["price_cents"]) for i in priced[:5]]}))
        assert not page["problems"], page["problems"]
        assert page["address"] and page["address"]["state"] and len(priced) >= 10 and linked
    elif host.endswith("menupages.com"):
        parsed = parse_store_page(html, meta.get("final_url") or meta["url"], "")
        priced = [i for i in parsed["items"] if i.get("price_cents")]
        print(json.dumps({"url": meta["url"], "name": parsed["name"], "items": len(parsed["items"]), "priced": len(priced),
                          "grubhub": linked}))
        assert parsed["restaurant_found"] and priced and linked
    else:
        pytest.skip(f"not a restaurant menu page: {meta['url']}")

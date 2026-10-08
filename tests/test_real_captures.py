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

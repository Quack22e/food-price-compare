"""Grubhub menu prices from MenuPages, a Grubhub-owned menu site.

Grubhub's own restaurant pages are a JavaScript shell with no structured
data (observed live, 2026-10-08). MenuPages (menupages.com, "powered by
Grubhub") publishes, for the restaurants it lists:
  * schema.org Restaurant JSON-LD with the full US address,
  * a separate schema.org Menu JSON-LD block with USD Offer prices,
  * a link to the restaurant's Grubhub listing (grubhub.com/restaurant/<slug>/<id>).
Its robots.txt allows restaurant pages and declares sitemap-index.xml, whose
sitemap-restaurants.xml.gz lists ~28k restaurant pages (observed live).

Mapping, observed on real data (live run 37713405667): the MenuPages page
  menupages.com/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago
links to
  grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510
and the restaurant sitemap lists menupages.com/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago.
So candidates are pages whose "<name>-<address>" equals the Grubhub slug, then
pages at the same address segment; a candidate is only accepted when the
MenuPages page itself links to the same Grubhub restaurant ID. MenuPages covers a subset of Grubhub restaurants;
when there is no match the user enters the Grubhub price manually.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .extract import dedupe_items, flag_zero_prices, normalize_us_address, parse_sitemap, scan_html
from .http_client import PoliteClient, utcnow_iso

MENUPAGES_BASE = "https://menupages.com"
GRUBHUB_LINK_RE = re.compile(r"grubhub\.com/restaurant/([^/?#]+)/(\d+)")


def _restaurant_sitemaps(client: PoliteClient, base: str, cancel=None) -> tuple[list[str], list[str]]:
    """Return (restaurant sitemap URLs, notes) from the robots.txt-declared index."""
    notes = []
    rules, rb = client.robots(base, cancel)
    index_urls = [u for u in rules.sitemaps if urlsplit(u).netloc == urlsplit(base).netloc] or [base + "/sitemap-index.xml"]
    out = []
    for idx in index_urls[:3]:
        f = client.fetch(idx, kind="sitemap", cancel=cancel, ttl=client.cfg.cache_ttl_sitemap_s)
        if not f.ok:
            notes.append(f"MenuPages sitemap index unavailable ({(f.barrier or {}).get('kind') or f.error})")
            continue
        parsed = parse_sitemap(f.body or b"", client.cfg.max_sitemap_bytes)
        locs = [l["loc"] for l in parsed["locs"]]
        out += [u for u in locs if "restaurant" in u.lower()] if parsed["kind"] == "index" else [idx]
    return out, notes


MAX_CANDIDATES = 4


def find_menupages_pages(client: PoliteClient, grubhub_slug: str, base: str = MENUPAGES_BASE, cancel=None) -> dict:
    """Candidate MenuPages pages for a Grubhub slug, best first.

    Exact: "<name>-<address>" equals the slug. Same address: the address
    segment ends the slug and the name segment shares its first word (the
    live Midway listing is "lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago"
    in pages while the slug is "lou-malnatis-pizzeria-6456-s-cicero-ave-chicago").
    Candidates are only accepted later if the page links to the same Grubhub ID.
    """
    slug = grubhub_slug.strip("/").lower()
    res = {"urls": [], "candidates_checked": 0, "notes": []}
    exact, near = [], []
    sitemaps, notes = _restaurant_sitemaps(client, base, cancel)
    res["notes"] += notes
    for sm in sitemaps[:5]:
        f = client.fetch(sm, kind="sitemap", cancel=cancel, ttl=client.cfg.cache_ttl_sitemap_s)
        if not f.ok:
            res["notes"].append(f"MenuPages sitemap {sm} unavailable ({(f.barrier or {}).get('kind') or f.error})")
            continue
        for loc in parse_sitemap(f.body or b"", client.cfg.max_sitemap_bytes)["locs"]:
            parts = [p.lower() for p in urlsplit(loc["loc"]).path.split("/") if p]
            res["candidates_checked"] += 1
            if len(parts) != 2:
                continue
            if f"{parts[0]}-{parts[1]}" == slug:
                exact.append(loc["loc"])
            elif slug.endswith("-" + parts[1]) and parts[0].split("-")[0] == slug.split("-")[0]:
                near.append(loc["loc"])
    res["urls"] = list(dict.fromkeys(exact + near))[:MAX_CANDIDATES]
    if not res["urls"]:
        res["notes"].append("This Grubhub restaurant is not listed on MenuPages (it covers a subset of Grubhub restaurants).")
    return res


def read_menupages_menu(client: PoliteClient, url: str, grubhub_id: str | None, cancel=None) -> dict:
    """Read a MenuPages page; accept it only if it links to the same Grubhub ID."""
    out = {"ok": False, "source_url": url, "observed_at": utcnow_iso(), "notes": [], "barrier": None}
    f = client.fetch(url, kind="page", cancel=cancel)
    if f.barrier:
        out["barrier"] = f.barrier
        return out
    if not f.ok:
        out["notes"].append(f"MenuPages page fetch failed: {f.error}")
        return out
    from .adapters import parse_store_page  # local import: adapters imports this module's users
    parsed = parse_store_page(f.text, f.final_url or url, "")
    linked = sorted({m.group(2) for a in scan_html(f.text, url).anchors for m in [GRUBHUB_LINK_RE.search(a["href"])] if m})
    out["grubhub_ids_linked"] = linked
    out["observed_at"] = f.fetched_at or out["observed_at"]
    if grubhub_id and grubhub_id not in linked:
        out["notes"].append(f"MenuPages page links to Grubhub {linked or 'nothing'}, not {grubhub_id}; not used.")
        return out
    if not parsed["restaurant_found"]:
        out["notes"].append("MenuPages page has no schema.org Restaurant data.")
        return out
    address, _why = normalize_us_address(parsed["address_raw"])
    items = flag_zero_prices(dedupe_items(parsed["items"]))
    out.update({
        "ok": bool(items),
        "restaurant_name": parsed["name"],
        "address": address,
        "items": [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section", "price_note")} for it in items
                  if (it.get("currency") or "USD") == "USD"],
        "source": "MenuPages (Grubhub-owned menu site), schema.org Menu JSON-LD",
        "price_context": "Grubhub menu price as published on MenuPages; options/add-ons and fees not included",
    })
    if not items:
        out["notes"].append("MenuPages page lists no priced menu items.")
    return out


def grubhub_menu_for(client: PoliteClient, grubhub_slug: str, grubhub_id: str, base: str = MENUPAGES_BASE, cancel=None) -> dict:
    found = find_menupages_pages(client, grubhub_slug, base, cancel)
    notes = list(found["notes"])
    for url in found["urls"]:
        menu = read_menupages_menu(client, url, grubhub_id, cancel)
        if menu["ok"] or menu.get("barrier"):
            menu["notes"] = notes + menu["notes"]
            menu["candidates_checked"] = found["candidates_checked"]
            return menu
        notes += [f"{url}: {n}" for n in menu["notes"]]
    if found["urls"]:
        notes.append(f"No MenuPages candidate linked to Grubhub restaurant {grubhub_id}; enter the Grubhub price manually.")
    return {"ok": False, "notes": notes, "candidates_checked": found["candidates_checked"]}

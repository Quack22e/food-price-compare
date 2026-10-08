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

import html as html_lib
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .extract import _text, dedupe_items, flag_zero_prices, normalize_us_address, parse_sitemap, scan_html
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
    items = [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section", "price_note")}
             for it in flag_zero_prices(dedupe_items(parsed["items"])) if (it.get("currency") or "USD") == "USD"]
    out.update({
        "ok": bool(items),
        "restaurant_name": parsed["name"],
        "address": address,
        "items": items,
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


# ---------------------------------------------------------------- AllMenus
# AllMenus (allmenus.com) is also Grubhub-owned. Observed live (runs
# 37743302443, 37744579395) on the Lou Malnati's Buffalo Grove page, which
# links to Grubhub ID 517712 and is not on MenuPages:
#   * its Restaurant JSON-LD lists every item name (217) but no prices, and
#     its name and address fields are blank;
#   * prices are in the HTML, one per item:
#       li.menu-category > div.menu-section-title
#       li.menu-items > div.item-main > span.item-title + span.item-price ("$27.15")
#     (217 item-title, 217 item-price, 217 menu-items on that page);
#   * the address is in <meta name="description">: "... located in 60089,
#     Buffalo Grove IL, 85 S Buffalo Grove Rd." and the <title> repeats the
#     name, city, state and ZIP.
# The HTML prices are used only when the HTML item names equal the JSON-LD
# item names (same names, same counts), the title and description agree on
# the address, and the page links to the same Grubhub restaurant ID.
ALLMENUS_BASE = "https://www.allmenus.com"
_ALLMENUS_PRICE = re.compile(r"^\$(\d{1,4}\.\d{2})$")
_ALLMENUS_TITLE = re.compile(r"^(?P<name>.+?) menu - (?P<city>.+?) (?P<state>[A-Z]{2}) (?P<zip>\d{5})\b")
_ALLMENUS_DESC = re.compile(r"located in (?P<zip>\d{5}), (?P<city>.+?) (?P<state>[A-Z]{2}), (?P<street>[^.]+)\.?\s*$")


class _AllMenusItems(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.section = None
        self._item = None
        self._capture = None   # ("section"|"title"|"price", tag, depth)
        self._buf: list[str] = []
        self._depth = 0
        self._item_depth = None

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "input", "meta", "link", "hr", "source", "wbr"):
            return
        self._depth += 1
        cls = set(dict(attrs).get("class", "").split()) if attrs else set()
        if tag == "li" and "menu-items" in cls:
            self._item = {"title": None, "price": None, "section": self.section}
            self._item_depth = self._depth
        elif self._capture is None:
            kind = ("section" if "menu-section-title" in cls else
                    "title" if "item-title" in cls and self._item is not None else
                    "price" if "item-price" in cls and self._item is not None else None)
            if kind:
                self._capture, self._buf = (kind, tag, self._depth), []

    def handle_endtag(self, tag):
        if self._capture and self._capture[1] == tag and self._capture[2] == self._depth:
            text = _text("".join(self._buf))
            kind = self._capture[0]
            if kind == "section":
                self.section = text
            elif self._item is not None and self._item[kind] is None:
                self._item[kind] = text
            self._capture = None
        if tag == "li" and self._item is not None and self._item_depth == self._depth:
            self.items.append(self._item)
            self._item, self._item_depth = None, None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def parse_allmenus_page(html: str, url: str) -> dict:
    """Items, name and address from an AllMenus menu page, with integrity checks."""
    from .adapters import parse_store_page  # local import: adapters imports this module
    out = {"items": [], "name": None, "address": None, "checks": [], "problems": []}
    p = _AllMenusItems()
    try:
        p.feed(html)
        p.close()
    except Exception as exc:  # malformed markup
        out["problems"].append(f"menu markup could not be read ({exc})")
        return out
    parsed = parse_store_page(html, url, "")
    ld_names = Counter(it["name"] for it in parsed["items"])
    html_names = Counter(it["title"] for it in p.items if it["title"])
    if not p.items:
        out["problems"].append("no li.menu-items entries")
    elif not ld_names:
        out["problems"].append("no JSON-LD item names to cross-check the HTML prices against")
    elif ld_names != html_names:
        diff = sorted((ld_names - html_names) + (html_names - ld_names))[:5]
        out["problems"].append(f"HTML item names differ from the JSON-LD item names (e.g. {diff})")
    else:
        out["checks"].append(f"{len(p.items)} HTML items match the page's {sum(ld_names.values())} JSON-LD item names")
    scan = scan_html(html, url)
    t = _ALLMENUS_TITLE.match(_text(html_lib.unescape(scan.title)))
    d = _ALLMENUS_DESC.search(_text(scan.meta.get("description", "")))
    if t and d and (t["city"], t["state"], t["zip"]) == (d["city"], d["state"], d["zip"]):
        out["name"] = t["name"]
        out["address"] = normalize_us_address({"streetAddress": d["street"], "addressLocality": d["city"],
                                               "addressRegion": d["state"], "postalCode": d["zip"], "addressCountry": "US"})[0]
        out["checks"].append("title and meta description agree on city, state and ZIP")
    else:
        out["problems"].append("restaurant name/address not found in the expected title and description")
    if out["problems"]:
        return out
    for it in p.items:
        m = _ALLMENUS_PRICE.match(it["price"] or "")
        item = {"name": it["title"], "variant": None, "price_cents": int(m.group(1).replace(".", "")) if m else None,
                "currency": "USD", "section": it["section"], "price_note": None}
        if not m:
            item["price_note"] = f"published price text {it['price']!r} is not a single USD amount"
        out["items"].append(item)  # $0.00 is handled by flag_zero_prices
    return out


def read_allmenus_menu(client: PoliteClient, url: str, grubhub_id: str | None, cancel=None) -> dict:
    """Read an AllMenus page; accept it only if it links to the same Grubhub ID."""
    out = {"ok": False, "source_url": url, "observed_at": utcnow_iso(), "notes": [], "barrier": None}
    f = client.fetch(url, kind="page", cancel=cancel)
    if f.barrier:
        out["barrier"] = f.barrier
        return out
    if not f.ok:
        out["notes"].append(f"AllMenus page fetch failed: {f.error}")
        return out
    linked = sorted({m.group(2) for a in scan_html(f.text, url).anchors for m in [GRUBHUB_LINK_RE.search(a["href"])] if m})
    out["grubhub_ids_linked"] = linked
    out["observed_at"] = f.fetched_at or out["observed_at"]
    if grubhub_id and grubhub_id not in linked:
        out["notes"].append(f"AllMenus page links to Grubhub {linked or 'nothing'}, not {grubhub_id}; not used.")
        return out
    page = parse_allmenus_page(f.text, f.final_url or url)
    if page["problems"]:
        out["notes"].append("AllMenus page not used: " + "; ".join(page["problems"]) + ".")
        return out
    items = flag_zero_prices(dedupe_items(page["items"]))
    out.update({
        "ok": any(it.get("price_cents") is not None for it in items),
        "restaurant_name": page["name"],
        "address": page["address"],
        "items": [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section", "price_note")} for it in items],
        "source": "AllMenus (Grubhub-owned menu site): HTML item prices, checked against its schema.org item names",
        "price_context": "Grubhub menu price as published on AllMenus; options/add-ons and fees not included",
        "checks": page["checks"],
    })
    return out

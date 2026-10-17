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


# Barriers that mean "stop asking this host now" (never worked around).
STOP_KINDS = {"rate_limited", "captcha", "forbidden", "login_wall", "robots_disallow", "egress_denied", "not_allowed"}


def _barrier_kind(f) -> str:
    return (f.barrier or {}).get("kind") or f.error or "unavailable"


def _restaurant_sitemaps(client: PoliteClient, base: str, cancel=None, label: str = "MenuPages") -> dict:
    """Restaurant sitemap URLs from the robots.txt-declared index.

    Returns {"sitemaps", "notes", "barrier", "complete"}; complete is False
    when an index could not be read, so callers never claim "not listed".
    """
    res = {"sitemaps": [], "notes": [], "barrier": None, "complete": True}
    rules, rb = client.robots(base, cancel)
    index_urls = [u for u in rules.sitemaps if urlsplit(u).netloc == urlsplit(base).netloc] or [base + "/sitemap-index.xml"]
    for idx in index_urls[:3]:
        f = client.fetch(idx, kind="sitemap", cancel=cancel, ttl=client.cfg.cache_ttl_sitemap_s)
        if not f.ok:
            res["complete"] = False
            res["notes"].append(f"{label} sitemap index unavailable ({_barrier_kind(f)})")
            if f.barrier and f.barrier.get("kind") in STOP_KINDS:
                res["barrier"] = f.barrier
                break
            continue
        parsed = parse_sitemap(f.body or b"", client.cfg.max_sitemap_bytes)
        locs = [l["loc"] for l in parsed["locs"]]
        res["sitemaps"] += [u for u in locs if "restaurant" in u.lower()] if parsed["kind"] == "index" else [idx]
    return res


MAX_CANDIDATES = 4


MAX_MENUPAGES_SITEMAPS = 5


def find_menupages_pages(client: PoliteClient, grubhub_slug: str, base: str = MENUPAGES_BASE, cancel=None) -> dict:
    """Candidate MenuPages pages for a Grubhub slug, best first.

    Exact: "<name>-<address>" equals the slug. Same address: the address
    segment ends the slug and the name segment shares its first word (the
    live Midway listing is "lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago"
    in pages while the slug is "lou-malnatis-pizzeria-6456-s-cicero-ave-chicago").
    Candidates are only accepted later if the page links to the same Grubhub ID.
    "Not listed" is said only when every restaurant sitemap was read.
    """
    slug = grubhub_slug.strip("/").lower()
    idx = _restaurant_sitemaps(client, base, cancel)
    res = {"urls": [], "candidates_checked": 0, "notes": list(idx["notes"]), "barrier": idx["barrier"], "complete": idx["complete"]}
    exact, near = [], []
    if len(idx["sitemaps"]) > MAX_MENUPAGES_SITEMAPS:
        res["complete"] = False
        res["notes"].append(f"Read {MAX_MENUPAGES_SITEMAPS} of {len(idx['sitemaps'])} MenuPages restaurant sitemaps.")
    for sm in [] if res["barrier"] else idx["sitemaps"][:MAX_MENUPAGES_SITEMAPS]:
        f = client.fetch(sm, kind="sitemap", cancel=cancel, ttl=client.cfg.cache_ttl_sitemap_s)
        if not f.ok:
            res["complete"] = False
            res["notes"].append(f"MenuPages sitemap {sm} unavailable ({_barrier_kind(f)})")
            if f.barrier and f.barrier.get("kind") in STOP_KINDS:
                res["barrier"] = f.barrier
                break
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
        res["notes"].append("This Grubhub restaurant is not listed on MenuPages (it covers a subset of Grubhub restaurants)."
                            if res["complete"] else "The MenuPages lookup was incomplete (see above), so it is not known "
                            "whether MenuPages lists this restaurant.")
    return res


def _linked_grubhub_ids(html: str, url: str) -> list[str]:
    return sorted({m.group(2) for a in scan_html(html, url).anchors for m in [GRUBHUB_LINK_RE.search(a["href"])] if m})


def _id_problem(site: str, linked: list[str], grubhub_id: str | None) -> str | None:
    """A page is this listing only if it links to exactly this one Grubhub restaurant."""
    if not grubhub_id or linked == [grubhub_id]:
        return None
    if grubhub_id in linked:
        return f"{site} page links to several Grubhub restaurants ({', '.join(linked)}), so it is not known which one it shows; not used."
    return f"{site} page links to Grubhub {linked or 'nothing'}, not {grubhub_id}; not used."


def read_menupages_menu(client: PoliteClient, url: str, grubhub_id: str | None, cancel=None) -> dict:
    """Read a MenuPages page; accept it only if it links to the same (single) Grubhub ID."""
    out = {"ok": False, "source_url": url, "observed_at": utcnow_iso(), "notes": [], "barrier": None}
    f = client.fetch(url, kind="page", cancel=cancel)
    if f.barrier:
        out["barrier"] = f.barrier
        out["notes"].append(f"MenuPages page {url} not read ({f.barrier.get('kind')}).")
        return out
    if not f.ok:
        out["notes"].append(f"MenuPages page fetch failed: {f.error}")
        return out
    from .adapters import parse_store_page  # local import: adapters imports this module's users
    parsed = parse_store_page(f.text, f.final_url or url, "")
    linked = _linked_grubhub_ids(f.text, url)
    out["grubhub_ids_linked"] = linked
    out["observed_at"] = f.fetched_at or out["observed_at"]
    problem = _id_problem("MenuPages", linked, grubhub_id)
    if problem:
        out["notes"].append(problem)
        return out
    if not parsed["restaurant_found"]:
        out["notes"].append("MenuPages page has no schema.org Restaurant data.")
        return out
    address, _why = normalize_us_address(parsed["address_raw"])
    items = []
    for it in flag_zero_prices(dedupe_items(parsed["items"])):
        rec = {k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "section", "price_note")}
        cur = (it.get("currency") or "").upper()
        if cur and cur != "USD":
            continue  # USD only
        if not cur and rec["price_cents"] is not None:
            # A price without a published currency is not assumed to be USD.
            rec["price_cents"], rec["price_note"] = None, "price published without a currency; USD cannot be confirmed"
        items.append(rec)
    priced = sum(1 for it in items if it["price_cents"] is not None)
    out.update({
        "ok": priced > 0,
        "restaurant_name": parsed["name"] or None,
        "address": address,
        "items": items,
        "site": "MenuPages",
        "source": "MenuPages (Grubhub-owned menu site), schema.org Menu JSON-LD",
        "price_context": "Grubhub menu price as published on MenuPages; options/add-ons and fees not included",
    })
    if not priced:
        out["notes"].append("MenuPages page lists no priced menu items.")
    return out


def grubhub_menu_for(client: PoliteClient, grubhub_slug: str, grubhub_id: str, base: str = MENUPAGES_BASE, cancel=None,
                     allmenus_base: str | None = None) -> dict:
    """Grubhub menu prices from MenuPages, then (if configured) AllMenus.

    A barrier stops work on that site and is returned in "barriers"; the
    other site is still tried. Nothing is returned as a menu unless a page
    linked to exactly this Grubhub ID published at least one USD price.
    """
    barriers: list[dict] = []
    found = find_menupages_pages(client, grubhub_slug, base, cancel)
    notes = list(found["notes"])
    if found["barrier"]:
        barriers.append(found["barrier"])
    for url in found["urls"]:
        menu = read_menupages_menu(client, url, grubhub_id, cancel)
        if menu["ok"]:
            menu.update(notes=notes + menu["notes"], candidates_checked=found["candidates_checked"], barriers=barriers)
            return menu
        notes += [f"{url}: {n}" for n in menu["notes"]]
        if menu.get("barrier"):
            barriers.append(menu["barrier"])
            if menu["barrier"].get("kind") in STOP_KINDS:
                break
    if found["urls"] and not barriers:
        notes.append(f"No MenuPages candidate linked to Grubhub restaurant {grubhub_id} alone with a USD price.")
    checked = found["candidates_checked"]
    complete = found["complete"] and not barriers
    if allmenus_base:
        look = find_allmenus_pages(client, grubhub_slug, allmenus_base, cancel)
        for url, n_checked, note in look:
            checked = n_checked
            if note:
                notes.append(note)
                continue
            menu = read_allmenus_menu(client, url, grubhub_id, cancel)
            if menu["ok"]:
                look.close()
                menu.update(notes=notes + menu["notes"], candidates_checked=checked, barriers=barriers)
                return menu
            notes += [f"{url}: {n}" for n in menu["notes"]]
            if menu.get("barrier"):
                barriers.append(menu["barrier"])
                if menu["barrier"].get("kind") in STOP_KINDS:
                    look.close()
                    break
        barriers += [b for b in look.barriers if b not in barriers]
        complete = complete and look.complete
    else:
        complete = False
    notes.append("No Grubhub-owned menu page linked to this Grubhub restaurant was found; enter the Grubhub price manually."
                 if complete else "The Grubhub-owned menu sites could not be fully searched (see notes); enter the "
                 "Grubhub price manually.")
    return {"ok": False, "notes": notes, "candidates_checked": checked, "barriers": barriers}


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
_ALLMENUS_DESC = re.compile(r"located in (?P<zip>\d{5}), (?P<city>.+?) (?P<state>[A-Z]{2}), (?P<street>.+?)\.?\s*$")


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

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def handle_startendtag(self, tag, attrs):
        # "<br/>" or "<img ... />": a void element never changes the depth.
        # Any other self-closed element opens and closes at once.
        if tag in self.VOID:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
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
        if tag in self.VOID:
            return
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
    untitled = sum(1 for it in p.items if not it["title"])
    if not p.items:
        out["problems"].append("no li.menu-items entries")
    elif untitled:
        out["problems"].append(f"{untitled} menu item(s) without a title, so their prices cannot be tied to an item")
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
        out["notes"].append(f"AllMenus page {url} not read ({f.barrier.get('kind')}).")
        return out
    if not f.ok:
        out["notes"].append(f"AllMenus page fetch failed: {f.error}")
        return out
    linked = _linked_grubhub_ids(f.text, url)
    out["grubhub_ids_linked"] = linked
    out["observed_at"] = f.fetched_at or out["observed_at"]
    problem = _id_problem("AllMenus", linked, grubhub_id)
    if problem:
        out["notes"].append(problem)
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
        "site": "AllMenus",
        "source": "AllMenus (Grubhub-owned menu site): HTML item prices, checked against its schema.org item names",
        "price_context": "Grubhub menu price as published on AllMenus; options/add-ons and fees not included",
        "checks": page["checks"],
    })
    return out


_ALLMENUS_PATH = re.compile(r"^/(?P<st>[a-z]{2})/(?P<city>[a-z0-9-]+)/(?P<id>\d+)-(?P<name>[a-z0-9-]+)/menu/?$")
MAX_ALLMENUS_SITEMAPS = 30
MAX_ALLMENUS_CANDIDATES = 12


class find_allmenus_pages:
    """Iterate (url, urls_checked, note) over AllMenus pages that may be this Grubhub listing.

    Observed live (run 37826699377): AllMenus' robots-declared sitemap index
    lists 21 restaurant sitemaps of up to 40,000 URLs shaped
    /<state>/<city>/<allmenus id>-<name>/menu/, in no particular order, and
    its city pages list no restaurant links in their HTML. A candidate's name
    must begin the Grubhub slug and its city must end it, e.g.
      /il/buffalo-grove/233783-lou-malnatis-pizzeria/menu/
      lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove
    The paths carry no street, so every branch of a chain in that city is a
    candidate; at most MAX_ALLMENUS_CANDIDATES are offered and a note says so
    when that cap is reached. Sitemaps are read lazily (cached 24 h). A
    barrier stops the scan (.barriers); .complete is True only if every
    restaurant sitemap was read and the cap was not reached, which is the
    only case in which "not listed" is said.
    """

    def __init__(self, client: PoliteClient, grubhub_slug: str, base: str = ALLMENUS_BASE, cancel=None):
        self.client, self.slug, self.base, self.cancel = client, grubhub_slug.strip("/").lower(), base, cancel
        self.barriers: list[dict] = []
        self.complete = False
        self._gen = self._run()

    def __iter__(self):
        return self._gen

    def close(self):
        self._gen.close()

    def _run(self):
        client, slug = self.client, self.slug
        checked, seen, complete = 0, [], True
        idx = _restaurant_sitemaps(client, self.base, self.cancel, label="AllMenus")
        for n in idx["notes"]:
            yield None, checked, n
        if idx["barrier"]:
            self.barriers.append(idx["barrier"])
            return
        complete = idx["complete"]
        if len(idx["sitemaps"]) > MAX_ALLMENUS_SITEMAPS:
            complete = False
            yield None, checked, f"Only {MAX_ALLMENUS_SITEMAPS} of {len(idx['sitemaps'])} AllMenus restaurant sitemaps are read."
        for sm in idx["sitemaps"][:MAX_ALLMENUS_SITEMAPS]:
            f = client.fetch(sm, kind="sitemap", cancel=self.cancel, ttl=client.cfg.cache_ttl_sitemap_s)
            if not f.ok:
                complete = False
                yield None, checked, f"AllMenus sitemap {sm} unavailable ({_barrier_kind(f)})"
                if f.barrier and f.barrier.get("kind") in STOP_KINDS:
                    self.barriers.append(f.barrier)
                    yield None, checked, "AllMenus lookup stopped at that barrier."
                    return
                continue
            for loc in parse_sitemap(f.body or b"", client.cfg.max_sitemap_bytes)["locs"]:
                checked += 1
                m = _ALLMENUS_PATH.match(urlsplit(loc["loc"]).path)
                if (m and loc["loc"] not in seen and slug.startswith(m["name"] + "-") and slug.endswith("-" + m["city"])
                        and len(m["name"]) + len(m["city"]) + 2 <= len(slug)):
                    seen.append(loc["loc"])
                    yield loc["loc"], checked, None
                    if len(seen) >= MAX_ALLMENUS_CANDIDATES:
                        yield None, checked, (f"Stopped after {MAX_ALLMENUS_CANDIDATES} AllMenus pages with this name and city; "
                                              "more may exist.")
                        return
        self.complete = complete
        if not seen:
            yield None, checked, (f"This Grubhub restaurant is not listed on AllMenus ({checked:,} AllMenus restaurant pages checked)."
                                  if complete else "The AllMenus lookup was incomplete (see above), so it is not known "
                                  "whether AllMenus lists this restaurant.")

"""Platform adapters.

What each adapter relies on, and how well it is evidenced:

* Store discovery uses (a) store/chain-page URLs the user supplies, (b) the
  sitemaps a site declares in its own robots.txt (sitemaps.org protocol), and
  (c) store links found on chain pages (plain HTML anchors / JSON-LD urls,
  following rel="next" pagination).
* Menus come from schema.org JSON-LD (Restaurant -> hasMenu -> MenuSection ->
  MenuItem -> Offer) on the public store page.
* URL shapes (DoorDash /store/<slug>-<id>, /business/...; Uber Eats
  /store/<slug>/<id>, /brand/...) and the pickup/dining-mode query parameters
  are taken from public third-party descriptions. They could NOT be confirmed
  live from the build environment (egress-blocked); `python -m server.inspect`
  exists to confirm them wherever network access is available.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .extract import (
    contains_sequence, dedupe_items, flag_zero_prices, extract_menu_objects, extract_restaurants, jsonld_type_summary,
    normalize_us_address, parse_jsonld_blocks, prefilter_tokens, loose_norm, scan_html,
    slug_tokens, same_site,
)
from .http_client import PoliteClient, barrier, utcnow_iso


class PlatformAdapter:
    key = ""
    label = ""
    store_path_re: re.Pattern
    chain_path_re: re.Pattern

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.host = urlsplit(self.base).netloc.lower()

    # -- URL handling ------------------------------------------------------
    def parse_store_url(self, url: str) -> dict | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.netloc.lower() != self.host:
            return None
        m = self.store_path_re.match(parts.path)
        if not m:
            return None
        g = m.groupdict()
        return {"store_id": g["id"], "slug": g.get("slug") or "", "country": (g.get("cc") or "").lower() or None,
                "canonical": urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))}

    def is_chain_page(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return parts.netloc.lower() == self.host and bool(self.chain_path_re.match(parts.path))

    def mode_url(self, canonical: str, mode: str) -> tuple[str, str]:
        raise NotImplementedError

    def sample_store_url(self) -> str:
        """A representative store URL, used to test robots.txt permission."""
        raise NotImplementedError

    def slug_matches_chain(self, slug: str, chain_tokens: list[str]) -> bool:
        if not chain_tokens:
            return True
        return contains_sequence(slug_tokens(slug), chain_tokens)


class DoorDashAdapter(PlatformAdapter):
    key = "doordash"
    label = "DoorDash"
    store_path_re = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?store/(?:(?P<slug>[^/]+?)-)?(?P<id>\d+)/?$", re.I)
    chain_path_re = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?business/(?P<slug>[^/]+?)(?:-\d+)?/?$", re.I)

    def mode_url(self, canonical: str, mode: str) -> tuple[str, str]:
        # DoorDash's own store links carry ?pickup=false / ?pickup=true (seen in
        # a 2026 DoorDash CLI test fixture mirroring DoorDash feed data).
        value = "true" if mode == "pickup" else "false"
        return canonical + "?" + urlencode({"pickup": value}), (
            f"{mode} requested with ?pickup={value}, the parameter DoorDash's own store links use; whether the "
            "page's published JSON-LD prices change with it was not verifiable from the build environment")

    def sample_store_url(self) -> str:
        return self.base + "/store/example-store-1/"


class UberEatsAdapter(PlatformAdapter):
    key = "ubereats"
    label = "Uber Eats"
    store_path_re = re.compile(
        r"^/(?:(?P<cc>[a-z]{2})(?:-[a-z]{2})?/)?store/(?P<slug>[^/]+)/(?P<id>[A-Za-z0-9_-]{6,})/?$", re.I)
    chain_path_re = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?brand/(?P<slug>[^/]+)/?$", re.I)

    def mode_url(self, canonical: str, mode: str) -> tuple[str, str]:
        value = "PICKUP" if mode == "pickup" else "DELIVERY"
        return canonical + "?" + urlencode({"diningMode": value}), (
            f"requested with ?diningMode={value} (parameter reported publicly; its effect on the "
            "page's published prices was not verifiable from the build environment)")

    def sample_store_url(self) -> str:
        return self.base + "/store/example-store/AbCdEf123456"


def make_adapters(platform_bases: dict) -> dict[str, PlatformAdapter]:
    return {
        "doordash": DoorDashAdapter(platform_bases["doordash"]),
        "ubereats": UberEatsAdapter(platform_bases["ubereats"]),
    }


# --------------------------------------------------------------------------
# Store page -> observation
# --------------------------------------------------------------------------

def _pick_restaurant(restaurants: list[dict], store_url: str, store_id: str) -> dict | None:
    if not restaurants:
        return None
    for r in restaurants:
        for v in (r.get("url"), r.get("id")):
            if v and (store_id in v or v.rstrip("/") == store_url.rstrip("/")):
                return r
    with_menu = [r for r in restaurants if r["items"] or r["menu_urls"]]
    return (with_menu or restaurants)[0]


def parse_store_page(html: str, page_url: str, store_id: str) -> dict:
    """Extract restaurant identity, address and menu from a store page."""
    scan = scan_html(html, page_url)
    objs, errors = parse_jsonld_blocks(scan.jsonld_raw)
    restaurants = extract_restaurants(objs)
    r = _pick_restaurant(restaurants, page_url.split("?")[0], store_id)
    loose_items, loose_menu_urls = extract_menu_objects(objs)
    result = {
        "jsonld_types": jsonld_type_summary(objs),
        "jsonld_errors": errors,
        "restaurant_found": r is not None,
        "name": r["name"] if r else (scan.meta.get("og:title") or scan.title or "").strip(),
        "address_raw": r["address_raw"] if r else None,
        "lat": r.get("lat") if r else None,
        "lng": r.get("lng") if r else None,
        "items": (r["items"] if r else []) + ([] if (r and r["items"]) else loose_items),
        "menu_urls": (r["menu_urls"] if r else []) + loose_menu_urls,
        "json_script_ids": [s["id"] for s in scan.json_scripts if s["id"]][:20],
    }
    return result


def build_observation(adapter: PlatformAdapter, ref: dict, parsed: dict, fetched, mode: str,
                      mode_basis: str, prefilter: set[str]) -> tuple[str, dict | None, str]:
    """Return (status, observation|None, detail)."""
    if not parsed["restaurant_found"]:
        types = ", ".join(parsed["jsonld_types"]) or "none"
        extra = f"; browser render blocked ({parsed['render_barrier']})" if parsed.get("render_barrier") else ""
        return "no_menu", None, (f"no schema.org Restaurant data on page (JSON-LD types: {types}){extra}; "
                                 "menu not extracted rather than guessed")
    address, why = normalize_us_address(parsed["address_raw"])
    if ref.get("country") and ref["country"] != "us":
        return "non_us", None, f"store URL is in country section '/{ref['country']}/'"
    if address is None:
        return "non_us", None, f"excluded: {why}"
    items = flag_zero_prices(dedupe_items(parsed["items"]))
    if not items:
        return "no_menu", None, "restaurant data published but no menu items/prices"
    kept = []
    for it in items:
        name_norm = loose_norm(" ".join(filter(None, [it.get("name"), it.get("variant")])))
        if not prefilter or any(t in name_norm for t in prefilter):
            kept.append(it)
    currencies = sorted({it.get("currency") or "?" for it in items})
    obs = {
        "source": "live",
        "platform": adapter.key,
        "store_id": ref["store_id"],
        "store_name": parsed["name"],
        "store_url": ref["canonical"],
        "fetched_url": fetched.url,
        "address": address,
        "lat": parsed.get("lat"),
        "lng": parsed.get("lng"),
        "observed_at": fetched.fetched_at or utcnow_iso(),
        "from_cache": fetched.from_cache,
        "fetch_via": fetched.via,
        "order_mode": mode,
        "mode_basis": mode_basis,
        "price_context": "anonymous session (no login, no saved cookies), no delivery address set; "
                         "menu prices as published on the public store page",
        "currency": currencies[0] if len(currencies) == 1 else "mixed",
        "extraction": "schema.org JSON-LD (Restaurant.hasMenu -> MenuItem.offers)",
        "menu_item_count": len(items),
        "items": kept,
        "fees": {"delivery_fee_cents": None, "service_fee_cents": None, "small_order_fee_cents": None,
                 "tax_cents": None, "destination_zip": None},
    }
    return "checked", obs, f"{len(items)} menu items published; {len(kept)} sent as candidates"


# --------------------------------------------------------------------------
# Chain pages
# --------------------------------------------------------------------------

def store_links_from_page(adapter: PlatformAdapter, html: str, page_url: str) -> tuple[list[str], list[str]]:
    """Return (store_urls, next_page_urls) found on a chain/brand page."""
    scan = scan_html(html, page_url)
    found: list[str] = []
    for a in scan.anchors:
        if adapter.parse_store_url(a["href"]):
            found.append(a["href"])
    objs, _ = parse_jsonld_blocks(scan.jsonld_raw)

    def walk(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("url", "@id") and isinstance(v, str) and adapter.parse_store_url(v):
                    found.append(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    walk(objs)
    nxt = [lnk["href"] for lnk in scan.links if "next" in lnk["rel"].split()]
    nxt += [a["href"] for a in scan.anchors if "next" in a["rel"].lower().split()]
    nxt = [u for u in nxt if same_site(u, adapter.host)]
    seen, uniq = set(), []
    for u in found:
        p = adapter.parse_store_url(u)
        if p and p["store_id"] not in seen:
            seen.add(p["store_id"])
            uniq.append(p["canonical"])
    return uniq, list(dict.fromkeys(nxt))


# --------------------------------------------------------------------------
# Grubhub URL extraction
# --------------------------------------------------------------------------

GRUBHUB_PATH_RE = re.compile(r"^/restaurant/(?P<slug>[^/]+)/(?P<id>\d+)/?$", re.I)


def chain_guess_from_slug(slug: str) -> str:
    words = []
    for w in slug.split("-"):
        if w.isdigit():
            break
        words.append(w)
    return " ".join(w.capitalize() for w in words) if words else ""


def extract_grubhub(client: PoliteClient, url: str, grubhub_base: str) -> dict:
    out = {"ok": False, "url": url, "fields": {}, "field_sources": {}, "barriers": [], "notes": [],
           "observed_at": utcnow_iso()}
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        out["notes"].append("Not a valid URL. Enter the order manually.")
        return out
    gh_host = urlsplit(grubhub_base).netloc.lower()
    if parts.netloc.lower() not in ("www.grubhub.com", "grubhub.com", gh_host):
        out["notes"].append(f"Not a Grubhub URL (host '{parts.netloc}'). Enter the order manually.")
        return out
    m = GRUBHUB_PATH_RE.match(parts.path)
    if not m:
        out["notes"].append("URL is on Grubhub but is not a /restaurant/<name>/<id> page; "
                            "only restaurant/menu-item links are supported.")
        return out
    qs = parse_qs(parts.query)
    menu_item_id = (qs.get("menuItemId") or [None])[0]
    guess = chain_guess_from_slug(m.group("slug"))
    out["fields"].update({"grubhub_restaurant_id": m.group("id"), "grubhub_menu_item_id": menu_item_id})
    if guess:
        out["fields"]["chain"] = guess
        out["field_sources"]["chain"] = "guessed from URL slug (confirm spelling/punctuation)"
    # Always fetched from the configured Grubhub base (the real site unless
    # the test-suite substitutes a local fixture server).
    canonical = grubhub_base.rstrip("/") + parts.path
    fetched = client.fetch(canonical + (f"?menuItemId={menu_item_id}" if menu_item_id else ""), kind="page")
    if fetched.barrier:
        out["barriers"].append(fetched.barrier)
        out["notes"].append("Grubhub page could not be retrieved (see barrier). Fields derived from the URL "
                            "only; complete the item details manually.")
        return out
    if not fetched.ok:
        out["notes"].append(f"Grubhub page fetch failed: {fetched.error}. Complete the details manually.")
        return out
    parsed = parse_store_page(fetched.text, fetched.final_url or canonical, m.group("id"))
    if parsed["restaurant_found"] and parsed["name"]:
        out["fields"]["chain"] = parsed["name"]
        out["field_sources"]["chain"] = "schema.org Restaurant.name on the Grubhub page"
        addr, _ = normalize_us_address(parsed["address_raw"])
        if addr:
            out["fields"]["restaurant_address"] = addr
    if menu_item_id:
        hit = next((it for it in parsed["items"] if it.get("item_id") and menu_item_id in it["item_id"]), None)
        if hit:
            out["fields"]["item"] = hit["name"]
            out["field_sources"]["item"] = "schema.org MenuItem matching menuItemId"
            if hit.get("variant"):
                out["fields"]["size"] = hit["variant"]
            if hit.get("price_cents") is not None and (hit.get("currency") or "").upper() == "USD":
                out["fields"]["unit_price_cents"] = hit["price_cents"]
                out["field_sources"]["unit_price_cents"] = "MenuItem Offer price (base price, before options)"
        else:
            out["notes"].append("The page did not publish a MenuItem matching menuItemId; enter the item name.")
    if not parsed["restaurant_found"]:
        out["notes"].append("No schema.org Restaurant data on the Grubhub page (the page may need JavaScript). "
                            "Fields come from the URL only.")
    out["ok"] = bool(out["fields"].get("chain"))
    out["page_jsonld_types"] = parsed["jsonld_types"]
    return out


# --------------------------------------------------------------------------
# Evidence harness
# --------------------------------------------------------------------------

def inspect_url(client: PoliteClient, url: str, use_browser: bool = False) -> dict:
    fetched = client.fetch(url, kind="inspect", ttl=0, use_browser=use_browser)
    return inspect_fetched(url, fetched)


def inspect_fetched(url: str, fetched) -> dict:
    from .structure_probe import probe_structure
    report = {"url": url, "fetch": fetched.summary()}
    if fetched.body:
        report["structure"] = probe_structure(fetched.text)
    if not fetched.ok:
        return report
    html = fetched.text
    scan = scan_html(html, fetched.final_url or url)
    objs, errors = parse_jsonld_blocks(scan.jsonld_raw)
    restaurants = extract_restaurants(objs)
    report.update({
        "title": scan.title.strip()[:200],
        "jsonld_block_count": len(scan.jsonld_raw),
        "jsonld_errors": errors,
        "jsonld_types": jsonld_type_summary(objs),
        "json_scripts": scan.json_scripts[:30],
        "has___NEXT_DATA__": any(s["id"] == "__NEXT_DATA__" for s in scan.json_scripts),
        "og": {k: v[:200] for k, v in scan.meta.items() if k.startswith("og:")},
        "restaurants": [{
            "name": r["name"], "types": r["types"],
            "address": normalize_us_address(r["address_raw"]),
            "menu_item_count": len(r["items"]),
            "menu_urls": r["menu_urls"][:5],
            "sample_items": [{k: it.get(k) for k in ("name", "variant", "price_cents", "currency", "availability", "section")}
                             for it in r["items"][:8]],
        } for r in restaurants],
    })
    return report


def order_prefilter(order: dict) -> set[str]:
    texts = []
    for it in order.get("items", []):
        texts.append(it.get("name", ""))
        texts.extend(it.get("aliases") or [])
        if it.get("flavor"):
            texts.append(it["flavor"])
    return prefilter_tokens(texts)


__all__ = [
    "DoorDashAdapter", "UberEatsAdapter", "make_adapters", "parse_store_page", "build_observation",
    "store_links_from_page", "extract_grubhub", "inspect_url", "inspect_fetched", "order_prefilter", "chain_guess_from_slug",
    "barrier",
]

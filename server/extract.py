"""Parsers for data that pages publish in standard, documented formats.

Only published standards are assumed here:
  * schema.org JSON-LD (Restaurant / FoodEstablishment, Menu, MenuSection,
    MenuItem, Offer, AggregateOffer, PriceSpecification, PostalAddress)
  * the sitemaps.org XML protocol (urlset / sitemapindex, optional gzip)
  * ordinary HTML anchors, <link rel>, <meta> and <title>

No private platform response schema is assumed. Pages whose data is not
published in one of these forms produce an explicit "no structured menu data"
outcome instead of guessed values.
"""
from __future__ import annotations

import gzip
import html as html_lib
import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}
_STATE_BY_NAME["washington dc"] = "DC"
_STATE_BY_NAME["washington d.c."] = "DC"
_US_COUNTRY = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")

FOOD_ESTABLISHMENT_TYPES = {
    "restaurant", "foodestablishment", "fastfoodrestaurant", "cafeorcoffeeshop",
    "bakery", "barorpub", "brewery", "icecreamshop", "winery", "distillery",
}


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

_PRICE_RE = re.compile(r"^(?:US\s*)?\$?\s*(\d{1,7}(?:\.\d+)?)\s*(?:USD)?$", re.I)


def price_to_cents(value) -> int | None:
    """Convert a published price to integer cents without binary floats.

    Accepts JSON numbers or strings such as "9.99", "$9.99", "1,299.00".
    Returns None for anything ambiguous: negative values, more than two
    non-zero decimal places, or a comma used as a decimal separator.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 100 if 0 <= value < 10_000_000 else None
    if isinstance(value, float):
        text = repr(value)
    else:
        text = str(value).strip()
        # Strip thousands separators only where they are unambiguous.
        text = re.sub(r",(?=\d{3}(?:\D|$))", "", text)
        if "," in text:
            return None
    m = _PRICE_RE.match(text)
    if not m:
        return None
    try:
        d = Decimal(m.group(1))
    except InvalidOperation:
        return None
    cents = d * 100
    if cents != cents.to_integral_value() or cents < 0:
        return None
    return int(cents)


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------

def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@id") or "")
    if isinstance(v, list):
        return _text(v[0]) if v else ""
    # Observed live (Uber Eats, 2026-10-08): JSON-LD strings carry HTML
    # entities, e.g. "McDonald's® (Columbia &amp; 165th St)".
    # Observed live (MenuPages, 2026-10-08): some section names end in bytes
    # that are not valid UTF-8 ("Deep Dish Mike's Hot Honey\ufffd\ufffd"); the
    # replacement characters carry no information, so they are dropped.
    return re.sub(r"\s+", " ", html_lib.unescape(str(v)).replace("\ufffd", "")).strip()


def normalize_state(value: str) -> str | None:
    s = _text(value).strip().rstrip(".")
    if not s:
        return None
    if s.upper() in US_STATES:
        return s.upper()
    return _STATE_BY_NAME.get(s.lower())


def normalize_us_address(addr) -> tuple[dict | None, str | None]:
    """Return (address, None) for a 50-states/DC address, else (None, reason)."""
    if isinstance(addr, list):
        addr = addr[0] if addr else None
    if isinstance(addr, str):
        return None, "address is unstructured text; cannot verify US state/ZIP"
    if not isinstance(addr, dict):
        return None, "no address published"
    country = _text(addr.get("addressCountry")).lower()
    region_raw = _text(addr.get("addressRegion"))
    state = normalize_state(region_raw)
    zip_code = _text(addr.get("postalCode"))
    out = {
        "street": _text(addr.get("streetAddress")),
        "city": _text(addr.get("addressLocality")),
        "state": state or region_raw,
        "zip": zip_code,
        "country": "US" if country in _US_COUNTRY else (country.upper() or None),
    }
    if country and country not in _US_COUNTRY:
        return None, f"country '{country}' is outside the US"
    if not state:
        return None, f"region '{region_raw}' is not one of the 50 states or DC"
    if zip_code and not _ZIP_RE.match(zip_code):
        return None, f"postal code '{zip_code}' is not a US ZIP"
    if not country and not zip_code:
        return None, "neither country nor ZIP published; cannot verify US location"
    out["country"] = "US"
    out["zip"] = zip_code[:5] if zip_code else ""
    return out, None


# --------------------------------------------------------------------------
# HTML scanning
# --------------------------------------------------------------------------

class PageScan(HTMLParser):
    """Single pass over an HTML document collecting standard metadata."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.jsonld_raw: list[str] = []
        self.json_scripts: list[dict] = []  # {id, type, length} for inspection
        self.anchors: list[dict] = []
        self.links: list[dict] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_script: dict | None = None
        self._script_buf: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            stype = a.get("type", "").lower()
            self._in_script = {"type": stype, "id": a.get("id", "")}
            self._script_buf = []
        elif tag == "a" and a.get("href"):
            self.anchors.append({"href": urljoin(self.base_url, a["href"]), "rel": a.get("rel", "")})
        elif tag == "link" and a.get("href"):
            self.links.append({"href": urljoin(self.base_url, a["href"]), "rel": a.get("rel", "").lower()})
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script is not None:
            body = "".join(self._script_buf)
            info = self._in_script
            if "ld+json" in info["type"]:
                self.jsonld_raw.append(body)
            elif "json" in info["type"] or info["id"]:
                self.json_scripts.append({"id": info["id"], "type": info["type"], "length": len(body)})
            self._in_script = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_script is not None:
            self._script_buf.append(data)
        elif self._in_title:
            self.title += data


def scan_html(html: str, base_url: str) -> PageScan:
    scan = PageScan(base_url)
    try:
        scan.feed(html)
        scan.close()
    except Exception:  # malformed markup: keep whatever was collected
        pass
    return scan


def parse_jsonld_blocks(raw_blocks: list[str]) -> tuple[list, list[str]]:
    """Parse JSON-LD script bodies. Returns (objects, errors)."""
    objs, errors = [], []
    for raw in raw_blocks:
        text = raw.strip()
        text = re.sub(r"^\s*<!--|-->\s*$", "", text)
        text = re.sub(r"^\s*/\*<!\[CDATA\[\*/|/\*\]\]>\*/\s*$", "", text)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON-LD block ({exc.msg} at char {exc.pos})")
            continue
        objs.extend(_flatten_graph(data))
    return objs, errors


def _flatten_graph(data) -> list:
    out = []
    if isinstance(data, list):
        for d in data:
            out.extend(_flatten_graph(d))
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            out.extend(_flatten_graph(data["@graph"]))
            rest = {k: v for k, v in data.items() if k != "@graph"}
            if "@type" in rest:
                out.append(rest)
        else:
            out.append(data)
    return out


def types_of(obj) -> set[str]:
    if not isinstance(obj, dict):
        return set()
    t = obj.get("@type")
    vals = t if isinstance(t, list) else [t]
    out = set()
    for v in vals:
        if isinstance(v, str):
            out.add(v.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower())
    return out


def jsonld_type_summary(objs: list) -> list[str]:
    seen = []
    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            for t in sorted(types_of(o)):
                if t not in seen:
                    seen.append(t)
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    walk(objs)
    return seen


# --------------------------------------------------------------------------
# schema.org Restaurant / Menu
# --------------------------------------------------------------------------

def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def normalize_availability(v) -> str:
    s = _text(v).rsplit("/", 1)[-1].lower()
    if not s:
        return "unknown"
    if s in {"instock", "instoreonly", "onlineonly", "limitedavailability", "preorder", "presale"}:
        return "available"
    if s in {"outofstock", "soldout", "discontinued"}:
        return "unavailable"
    return "unknown"


def _parse_offer(offer) -> dict:
    if not isinstance(offer, dict):
        # A bare number/string as "offers" is not schema-conformant; record it
        # but do not trust the currency.
        return {"price_cents": price_to_cents(offer), "currency": None, "variant": None,
                "availability": "unknown", "note": "offer is not an Offer object"}
    t = types_of(offer)
    if "aggregateoffer" in t:
        return {
            "price_cents": None, "currency": _text(offer.get("priceCurrency")) or None,
            "variant": _text(offer.get("name")) or None, "availability": normalize_availability(offer.get("availability")),
            "price_range_cents": [price_to_cents(offer.get("lowPrice")), price_to_cents(offer.get("highPrice"))],
            "note": "AggregateOffer publishes a price range, not one configured price",
        }
    price = offer.get("price")
    currency = offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if price is None and spec is not None:
        for s in _as_list(spec):
            if isinstance(s, dict) and s.get("price") is not None:
                price, currency = s.get("price"), currency or s.get("priceCurrency")
                break
    return {
        "price_cents": price_to_cents(price),
        "price_raw": None if price is None else str(price)[:32],
        "currency": (_text(currency).upper() or None),
        "variant": _text(offer.get("name")) or None,
        "availability": normalize_availability(offer.get("availability")),
    }


def _parse_addons(item: dict) -> list[dict]:
    addons = []
    for section in _as_list(item.get("menuAddOn")):
        if not isinstance(section, dict):
            continue
        group = _text(section.get("name"))
        entries = _as_list(section.get("hasMenuItem")) if "menusection" in types_of(section) else [section]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            offers = _as_list(entry.get("offers"))
            off = _parse_offer(offers[0]) if offers else {"price_cents": None, "currency": None}
            addons.append({
                "name": _text(entry.get("name")),
                "group": group or None,
                "price_cents": off.get("price_cents"),
                "currency": off.get("currency"),
            })
    return addons


def _parse_menu_item(item: dict, section: str | None) -> list[dict]:
    name = _text(item.get("name"))
    if not name:
        return []
    base = {
        "item_id": _text(item.get("identifier") or item.get("@id") or item.get("url")) or None,
        "name": name,
        "description": _text(item.get("description"))[:500] or None,
        "section": section,
        "addons": _parse_addons(item),
        # schema.org has no field that says whether mandatory option groups
        # exist, so this is "unknown" unless a source states otherwise.
        "required_options": "unknown",
    }
    offers = _as_list(item.get("offers"))
    if not offers:
        return [dict(base, price_cents=None, currency=None, variant=None, availability="unknown",
                     note="no Offer published for this item")]
    out = []
    for off in offers:
        parsed = _parse_offer(off)
        variant = parsed.get("variant")
        if variant and variant.strip().lower() == name.strip().lower():
            variant = None
        rec = dict(base)
        rec.update(parsed)
        rec["variant"] = variant
        out.append(rec)
    return out


def walk_menu(node, section: str | None, items: list, menu_urls: list, depth: int = 0) -> None:
    if depth > 10 or node is None:
        return
    if isinstance(node, list):
        for n in node:
            walk_menu(n, section, items, menu_urls, depth + 1)
        return
    if isinstance(node, str):
        menu_urls.append(node)
        return
    if not isinstance(node, dict):
        return
    t = types_of(node)
    if "menuitem" in t:
        items.extend(_parse_menu_item(node, section))
        return
    name = _text(node.get("name")) or None
    sec = name if "menusection" in t else section
    walk_menu(node.get("hasMenuSection"), sec, items, menu_urls, depth + 1)
    walk_menu(node.get("hasMenuItem"), sec, items, menu_urls, depth + 1)


def dedupe_items(items: list[dict]) -> list[dict]:
    """Collapse identical listings repeated across menu sections.

    Observed live: Uber Eats repeats an item under "Featured items", "Most
    Popular", promotion sections, etc. with the same name and price. Listings
    that differ in price or variant are kept separately.
    """
    out, index = [], {}
    for it in items:
        key = (it.get("name"), it.get("variant"), it.get("price_cents"), it.get("currency"), it.get("availability"))
        if key in index:
            sec = it.get("section")
            if sec and sec not in index[key]["sections"]:
                index[key]["sections"].append(sec)
            continue
        rec = dict(it)
        rec["sections"] = [it["section"]] if it.get("section") else []
        index[key] = rec
        out.append(rec)
    return out


def flag_zero_prices(items: list[dict]) -> list[dict]:
    """A published price of exactly 0.00 is not treated as a real price.

    Observed live (Uber Eats, Zucker's Bagels, 2026-10-08): "Iced Latte",
    "Coffee", "Plain Cream Cheese (Lb)" are published at 0.00 because the
    price is set by required options (size/weight). Such listings can never
    be verified; they surface as "needs confirmation".
    """
    for it in items:
        if it.get("price_cents") == 0:
            it["price_cents"] = None
            it["price_note"] = ("published price is $0.00, which on this platform means the price is set by "
                                "required options (e.g. size); actual price not published")
            it["required_options"] = [{"name": "price-setting options (not published)", "required": True}]
    return items


def extract_restaurants(objs: list) -> list[dict]:
    """Find schema.org food establishments (searching nested objects too)."""
    found = []

    def visit(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, list):
            for x in o:
                visit(x, depth + 1)
            return
        if not isinstance(o, dict):
            return
        if types_of(o) & FOOD_ESTABLISHMENT_TYPES:
            found.append(o)
            return
        for k, v in o.items():
            if k in ("@context",):
                continue
            visit(v, depth + 1)

    visit(objs)
    out = []
    for r in found:
        items, menu_urls = [], []
        walk_menu(r.get("hasMenu") or r.get("menu"), None, items, menu_urls)
        geo = r.get("geo") if isinstance(r.get("geo"), dict) else {}
        out.append({
            "name": _text(r.get("name")),
            "url": _text(r.get("url")) or None,
            "id": _text(r.get("@id")) or None,
            "address_raw": r.get("address"),
            "lat": geo.get("latitude"),
            "lng": geo.get("longitude"),
            "items": items,
            "menu_urls": menu_urls,
            "types": sorted(types_of(r)),
        })
    return out


def extract_menu_objects(objs: list) -> tuple[list[dict], list[str]]:
    """Top-level Menu objects published separately from the Restaurant."""
    items, urls = [], []
    for o in objs:
        if "menu" in types_of(o):
            walk_menu(o, None, items, urls)
    return items, urls


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------

def parse_sitemap(body: bytes, max_bytes: int) -> dict:
    """Parse a sitemaps.org urlset or sitemapindex (optionally gzip'd)."""
    if body[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(body)) as gz:
                body = gz.read(max_bytes + 1)
        except OSError as exc:
            return {"kind": "invalid", "error": f"bad gzip: {exc}", "locs": []}
        if len(body) > max_bytes:
            return {"kind": "invalid", "error": "sitemap exceeds size limit", "locs": []}
    if b"<!DOCTYPE" in body[:2000].upper() or b"<!ENTITY" in body[:5000].upper():
        return {"kind": "invalid", "error": "DTD/entities are not allowed in sitemaps", "locs": []}
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        return {"kind": "invalid", "error": f"XML parse error: {exc}", "locs": []}
    tag = root.tag.rsplit("}", 1)[-1].lower()
    kind = {"sitemapindex": "index", "urlset": "urlset"}.get(tag, "unknown")
    locs = []
    for child in root:
        loc = lastmod = None
        for sub in child:
            st = sub.tag.rsplit("}", 1)[-1].lower()
            if st == "loc" and sub.text:
                loc = sub.text.strip()
            elif st == "lastmod" and sub.text:
                lastmod = sub.text.strip()
        if loc:
            locs.append({"loc": loc, "lastmod": lastmod})
    return {"kind": kind, "locs": locs}


def same_site(url: str, host: str) -> bool:
    try:
        return urlsplit(url).netloc.lower() == host.lower()
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Loose text normalization (used only to pre-filter what is sent to the
# browser; the authoritative matcher lives in index.html)
# --------------------------------------------------------------------------

_SIZE_WORDS = {"small", "medium", "large", "regular", "kids", "family", "personal", "extra", "xl"}


def loose_norm(s: str) -> str:
    s = re.sub(r"['’`]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def prefilter_tokens(texts: list[str]) -> set[str]:
    toks = set()
    for t in texts:
        for w in loose_norm(t).split():
            if len(w) >= 3 and w not in _SIZE_WORDS and not w.isdigit():
                toks.add(w)
    return toks


def slug_tokens(s: str) -> list[str]:
    return loose_norm(s).split()


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))

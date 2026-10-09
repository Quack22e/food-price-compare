"""What real DoorDash store pages publish, read from public web archives.

DoorDash store pages answer datacenter IPs with a Cloudflare challenge (see
README), so the app's DoorDash store-page reader has never seen a real page
from the live runs. Public archives hold copies their own crawlers captured.
This script reads a few EXISTING captures (it never asks an archive to fetch
or save a page) and records which structured data they carry, using the app's
own parser (server.adapters.parse_store_page) and the structure probe.

Evidence only: archived prices are historical and are never used as current
prices. Raw pages go to tests/captures_archive/ (git-ignored, artifact only);
the JSON summary quotes only short snippets.

    python scripts/archive_probe.py --out live-verification
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.adapters import parse_store_page  # noqa: E402
from server.extract import dedupe_items, flag_zero_prices, normalize_us_address, parse_jsonld_blocks, scan_html  # noqa: E402
from server.http_client import utcnow_iso  # noqa: E402
from server.robots import rules_for_status  # noqa: E402
from server.structure_probe import probe_structure  # noqa: E402

UA = "FoodPriceCompare/1.0 (structure research; reads existing archive captures only)"
TOKEN = "FoodPriceCompare"
MIN_INTERVAL = 3.0
MAX_PAGES = 15           # captures read per archive
STORE_PATH = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?store/(?:(?P<slug>[^/]+?)-)?(?P<id>\d+)/?$", re.I)
CAPTURES = ROOT / "tests" / "captures_archive"


class Client:
    """Small polite reader for the archive hosts: robots.txt, one request per
    host every MIN_INTERVAL seconds, truthful User-Agent, no retries on 4xx."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last: dict[str, float] = {}
        self.robots: dict[str, object] = {}
        self.log: list[dict] = []

    def _wait(self, host):
        gap = time.time() - self.last.get(host, 0)
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self.last[host] = time.time()

    def allowed(self, url) -> bool:
        p = urlsplit(url)
        host = p.netloc
        if host not in self.robots:
            self._wait(host)
            try:
                r = self.s.get(f"{p.scheme}://{host}/robots.txt", timeout=30)
                self.robots[host] = rules_for_status(r.status_code, r.text, TOKEN)
                self.log.append({"robots": host, "status": r.status_code})
            except requests.RequestException as exc:
                self.robots[host] = rules_for_status(None, None, TOKEN)
                self.log.append({"robots": host, "error": repr(exc)})
        return self.robots[host].can_fetch(url)

    def get(self, url, **kw):
        if not self.allowed(url):
            self.log.append({"url": url, "skipped": "robots.txt disallows"})
            return None
        self._wait(urlsplit(url).netloc)
        try:
            r = self.s.get(url, timeout=60, **kw)
        except requests.RequestException as exc:
            self.log.append({"url": url, "error": repr(exc)})
            return None
        self.log.append({"url": url, "status": r.status_code, "bytes": len(r.content)})
        return r


def summarize(html: str, url: str) -> dict:
    """Run the app's own store-page parser on an archived page."""
    m = STORE_PATH.match(urlsplit(url).path)
    store_id = m.group("id") if m else ""
    parsed = parse_store_page(html, url, store_id)
    items = flag_zero_prices(dedupe_items(parsed["items"]))
    priced = [it for it in items if isinstance(it.get("price_cents"), int)]
    address, why = normalize_us_address(parsed["address_raw"]) if parsed["address_raw"] else (None, "no address")
    probe = probe_structure(html)
    return {
        "bytes": len(html),
        "challenge": bool(probe["challenge_markers"]) and not parsed["restaurant_found"],
        "challenge_markers": [c["marker"] for c in probe["challenge_markers"]][:5],
        "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, None])[1],
        "jsonld_types": parsed["jsonld_types"],
        "restaurant_found": parsed["restaurant_found"],
        "name": parsed["name"][:120] if parsed["name"] else None,
        "us_address": address and {k: address.get(k) for k in ("city", "state", "zip")},
        "address_problem": None if address else why,
        "menu_items": len(items),
        "priced_items": len(priced),
        "currencies": sorted({it.get("currency") or "?" for it in priced}),
        "sample_items": [{"name": it.get("name"), "price_cents": it.get("price_cents"), "currency": it.get("currency")}
                         for it in priced[:5]],
        "embedded_json": [{k: e.get(k) for k in ("id", "type", "bytes", "parsed", "price_paths", "menu_paths")}
                          for e in probe["embedded_json_scripts"][:8]],
        "counts": probe["counts"],
        "phrases": probe["phrases"],
        "price_contexts": probe["price_contexts"][:3],
    }


def save(name: str, html: str) -> None:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    (CAPTURES / name).write_text(html)


# Chains the live snapshots search on Uber Eats, so archived DoorDash pages of
# the same chains show how DoorDash publishes those items.
CHAIN_PREFIXES = ["taco-bell-", "mcdonalds-", "chipotle-mexican-grill-", "lou-malnatis-pizzeria-"]
PER_CHAIN = 3


def _cdx(c: Client, query: str, out: dict) -> list[dict]:
    r = None
    for wait in (0, 15, 45):                    # the CDX server answers 503 when busy
        time.sleep(wait)
        r = c.get(query)
        if r is None or r.status_code != 503:
            break
    rec = {"query": query, "status": r and r.status_code}
    got = []
    if r is not None and r.ok:
        try:
            data = r.json()
        except ValueError:
            data = []
        got = [dict(zip(data[0], row)) for row in data[1:]] if data else []
    rec["captures"] = len(got)
    out["queries"].append(rec)
    return got


def wayback(c: Client, known: list[str]) -> dict:
    out = {"source": "Wayback Machine (web.archive.org), existing captures, raw bytes via id_", "queries": [], "pages": []}
    fields = "&output=json&fl=timestamp,original,statuscode,mimetype,length&filter=statuscode:200&filter=mimetype:text/html"
    groups: dict[str, list[dict]] = {}
    for prefix in CHAIN_PREFIXES + [""]:
        q = (f"https://web.archive.org/cdx/search/cdx?url={quote('www.doordash.com/store/' + prefix, safe='')}"
             f"&matchType=prefix&from=2025{fields}&collapse=urlkey&limit=300")
        groups[prefix or "any"] = _cdx(c, q, out)
    if known:
        groups["seen in live runs"] = _cdx(c, f"https://web.archive.org/cdx/search/cdx?url={quote(known[0], safe='')}{fields}&limit=20", out)
    picks, seen = [], set()
    for name, rows in groups.items():
        newest: dict[str, dict] = {}
        for row in rows:
            m = STORE_PATH.match(urlsplit(row["original"]).path)
            if m and (m.group("id") not in newest or row["timestamp"] > newest[m.group("id")]["timestamp"]):
                newest[m.group("id")] = row
        out.setdefault("distinct_stores_with_captures", {})[name] = len(newest)
        for row in sorted(newest.values(), key=lambda r: r["timestamp"], reverse=True):
            sid = STORE_PATH.match(urlsplit(row["original"]).path).group("id")
            if sid in seen:
                continue
            if sum(1 for p in picks if p[0] == name) >= PER_CHAIN:
                break
            seen.add(sid)
            picks.append((name, sid, row))
    for name, sid, row in picks[:MAX_PAGES]:
        url = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        r = c.get(url)
        rec = {"group": name, "original": row["original"], "captured": row["timestamp"], "status": r and r.status_code}
        if r is not None and r.ok:
            html = r.content.decode(r.encoding or "utf-8", "replace")
            rec.update(summarize(html, row["original"]))
            rec["jsonld_shape"] = jsonld_shape(html, row["original"])
            save(f"wayback-{row['timestamp']}-{sid}.html", html)
        out["pages"].append(rec)
    return out


def _cut(o, n=700):
    t = json.dumps(o, ensure_ascii=False)
    return t if len(t) <= n else t[:n] + "…"


INTEREST = re.compile(r"crunchwrap supreme|chalupa supreme|burrito bowl|deep dish|mcnuggets", re.I)


def items_of_interest(blocks, html: str) -> list[dict]:
    """The JSON-LD MenuItems of the snapshot items, verbatim (short), with the
    price the page displays for the same name, to see how offers relate to the
    displayed price (e.g. a trailing "+" for a starting price)."""
    found = []

    def walk(o, depth=0):
        if depth > 14 or len(found) >= 6:
            return
        if isinstance(o, dict):
            if str(o.get("@type")) == "MenuItem" and INTEREST.search(str(o.get("name", ""))):
                name = str(o.get("name"))
                shown = re.findall(r'\\?"name\\?":\\?"' + re.escape(name).replace('"', '\\\\?"') +
                                   r'\\?".{0,600}?\\?"displayPrice\\?":\\?"([^"\\]{1,20})', html)[:3]
                found.append({"item": _cut(o, 600), "displayed": shown})
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    for b in blocks:
        walk(b)
    return found


def jsonld_shape(html: str, url: str) -> dict:
    """How the JSON-LD is laid out: top-level types and keys, the address
    object, where the menu sits, and the first items and offers verbatim
    (short), so the reader can be written from evidence."""
    blocks, _ = parse_jsonld_blocks(scan_html(html, url).jsonld_raw)
    shape = {"blocks": []}
    offers_total = offers_with_currency = 0
    currency_values = set()

    def walk(o, depth=0):
        nonlocal offers_total, offers_with_currency
        if depth > 12:
            return
        if isinstance(o, dict):
            if "offers" in o:
                for off in o["offers"] if isinstance(o["offers"], list) else [o["offers"]]:
                    offers_total += 1
                    if isinstance(off, dict) and off.get("priceCurrency"):
                        offers_with_currency += 1
                        currency_values.add(str(off["priceCurrency"]))
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    for b in blocks:
        for o in b if isinstance(b, list) else [b]:
            if not isinstance(o, dict):
                continue
            ent = {"type": o.get("@type"), "keys": sorted(o.keys())[:40]}
            if "address" in o:
                ent["address"] = _cut(o["address"], 400)
            menu = o.get("hasMenu") if "hasMenu" in o else (o if str(o.get("@type")) == "Menu" else None)
            if menu is not None:
                ent["menu_where"] = "Restaurant.hasMenu" if "hasMenu" in o else "top-level Menu block"
                m0 = menu[0] if isinstance(menu, list) and menu else menu
                if isinstance(m0, dict):
                    ent["menu_keys"] = sorted(m0.keys())
                    secs = m0.get("hasMenuSection") or []
                    secs = secs if isinstance(secs, list) else [secs]
                    ent["section_nesting"] = "list of lists" if secs and isinstance(secs[0], list) else "list"
                    while secs and isinstance(secs[0], list):          # DoorDash nests the section list
                        secs = secs[0]
                    ent["sections"] = len(secs)
                    s0 = secs[0] if secs and isinstance(secs[0], dict) else {}
                    its = s0.get("hasMenuItem") or []
                    its = its if isinstance(its, list) else [its]
                    ent["first_section_keys"] = sorted(s0.keys())
                    ent["first_items"] = [_cut(i, 500) for i in its[:2]]
                else:
                    ent["menu_value"] = _cut(menu, 200)
            for k in ("url", "@id", "name", "servesCuisine", "priceRange", "currenciesAccepted"):
                if k in o:
                    ent[k] = _cut(o[k], 160)
            shape["blocks"].append(ent)
            walk(o)
    shape["items_of_interest"] = items_of_interest(blocks, html)
    shape["offers"] = offers_total
    shape["offers_with_priceCurrency"] = offers_with_currency
    shape["priceCurrency_values"] = sorted(currency_values)
    low = html.lower()
    for ph in ('"currency"', "currencycode", "usd"):
        i = low.find(ph)
        shape.setdefault("currency_mentions", {})[ph] = None if i < 0 else re.sub(r"\s+", " ", html[max(0, i - 80): i + 80])
    return shape


def _warc_body(raw: bytes) -> tuple[int | None, dict, bytes]:
    data = gzip.decompress(raw)
    _, _, rest = data.partition(b"\r\n\r\n")             # WARC headers
    head, _, body = rest.partition(b"\r\n\r\n")          # HTTP headers
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else None
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    enc = headers.get("content-encoding", "")
    if "gzip" in enc:
        body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
    return status, headers, body


def common_crawl(c: Client) -> dict:
    out = {"source": "Common Crawl (index.commoncrawl.org + data.commoncrawl.org), existing WARC records", "indexes": [],
           "pages": []}
    r = c.get("https://index.commoncrawl.org/collinfo.json")
    if r is None or not r.ok:
        out["error"] = f"collinfo.json: {r and r.status_code}"
        return out
    rows = []
    for coll in r.json()[:3]:
        api = coll["cdx-api"]
        q = f"{api}?url={quote('www.doordash.com/store/*', safe='*')}&output=json&limit=60&filter=status:200"
        rr = c.get(q)
        lines = [json.loads(ln) for ln in (rr.text.splitlines() if rr is not None and rr.ok else []) if ln.strip()]
        out["indexes"].append({"id": coll["id"], "status": rr and rr.status_code, "records": len(lines)})
        rows += [dict(ln, index=coll["id"]) for ln in lines if "html" in ln.get("mime", "") or "html" in ln.get("mime-detected", "")]
        if len(rows) >= MAX_PAGES * 2:
            break
    seen = set()
    for row in sorted(rows, key=lambda x: x.get("timestamp", ""), reverse=True):
        m = STORE_PATH.match(urlsplit(row["url"]).path)
        if not m or m.group("id") in seen:
            continue
        seen.add(m.group("id"))
        if len(out["pages"]) >= MAX_PAGES:
            break
        off, ln = int(row["offset"]), int(row["length"])
        rr = c.get("https://data.commoncrawl.org/" + row["filename"], headers={"Range": f"bytes={off}-{off + ln - 1}"})
        rec = {"original": row["url"], "captured": row.get("timestamp"), "index": row["index"], "status": rr and rr.status_code}
        if rr is not None and rr.status_code in (200, 206):
            try:
                status, headers, body = _warc_body(rr.content)
                html = body.decode("utf-8", "replace")
                rec["http_status"] = status
                rec.update(summarize(html, row["url"]))
                save(f"commoncrawl-{row.get('timestamp')}-{m.group('id')}.html", html)
            except Exception as exc:  # recorded, never hidden
                rec["error"] = repr(exc)
        out["pages"].append(rec)
    return out


def known_store_urls() -> list[str]:
    try:
        rep = (ROOT / "live-verification" / "report.json").read_text()
    except OSError:
        return []
    urls = sorted(set(re.findall(r"https://www\.doordash\.com/store/[^\"\\ ?]+", rep)))
    return [u.replace("https://", "") for u in urls]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="live-verification")
    args = ap.parse_args()
    c = Client()
    rep = {"started_at": utcnow_iso(), "user_agent": UA,
           "note": "Structure evidence only. Archived prices are historical and are not used as current prices."}
    for name, fn in (("wayback", lambda: wayback(c, known_store_urls())), ("common_crawl", lambda: common_crawl(c))):
        try:
            rep[name] = fn()
        except Exception as exc:  # recorded, never hidden
            rep[name] = {"error": repr(exc)}
    rep["requests"] = c.log
    rep["finished_at"] = utcnow_iso()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "doordash-archive.json").write_text(json.dumps(rep, indent=1, default=str))
    for name in ("wayback", "common_crawl"):
        part = rep.get(name) or {}
        print(name, json.dumps([{k: p.get(k) for k in ("original", "captured", "status", "challenge", "jsonld_types",
                                                         "restaurant_found", "menu_items", "priced_items")}
                                for p in part.get("pages", [])], indent=1), part.get("error"))


if __name__ == "__main__":
    main()

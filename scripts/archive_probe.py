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
from server.extract import flag_zero_prices, dedupe_items, normalize_us_address  # noqa: E402
from server.http_client import utcnow_iso  # noqa: E402
from server.robots import rules_for_status  # noqa: E402
from server.structure_probe import probe_structure  # noqa: E402

UA = "FoodPriceCompare/1.0 (structure research; reads existing archive captures only)"
TOKEN = "FoodPriceCompare"
MIN_INTERVAL = 3.0
MAX_PAGES = 8            # captures read per archive
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


def wayback(c: Client, known: list[str]) -> dict:
    out = {"source": "Wayback Machine (web.archive.org), existing captures, raw bytes via id_", "queries": [], "pages": []}
    rows = []
    queries = [f"https://web.archive.org/cdx/search/cdx?url={quote('www.doordash.com/store/', safe='')}"
               "&matchType=prefix&from=2025&output=json&fl=timestamp,original,statuscode,mimetype,length"
               "&filter=statuscode:200&filter=mimetype:text/html&collapse=urlkey&limit=400"]
    for u in known[:6]:
        queries.append(f"https://web.archive.org/cdx/search/cdx?url={quote(u, safe='')}&output=json"
                       "&fl=timestamp,original,statuscode,mimetype,length&filter=statuscode:200&limit=20")
    for q in queries:
        r = c.get(q)
        rec = {"query": q, "status": r and r.status_code}
        if r is not None and r.ok:
            try:
                data = r.json()
            except ValueError:
                data = []
            got = [dict(zip(data[0], row)) for row in data[1:]] if data else []
            rec["captures"] = len(got)
            rows += got
        out["queries"].append(rec)
    by_url: dict[str, dict] = {}
    for row in rows:
        path = urlsplit(row["original"]).path
        if not STORE_PATH.match(path):
            continue
        key = STORE_PATH.match(path).group("id")
        if key not in by_url or row["timestamp"] > by_url[key]["timestamp"]:
            by_url[key] = row
    picks = sorted(by_url.values(), key=lambda r: r["timestamp"], reverse=True)[:MAX_PAGES]
    out["distinct_stores_with_captures"] = len(by_url)
    for row in picks:
        url = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        r = c.get(url)
        rec = {"original": row["original"], "captured": row["timestamp"], "status": r and r.status_code}
        if r is not None and r.ok:
            html = r.content.decode(r.encoding or "utf-8", "replace")
            rec.update(summarize(html, row["original"]))
            save(f"wayback-{row['timestamp']}-{STORE_PATH.match(urlsplit(row['original']).path).group('id')}.html", html)
        out["pages"].append(rec)
    return out


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

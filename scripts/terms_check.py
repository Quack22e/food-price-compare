"""What each platform's own terms say about scripts and scraping, read from
the published terms pages (or, where a page cannot be read, the newest
existing Wayback Machine capture of it). Quotes are verbatim sentences that
mention scraping, crawling, robots, spiders, scripts or data mining, so the
README's summary of each platform's terms can be checked against the source.

Same politeness as scripts/archive_probe.py: robots.txt (with Crawl-delay),
one request per host every 3 s or longer, Retry-After honoured, redirects only
to the listed hosts. Nothing is logged in to; no form is submitted.

    python scripts/terms_check.py --out live-verification
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from archive_probe import Client, status_of  # noqa: E402
from server.http_client import utcnow_iso  # noqa: E402

UA = "FoodPriceCompare/1.0 (terms check; reads published terms pages)"
TERMS = [
    {"platform": "ubereats", "name": "Uber U.S. Terms of Use",
     "url": "https://www.uber.com/legal/en/document/?name=general-terms-of-use&country=united-states&lang=en"},
    {"platform": "doordash", "name": "DoorDash Consumer Terms and Conditions (US)",
     "url": "https://help.doordash.com/legal/document?type=cx-terms-and-conditions&region=US&locale=en-US"},
    {"platform": "grubhub", "name": "Grubhub Terms of Use",
     "url": "https://www.grubhub.com/legal/terms-of-use"},
]
HOSTS = {"www.uber.com", "help.doordash.com", "www.grubhub.com", "web.archive.org"}
KEYWORDS = re.compile(r"scrap|data[\s-]?min|spider|\brobots?\b|crawl|\bscripts?\b|automated (?:means|process|system|device|access)", re.I)
DATE = re.compile(r"(?:effective|last (?:modified|updated|revised))[^.]{0,40}?\b((?:January|February|March|April|May|June|July|"
                  r"August|September|October|November|December) \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})", re.I)


def page_text(raw: str) -> str:
    """Visible text, plus text held in the page's JSON data (escaped HTML)."""
    t = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&").replace('\\"', '"').replace("\\n", " ")
    t = re.sub(r"(?is)<style\b.*?</style>", " ", t)
    # Script code goes; JSON data blocks stay (some terms pages ship their text that way).
    t = re.sub(r"(?is)<script\b([^>]*)>(.*?)</script>", lambda m: " " + m.group(2) + " " if "json" in m.group(1).lower() else " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def quotes(text: str, limit: int = 8) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.;])\s+", text):
        sent = re.sub(r'^[^\w(“]*(?:\w+"\s*:\s*")?\s*', "", sent).strip()     # also a JSON key before the text
        if KEYWORDS.search(sent) and 40 <= len(sent) <= 900 and sent not in out:
            out.append(sent)
        if len(out) >= limit:
            break
    return out


def read(c: Client, url: str) -> dict:
    r = c.get(url)
    rec = {"url": url, "status": status_of(r)}
    if r is not None and r.ok:
        text = page_text(r.text)
        rec.update(chars=len(text), quotes=quotes(text), dates=sorted(set(m.group(1) for m in DATE.finditer(text)))[:5])
    return rec


def newest_capture(c: Client, url: str) -> str | None:
    r = c.get("https://web.archive.org/cdx/search/cdx?url=" + quote(url, safe="") +
              "&output=json&filter=statuscode:200&limit=-1&fl=timestamp,original")
    try:
        rows = r.json() if r is not None and r.ok else []
    except ValueError:
        rows = []
    return f"https://web.archive.org/web/{rows[-1][0]}id_/{rows[-1][1]}" if len(rows) > 1 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="live-verification")
    args = ap.parse_args()
    c = Client(hosts=HOSTS, user_agent=UA)
    c.s.headers["Accept"] = "text/html,application/xhtml+xml"      # uber.com answered 406 to */*
    out = {"started_at": utcnow_iso(), "user_agent": UA, "terms": []}
    for t in TERMS:
        entry = dict(t, live=read(c, t["url"]))
        if not entry["live"].get("quotes"):           # unreadable here, or the text is drawn by scripts
            cap = newest_capture(c, t["url"])
            entry["archived"] = read(c, cap) if cap else {"note": "no Wayback Machine capture with HTTP 200"}
        out["terms"].append(entry)
    out["requests"] = c.log
    out["finished_at"] = utcnow_iso()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "terms.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps([{"platform": e["platform"], "live": e["live"].get("status"), "quotes": len(e["live"].get("quotes") or []),
                       "archived": (e.get("archived") or {}).get("status"),
                       "archived_quotes": len((e.get("archived") or {}).get("quotes") or [])} for e in out["terms"]], indent=1))


if __name__ == "__main__":
    main()

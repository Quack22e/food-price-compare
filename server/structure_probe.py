"""Structure discovery for real pages (evidence gathering, not extraction).

Summarises how a page publishes its data so extraction paths can be chosen
from observation: challenge markers with context, JSON-LD types, embedded
JSON scripts (parse method, key skeleton, price-like key paths with sample
values), and inline-script statistics. Output is bounded and contains short
snippets only, never the whole page.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote

from .http_client import STRONG_CHALLENGE_MARKERS, WEAK_CHALLENGE_MARKERS

PRICE_KEY_RE = re.compile(r"price|amount|cost", re.I)
MENU_KEY_RE = re.compile(r"menu|catalog|section|item", re.I)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict] = []
        self._cur = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            a = {k.lower(): (v or "") for k, v in attrs}
            self._cur = {"id": a.get("id", ""), "type": a.get("type", ""), "src": a.get("src", "")}
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            self._cur["text"] = "".join(self._buf)
            self.scripts.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._buf.append(data)


def _snip(text: str, i: int, width: int = 90) -> str:
    s = text[max(0, i - width): i + width]
    return re.sub(r"\s+", " ", s)


def _try_parse(text: str):
    t = text.strip()
    if not t:
        return None, None
    attempts = [("json", t)]
    if "%7B" in t[:200] or "%22" in t[:200]:
        attempts.append(("urldecoded", unquote(t)))
    if "\\u0022" in t[:500]:
        attempts.append(("unicode-unescaped", t.replace("\\u0022", '"')))
    m = re.match(r"^\s*(?:window\.)?[\w$.]+\s*=\s*(.+?);?\s*$", t, re.S)
    if m:
        attempts.append(("assignment", m.group(1)))
    for how, cand in attempts:
        try:
            return json.loads(cand), how
        except (json.JSONDecodeError, ValueError):
            continue
    return None, None


def _skeleton(o, depth=0, max_depth=3):
    if depth >= max_depth:
        return type(o).__name__
    if isinstance(o, dict):
        return {k: _skeleton(v, depth + 1, max_depth) for k, v in list(o.items())[:25]}
    if isinstance(o, list):
        return [_skeleton(o[0], depth + 1, max_depth), f"len={len(o)}"] if o else []
    return type(o).__name__


def _key_paths(o, pattern, path="", out=None, limit=25, depth=0):
    out = [] if out is None else out
    if len(out) >= limit or depth > 14:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}"
            if pattern.search(k) and not isinstance(v, (dict, list)):
                out.append({"path": re.sub(r"\[\d+\]", "[]", p), "sample": str(v)[:60]})
            elif pattern.search(k) and isinstance(v, dict):
                out.append({"path": re.sub(r"\[\d+\]", "[]", p), "sample": json.dumps(v)[:160]})
            _key_paths(v, pattern, p, out, limit, depth + 1)
    elif isinstance(o, list):
        for i, v in enumerate(o[:40]):
            _key_paths(v, pattern, f"{path}[{i}]", out, limit, depth + 1)
    # de-duplicate by normalised path
    seen, uniq = set(), []
    for e in out:
        if e["path"] not in seen:
            seen.add(e["path"])
            uniq.append(e)
    out[:] = uniq
    return out


def probe_structure(html: str) -> dict:
    lower = html.lower()
    markers = []
    for m in STRONG_CHALLENGE_MARKERS + WEAK_CHALLENGE_MARKERS:
        i = lower.find(m)
        if i >= 0:
            markers.append({"marker": m, "offset": i, "context": _snip(html, i, 70)})
    p = _Scripts()
    try:
        p.feed(html)
    except Exception:
        pass
    inline = [s for s in p.scripts if not s["src"]]
    embedded = []
    for s in inline:
        if not s["id"] and "json" not in s["type"]:
            continue
        data, how = _try_parse(s["text"])
        entry = {"id": s["id"], "type": s["type"], "bytes": len(s["text"]), "parsed": how}
        if data is None:
            entry["head"] = re.sub(r"\s+", " ", s["text"][:160])
        if data is not None and len(s["text"]) > 200:
            entry["skeleton"] = _skeleton(data)
            entry["price_paths"] = _key_paths(data, PRICE_KEY_RE)[:15]
            entry["menu_paths"] = [e["path"] for e in _key_paths(data, MENU_KEY_RE, limit=40)][:15]
        embedded.append(entry)
    anon = sorted((s for s in inline if not s["id"] and "json" not in s["type"]), key=lambda s: -len(s["text"]))
    price_ctx = []
    for pat in (r'\\?"(?:displayPrice|price|unitPrice|priceTagline)\\?"\s*:', r"\$\d+\.\d{2}"):
        for m in list(re.finditer(pat, html))[:3]:
            price_ctx.append({"pattern": pat, "context": _snip(html, m.start(), 110)})
    key_ctx = {}
    for key in ("storeId", "storeUuid", "store_id", "menuId", "hasCustomizations", "customizationIds", "isAvailable",
                "isSoldOut", "soldOut", "itemPrice", "priceTagline", "currencyCode", "address", "streetAddress", "displayPrice"):
        hits = [m.start() for m in re.finditer(r'\\?"' + key + r'\\?"\s*:', html)][:2]
        if hits:
            key_ctx[key] = [_snip(html, i, 100) for i in hits]
    return {
        "key_contexts": key_ctx,
        "bytes": len(html),
        "challenge_markers": markers[:10],
        "embedded_json_scripts": embedded[:30],
        "inline_scripts": {"count": len(inline), "anonymous_count": len(anon),
                           "largest_anonymous": [{"bytes": len(s["text"]), "head": re.sub(r"\s+", " ", s["text"][:120])}
                                                 for s in anon[:5]]},
        "counts": {k: html.count(k) for k in ('"price"', '\\"price\\"', "displayPrice", "__next_f", "__APOLLO_STATE__",
                                               "__REACT_QUERY_STATE__", "application/ld+json", "MenuItem", "storeUuid")},
        "price_contexts": price_ctx[:6],
    }

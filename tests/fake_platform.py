"""Local stand-in HTTP servers for pipeline tests.

SYNTHETIC FIXTURES: every page here was written for the tests. They follow
the public schema.org / sitemaps.org standards the adapters parse; they are
NOT copies of real DoorDash / Uber Eats / Grubhub pages and prove nothing
about the live sites' current markup. They exist to exercise the pipeline
mechanics (robots, discovery, retries, barriers, cancellation, resume).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSite:
    def __init__(self):
        self.routes: dict[str, dict] = {}
        self.hits: dict[str, int] = {}
        self.hit_times: dict[str, list] = {}
        self._lock = threading.Lock()
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path
                with site._lock:
                    site.hits[path] = site.hits.get(path, 0) + 1
                    site.hit_times.setdefault(path, []).append(time.monotonic())
                    n = site.hits[path]
                r = site.routes.get(path) or site.routes.get(path.split("?")[0])
                if r is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if callable(r):
                    r = r(n)
                if r.get("delay"):
                    time.sleep(r["delay"])
                body = r.get("body", "")
                body = body.encode() if isinstance(body, str) else body
                self.send_response(r.get("status", 200))
                for k, v in (r.get("headers") or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", r.get("ctype", "text/html; charset=utf-8"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def route(self, path, body="", status=200, ctype=None, headers=None, delay=None):
        self.routes[path] = {"body": body, "status": status, "headers": headers or {}, "delay": delay,
                             **({"ctype": ctype} if ctype else {})}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def restaurant_page(name, url, street, city, region, zip_code, items, country="US", extra_head=""):
    """items: list of (section, name, price|None, currency, availability|None, extra_dict)"""
    sections = {}
    for sec, iname, price, cur, avail, extra in items:
        offer = {"@type": "Offer", "priceCurrency": cur}
        if price is not None:
            offer["price"] = price
        if avail:
            offer["availability"] = f"https://schema.org/{avail}"
        mi = {"@type": "MenuItem", "name": iname, "offers": offer}
        mi.update(extra or {})
        sections.setdefault(sec, []).append(mi)
    data = {
        "@context": "https://schema.org", "@type": "Restaurant", "name": name, "url": url,
        "address": {"@type": "PostalAddress", "streetAddress": street, "addressLocality": city,
                    "addressRegion": region, "postalCode": zip_code, "addressCountry": country},
        "hasMenu": {"@type": "Menu", "hasMenuSection": [
            {"@type": "MenuSection", "name": s, "hasMenuItem": its} for s, its in sections.items()]},
    }
    return (f"<!doctype html><html><head><title>{name}</title>{extra_head}"
            f"<script type=\"application/ld+json\">{json.dumps(data)}</script></head>"
            f"<body><h1>{name}</h1></body></html>")


def sitemap_index(urls):
    inner = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{inner}</sitemapindex>'


def urlset(urls):
    inner = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{inner}</urlset>'


NUGGETS = [
    ("Chicken", "10 pc. Chicken McNuggets®", "5.49", "USD", "InStock", None),
    ("Chicken", "20 pc. Chicken McNuggets®", "9.99", "USD", "InStock", None),
    ("Burgers", "Big Mac®", "6.39", "USD", None, None),
    ("Sides", "Fries", "3.89", "USD", None, {"offers": [
        {"@type": "Offer", "name": "Small", "price": "2.79", "priceCurrency": "USD"},
        {"@type": "Offer", "name": "Large", "price": "4.19", "priceCurrency": "USD"}]}),
]


def build_doordash(site: FakeSite, n_stores=4, blocked=False, slow=None):
    """A DoorDash-shaped synthetic site: robots -> sitemap index -> urlset -> stores."""
    b = site.base
    site.route("/robots.txt", f"User-agent: *\nDisallow: /consumer/\nAllow: /store/\nSitemap: {b}/sitemap_index.xml\n", ctype="text/plain")
    store_urls = [f"{b}/store/mcdonalds-city{i}-{1000 + i}/" for i in range(n_stores)]
    other = [f"{b}/store/burger-king-city0-9001/"]
    site.route("/sitemap_index.xml", sitemap_index([f"{b}/sitemap_misc.xml", f"{b}/sitemap_stores_1.xml"]), ctype="application/xml")
    site.route("/sitemap_misc.xml", urlset([f"{b}/about"]), ctype="application/xml")
    site.route("/sitemap_stores_1.xml", urlset(store_urls + other), ctype="application/xml")
    cities = [("1 Main St", "Springfield", "IL", "62701"), ("2 Oak Ave", "Austin", "TX", "78701"),
              ("3 Pine Rd", "Portland", "OR", "97201"), ("4 Elm St", "Denver", "CO", "80202"),
              ("5 Bay St", "Boston", "MA", "02108"), ("6 Lake Dr", "Madison", "WI", "53703")]
    for i, u in enumerate(store_urls):
        path = u[len(b):]
        if blocked:
            site.route(path, "<html><title>Access denied</title></html>", status=403)
            continue
        street, city, st, z = cities[i % len(cities)]
        items = [(s, n, (f"{float(p) + 0.10 * i:.2f}" if p else p), c, a, e) for s, n, p, c, a, e in NUGGETS]
        site.route(path, restaurant_page("McDonald's", u, street, city, st, z, items), delay=slow)
    site.route("/store/burger-king-city0-9001/", restaurant_page("Burger King", other[0], "9 X St", "Springfield", "IL", "62701",
                                                                [("Chicken", "10 pc. Chicken Nuggets", "4.00", "USD", None, None)]))
    return store_urls


def build_ubereats(site: FakeSite):
    b = site.base
    site.route("/robots.txt", f"User-agent: *\nDisallow: /_p/\nCrawl-delay: 0\nSitemap: {b}/sitemap.xml\n", ctype="text/plain")
    urls = {
        "us1": f"{b}/store/mcdonalds-main-st/AbCdEfGh1234",
        "us2": f"{b}/store/mcdonalds-broadway/ZzYyXx987654",
        "ca": f"{b}/ca/store/mcdonalds-toronto/CaNaDa000111",
        "nomenu": f"{b}/store/mcdonalds-js-only/NoMenu555555",
        "dupe": f"{b}/store/mcdonalds-main-street-2/DuPe00000001",
    }
    site.route("/sitemap.xml", urlset([urls["us1"], urls["ca"], urls["nomenu"]]), ctype="application/xml")
    site.route("/brand/mcdonalds", "<html><body>" + "".join(
        f'<a href="{u}">store</a>' for u in (urls["us2"], urls["dupe"])) + '<a href="/brand/mcdonalds?page=2" rel="next">next</a></body></html>')
    site.route("/brand/mcdonalds?page=2", "<html><body>no more</body></html>")
    site.route("/store/mcdonalds-main-st/AbCdEfGh1234", restaurant_page(
        "McDonald's (Main St)", urls["us1"], "100 Main St", "Columbus", "OH", "43215",
        [("Chicken", "Chicken McNuggets", "5.29", "USD", "InStock", {"offers": [
            {"@type": "Offer", "name": "10 pc", "price": "5.29", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
            {"@type": "Offer", "name": "20 pc", "price": "9.49", "priceCurrency": "USD"}]})]))
    site.route("/store/mcdonalds-broadway/ZzYyXx987654", restaurant_page(
        "McDonald's", urls["us2"], "200 Broadway", "New York", "New York", "10007",
        [("Chicken", "10 pc Chicken McNuggets", "6.99", "USD", "OutOfStock", None)]))
    site.route("/store/mcdonalds-main-street-2/DuPe00000001", restaurant_page(
        "McDonald's", urls["dupe"], "100 Main St", "Columbus", "OH", "43215",
        [("Chicken", "10 pc Chicken McNuggets", "1.00", "USD", None, None)]))
    site.route("/ca/store/mcdonalds-toronto/CaNaDa000111", restaurant_page(
        "McDonald's", urls["ca"], "1 King St", "Toronto", "ON", "M5H 1A1",
        [("Chicken", "10 pc Chicken McNuggets", "4.00", "CAD", None, None)], country="CA"))
    site.route("/store/mcdonalds-js-only/NoMenu555555", "<html><head><title>McDonald's</title></head><body><div id=root></div></body></html>")
    return urls


def build_menupages(mp, gh_id="434510", layout="exact"):
    """SYNTHETIC MenuPages-shaped site (shape observed on real MenuPages pages, 2026-10-08).

    layout="midway" mirrors the live listing: the sitemap's exact-slug URL is
    not the page that links to Grubhub; a same-address "-midway" page is, and a
    same-address decoy links to a different Grubhub restaurant.
    """
    import gzip as _gz
    import json
    b = mp.base
    mp.route("/robots.txt", f"User-agent: *\nDisallow: /*/*/checkout\nSitemap: {b}/sitemap-index.xml\n", ctype="text/plain")
    mp.route("/sitemap-index.xml", '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             f'<sitemap><loc>{b}/sitemap-restaurants.xml.gz</loc></sitemap><sitemap><loc>{b}/sitemap-states.xml</loc></sitemap></sitemapindex>',
             ctype="application/xml")
    urls = [f"{b}/planet-pizza/411-main-st-ridgefield", f"{b}/planet-pizza/6456-s-cicero-ave-chicago",
            f"{b}/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago"]
    if layout == "midway":
        urls += [f"{b}/lou-malnatis-pizzeria-express/6456-s-cicero-ave-chicago", f"{b}/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago"]
    xml = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
    mp.routes["/sitemap-restaurants.xml.gz"] = {"body": _gz.compress(xml.encode()), "status": 200, "headers": {}, "delay": None, "ctype": "application/x-gzip"}
    rest = {"@context": "https://schema.org", "@type": "Restaurant", "name": "Lou Malnati&#39;s Pizzeria",
            "address": {"@type": "PostalAddress", "streetAddress": "6456 S Cicero Ave", "addressLocality": "Chicago", "addressRegion": "IL", "postalCode": "60638", "addressCountry": "US"},
            "hasMenu": f"{b}/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago"}
    menu = {"@context": "https://schema.org", "@type": "Menu", "hasMenuSection": [{"@type": "MenuSection", "name": "Deep Dish", "hasMenuItem": [
        {"@type": "MenuItem", "name": "Small - Deep Dish Margherita", "offers": {"@type": "Offer", "price": "21.55", "priceCurrency": "USD"}},
        {"@type": "MenuItem", "name": "Personal - Deep Dish Margherita", "offers": {"@type": "Offer", "price": "14.25", "priceCurrency": "USD"}}]}]}
    def page(linked_id):
        return ("<html><head><title>Lou</title>"
                f"<script type=\"application/ld+json\" id=\"restaurantStructuredData\">{json.dumps(rest)}</script>"
                f"<script type=\"application/ld+json\" id=\"menuStructuredData\">{json.dumps(menu)}</script></head><body>"
                f"<a href=\"https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/{linked_id}\">Order on Grubhub</a></body></html>")
    mp.route("/planet-pizza/6456-s-cicero-ave-chicago", page("111111"))  # same address, other name: never a candidate
    if layout == "midway":
        mp.route("/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago", "<html><title>Not found</title></html>", status=404)
        mp.route("/lou-malnatis-pizzeria-express/6456-s-cicero-ave-chicago", page("222222"))
        mp.route("/lou-malnatis-pizzeria-midway/6456-s-cicero-ave-chicago", page(gh_id))
    else:
        mp.route("/lou-malnatis-pizzeria/6456-s-cicero-ave-chicago", page(gh_id))


def allmenus_page(gh_id="517712", items=None, ld_items=None, title=None, desc=None):
    """SYNTHETIC AllMenus-shaped menu page (markup observed on a real AllMenus page, 2026-10-08)."""
    import json
    items = items if items is not None else [
        ("BuffaLou Chicken Pizza", "BuffaLou Chicken Deep Dish - GF", "$27.15"),
        ("BuffaLou Chicken - Deep Dish", "Personal - Deep Dish BuffaLou Chicken", "$15.85"),
        ("Deep Dish Margherita", "Small - Deep Dish Margherita", "$21.95"),
        ("Deep Dish Margherita", "Large - Deep Dish Margherita", "$0.00"),
        ("Drinks", "Fountain Drink", "Market Price"),
    ]
    ld_items = ld_items if ld_items is not None else [n for _, n, _ in items]
    sections = {}
    for sec, name, _ in items:
        sections.setdefault(sec, [])
    ld = {"@context": "http://schema.org", "@type": "Restaurant",
          "address": {"@type": "PostalAddress", "addressLocality": "", "addressRegion": "", "postalCode": "", "streetAddress": ""},
          "name": "", "hasMenu": {"@type": "Menu", "hasMenuSection": [{"@type": "MenuSection", "name": "Menu",
                                  "hasMenuItem": [{"@type": "MenuItem", "name": n, "description": "Line one\tline two"} for n in ld_items]}]}}
    ld_text = json.dumps(ld).replace("\\t", "\t")  # raw control character, as observed
    body = []
    for sec in dict.fromkeys(s for s, _, _ in items):
        lis = "".join(f'<ul class="menu-items-list unordered-list"><li class="menu-items"><div class="h6 item-main">'
                      f'<span class="item-title">{n}</span> <span class="item-price"> {p} </span></div>'
                      f'<p class="description">About {n}.</p></li></ul>' for s, n, p in items if s == sec)
        body.append(f'<ul class="unordered-list"><li class="menu-category"><div class="h4 category-name menu-section-title">{sec}</div>'
                    f'<div class="category-description">Section text.</div>{lis}</li></ul>')
    title = title or "Lou Malnati&#39;s Pizzeria menu - Buffalo Grove IL 60089 - (847) 215-7100"
    desc = desc or "Restaurant menu, map for Lou Malnati&#39;s Pizzeria located in 60089, Buffalo Grove IL, 85 S Buffalo Grove Rd."
    return (f'<!DOCTYPE html><html lang="en"><head><title>{title}</title><meta name="description" content="{desc}" />'
            f'<script type="application/ld+json">{ld_text}</script></head><body>'
            f'<div class="menu-container menu-nr1 active">{"".join(body)}</div>'
            f'<a href="https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/{gh_id}?utm_source=allmenus.com">Order online</a>'
            f'</body></html>')

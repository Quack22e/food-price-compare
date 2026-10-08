# Food Price Compare (US)

Enter a Grubhub item or order. The app looks for the **same item** (same chain, product, size, flavor, piece count, modifiers and quantity) on **DoorDash** and **Uber Eats** at branches anywhere in the 50 US states and DC. It then reports the lowest **verified** price among the locations it actually checked.

* `index.html` is the whole frontend: one file with embedded CSS and vanilla JS, and no build step.
* `server/` is a small Python companion service that does the scraping. A browser page cannot read these sites cross-origin, and the service keeps request policy and limits server-side.

> **Status of live scraping, in short.** The scrapers are implemented and tested end to end over HTTP against local fixtures. A live run from the build environment was attempted, but its network egress policy denies `www.doordash.com`, `www.ubereats.com` and `www.grubhub.com`. Both plain HTTP and real Chromium were refused, so no real page from these sites was observed during the build. See [Verification results](#verification-results) and [Access limitations](#access-limitations-and-dependencies). Nothing in this repository contains real or invented prices.

---

## Setup

Python 3.10+.

```bash
cd food-price-compare
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                 # requests

# Optional: enables the "Browser render" and "Auto" fetch methods
pip install playwright && python -m playwright install chromium

python -m server.app                            # serves http://127.0.0.1:8765/
```

Open **http://127.0.0.1:8765/**. The backend serves `index.html` from the same origin.

You can also open `index.html` directly (`file://`) and point it at the backend with the URL box at the top right. Without a backend, manual entry, receipt parsing, imports, demo mode and export still work. The page shows a "Backend disconnected" state and disables live search.

Before relying on results, check that your machine can reach the platforms:

```bash
python -m server.inspect_page --probe            # robots.txt reachability + declared sitemaps
python -m server.inspect_page https://www.doordash.com/store/<slug>-<id>/   # what structured data a page publishes
python -m server.inspect_page --browser https://www.ubereats.com/store/<slug>/<id>

# Capture real pages, then let the test-suite verify the extractors against them
python -m server.inspect_page --save tests/captures https://www.doordash.com/store/<slug>-<id>/
python -m pytest -q tests/test_real_captures.py -s
```

## Using it

1. **Enter the order.** Use one of the three tabs:
   * **Grubhub link:** the backend fetches the page (robots.txt-checked) and reads schema.org data. If that fails, the chain is guessed from the URL slug and labeled as a guess, and you complete the rest by hand.
   * **Paste order / receipt:** parsed in the browser and never sent anywhere. Fee, tax, tip, total, promo and payment lines are ignored.
   * **Manual entry.**

   Every field stays editable: chain, item, size, flavor, piece count, quantity, modifiers (with Grubhub charges), and the **Grubhub item subtotal**. That subtotal is the line amount for that item, not the receipt total. Ambiguities and conflicts are flagged under each item.
2. **Scope.** The default is **Nationwide** with both platforms, the **Delivery** menu price, and USD only. **Filter by location** limits results to a state, city or ZIP. **Pickup** is a separate order mode, and the two modes are never mixed.
3. **Search live prices.** Progress shows, per platform: discovered, remaining, checked, matched (exact), needs confirmation, blocked, failed, no menu data, non-US, duplicates, unsearched, and sitemaps read. Results stream in as stores are checked. **Cancel** stops the run, and **Resume** continues any saved checkpoint, including one from before a server restart.
4. **Read the results:**
   * The **winner card** reads "Lowest verified price among *N* checked locations". It is never "cheapest in the US" unless exhaustive coverage is evidenced, which no live source currently provides. It shows savings against your Grubhub item subtotal, and notes that a distant branch may not deliver to you.
   * **Verified matches** is a sortable table. Every row has a **Why?** explanation, a source link, the observation time, the order mode and the price context.
   * **Needs confirmation:** same product name, but something required for an exact match is missing.
   * **Not equivalent:** look-alike listings, each with the reason.
   * **Same-branch baskets:** for multi-item orders, every item must be at one branch on one platform.
   * **Barriers & log.**
5. **Rank by** item subtotal (the default; before tax, fees, tip and promotions) or by **checkout total**. Checkout totals only exist where every destination-specific charge is known. Unknown charges are never treated as zero.
6. **Export** CSV or JSON. CSV cells are guarded against spreadsheet formula injection.

**Import** (Data sources panel) adds menus you collected yourself (JSON or CSV, schema below). Imported rows are labeled "Imported" and can be excluded with a checkbox. **Demo mode** is opt-in, shows a banner, uses a fictional chain, and switches the whole results view to demo data, which never mixes with real data.

## How live data is acquired

Each adapter (`server/adapters.py`) relies only on published standards and on what the page itself publishes:

| Step | Method | Evidence basis |
|---|---|---|
| Permission | `robots.txt` per RFC 9309 (wildcards, longest match, `Crawl-delay`). If robots.txt is unreachable (401/403/429/5xx or network failure), the site is treated as **disallowed**. A representative store path is tested before any crawl. | Standard |
| Branch discovery | (a) store or chain URLs you paste as seeds; (b) the **sitemaps the site declares in its own robots.txt**, read as sitemap index → urlsets, with gzip support and DTDs rejected; (c) store links on chain/brand pages, following `rel="next"` pagination. Store URLs are filtered by chain-name tokens in the slug. Seeds come first, then chain pages, then sitemaps. Store-, business- and brand-named sitemaps are read first. | Sitemaps.org protocol; HTML anchors |
| Menu + prices | schema.org **JSON-LD**: `Restaurant`/`FoodEstablishment` → `hasMenu` → `MenuSection` → `MenuItem` → `Offer` (also `priceSpecification`, `AggregateOffer` (a range, so never verified), `menuAddOn`, `availability`), and `PostalAddress` for the US check | schema.org standard. Third-party write-ups report this markup on DoorDash and Uber Eats store pages, but it was **not observed directly** (see limitations). |
| JS-rendered pages | **Auto** mode re-renders a page with Playwright/Chromium only when it answered normally but carried no structured data. A browser is never used to get past a block or challenge. | |
| URL shapes | DoorDash `/store/<slug>-<id>/` or `/store/<id>/`, chain pages `/business/<slug>-<id>/`. Uber Eats `/store/<slug>/<id>`, `/<cc>/store/…` (non-US country sections are skipped without a request), `/brand/…`. Mode parameters: DoorDash `?pickup=false|true`, Uber Eats `?diningMode=DELIVERY|PICKUP`. | Corroborated by 2026 third-party tool code and docs (see [Evidence log](#evidence-log)), except `diningMode`. **Not observed live from here.** Each observation records the mode basis. |

Pages without the standard markup produce an explicit **"no menu data"** outcome. The adapters do not guess selectors or call private endpoints.

**Politeness and robustness** (`server/http_client.py`, `server/jobs.py`):

* Each platform has a bounded worker pool (default 2), and each host has a minimum interval between requests (default 3 s; a larger `Crawl-delay` wins).
* Network errors, 429 and 5xx get up to 3 retries with exponential backoff and jitter. `Retry-After` is honored, in seconds or as an HTTP date. If it exceeds 300 s, the adapter stops instead of waiting.
* Barriers are classified and reported, never bypassed: `egress_denied`, `robots_disallow`, `captcha` (recognized challenge interstitials), `login_wall`, `forbidden`, `rate_limited`, `offsite_redirect`, `network`.
* Redirects are followed manually, and every hop must stay on an allowed platform host and pass robots.txt. A redirect to any other host is refused, so the service cannot be used for server-side request forgery.
* A circuit breaker stops an adapter after 4 consecutive blocked or failed stores. The other platform keeps going.
* Pages are cached on disk (6 h; sitemaps and robots.txt 24 h). A cached result keeps its **original** observation time, so staleness is computed correctly.
* Exact store-ID duplicates are merged. Two listings at the same street and ZIP are both kept, flagged, and counted once as a branch.
* Each run is bounded (max branches per platform, sitemap documents per run). Checkpoints are written after every store. Resume continues the queue and never refetches checked stores.
* The service only fetches the three platform hosts, which blocks its use as an open proxy.
* It identifies itself truthfully as `FoodPriceCompare/1.0`, with no stealth plugins, TLS impersonation, CAPTCHA solving, or third-party proxies.

Every observation records:
* platform and store ID, with source URL and fetched URL
* full US address
* observation timestamp (UTC), plus whether it came from the cache
* currency
* order mode and its basis
* price context ("anonymous session, no delivery address")
* extraction method
* availability
* fee fields, which stay `null` (unknown) for live data

### Coverage semantics

The universe of US branches on a platform is **not known**. Even after every declared sitemap has been read (shown as "All declared sitemaps were read"), sitemaps are not proof of the live marketplace. Results are therefore always worded "among N checked locations", and the coverage line says it is not proof of the cheapest price nationwide.

## Matching rules (in `index.html`, `FPCEngine`)

* **Chain:**
  * The store name is compared after stripping parenthetical or "– location" suffixes, case, accents, ® and ™, and punctuation.
  * A partial name (for example "Chipotle" vs "Chipotle Mexican Grill") needs confirmation, unless you add it as a confirmed alias.
  * A different chain is excluded.
* **Product:**
  * Normalization handles only harmless differences: case, punctuation, `&` and `+` vs "and", `'N'` vs "and", `w/` vs "with", and inch marks.
  * Product tokens must be identical once size and piece-count tokens are extracted. Extra or missing words mean **not equivalent**, because fuzzy names never prove equivalence.
  * Item aliases are used only when you enter them, and they are labeled as yours.
* **Size, count and flavor:**
  * Sizes are word sizes (small … extra large, kids, regular, tall/grande/venti, footlong), ounces and inches. Piece counts accept pc, piece, ct, pack and dozen.
  * The same attribute stated differently on each side means **not equivalent**. If either side doesn't state it, the match **needs confirmation**.
  * A flavor must appear in the listing name or as a listed add-on.
* **Modifiers:**
  * Each modifier must be listed with a known charge.
  * Removals ("No onions") that aren't listed need confirmation.
  * A mandatory option group needs a priced selection.
  * When the source doesn't say whether paid mandatory options exist, the row shows that caveat.
* **Money:** all amounts are integer cents. The line price is quantity × (base price + selected option charges). A missing charge means no verified price.
* **Ranking:** item subtotal before tax, fees, tip and promotions, within one order mode only. Promotions are shown separately with their eligibility conditions. Savings are computed against your Grubhub **item** subtotal.
* **Exclusions and staleness:**
  * Unavailable items are shown but excluded from winners.
  * Unknown availability is flagged.
  * Stale observations (default older than 24 h) are flagged and cannot win.
  * Non-US and non-USD data is excluded.
* **Baskets:** every item must be an exact, purchasable match at one branch on one platform. Different restaurants are never combined.

## Import schema

Files: `examples/import-example.json` and `examples/import-example.csv`. Both are format examples for a **fictional** chain, so they can never match a real order. Validation runs in the browser. Invalid records are reported and dropped. Untrusted text is only ever rendered as text.

JSON is an array of observations, or `{"observations": [...]}`:

| Field | Required | Rules |
|---|---|---|
| `platform` | yes | `doordash` or `ubereats` |
| `store_id`, `store_name` | yes | text |
| `store_url` | yes | `https://www.doordash.com/...` or `https://www.ubereats.com/...` (must match the platform) |
| `address.street/city/state/zip/country` | city, state, zip | 50 states + DC (codes or names); 5-digit ZIP; country `US` or omitted |
| `observed_at` | yes | ISO-8601; not in the future |
| `order_mode` | yes | `delivery` or `pickup` |
| `currency` | — | must be `USD` (default) |
| `mode_basis`, `price_context` | — | how the price was observed (e.g. "logged out, no address") |
| `items[]` | ≥1 | `name` (required), `variant`, `section`, `price` (`"8.99"`) or `price_cents`, `availability` (`available`/`unavailable`/`unknown`), `addons[]` `{name, group, price}`, `required_options` (`"none"`, `"unknown"`, or `[{name, required}]`), `promotions[]` `{description, price, conditions}` |
| `fees` | — | `delivery_fee`, `service_fee`, `small_order_fee`, `tax`, `destination_zip`. Used for checkout totals only when **all** are known *and* `destination_zip` is given. |

CSV has one row per menu item, and rows with the same `platform, store_id, order_mode, observed_at` form one observation. The columns are:

```
platform,store_id,store_name,store_url,street,city,state,zip,country,observed_at,order_mode,currency,
item_name,variant,price,availability,addons,required_options,section,promo_description,promo_price,
promo_conditions,delivery_fee,service_fee,small_order_fee,tax,fee_destination_zip,mode_basis,price_context
```

`addons` is written as `Group: Name=1.50; Name=0.00`. Store-level fee columns may appear on any row of the store, and conflicting values are rejected.

## API (`python -m server.app`, default `127.0.0.1:8765`)

| Method & path | Purpose |
|---|---|
| `GET /api/health` | capabilities, limits, user agent |
| `POST /api/probe` `{platforms}` | live robots.txt reachability, barrier, declared sitemaps |
| `POST /api/grubhub/extract` `{url}` | Grubhub URL → order fields (+ barriers / URL-only fallback) |
| `POST /api/inspect` `{url, browser}` | structured-data report for one platform page |
| `POST /api/jobs` | start a search: `{order:{chain, chain_aliases, items:[{name, aliases, flavor}]}, platforms, order_mode, fetch_mode: http/auto/browser, max_stores_per_platform, seed_urls, prioritize:{city,state}}` |
| `GET /api/jobs` | saved checkpoints |
| `GET /api/jobs/<id>?since=N` | summary + incremental events (`observation`, `store`, `barrier`, `platform`, `discovered`, `log`, `job`) |
| `POST /api/jobs/<id>/cancel`, `POST /api/jobs/<id>/resume`, `DELETE /api/jobs/<id>` | control |
| `POST /api/purge` | delete all checkpoints and cache |

Environment variables:

* `FPC_MIN_INTERVAL` (3.0)
* `FPC_CONCURRENCY` (2, max 4)
* `FPC_MAX_RETRIES` (3)
* `FPC_MAX_RETRY_AFTER` (300)
* `FPC_BREAKER` (4)
* `FPC_TIMEOUT` (25)
* `FPC_CACHE_TTL_PAGE` / `_SITEMAP` / `_ROBOTS`
* `FPC_MAX_STORES_CAP` (500)
* `FPC_MAX_SITEMAPS_PER_RUN` (60)
* `FPC_DATA_DIR` (`./.fpc-data`)
* `FPC_USER_AGENT`

CORS is limited to `null` (file://) and localhost origins.

**Privacy.** Receipts are parsed in the browser only. The backend stores only:
* chain and item names, aliases and flavor (needed to discover branches)
* discovered store URLs and their status
* public menu observations

It never stores receipts, Grubhub prices, quantities, delivery addresses or location filters. The browser keeps only the backend URL in `localStorage`. **Delete server cache & checkpoints** wipes the server side.

## Verification results

All of these were run in the build environment on 2026-10-08:

| Suite | Command | Result |
|---|---|---|
| Backend (pytest) | `python -m pytest -q` | **20 passed, 1 skipped**. The skipped test is `test_real_captures.py`: there are no real page captures because the platforms were unreachable. |
| Engine (Node, runs the exact `<script id="fpc-engine">` from index.html) | `node --test tests/engine.test.mjs` | **20 passed** |
| UI end-to-end (Playwright + Chromium, real backend, local fixture sites) | `node --test tests/ui.e2e.mjs` | **18 passed** |

The pipeline tests use **synthetic local fixture servers** (`tests/fake_platform.py`). Those pages were written for the tests to the schema.org and sitemaps standards. They exercise the mechanics and prove nothing about the live sites' current markup. What they cover:

* **Discovery and dedup:** robots.txt gating (including `Disallow: /`) and Crawl-delay spacing; sitemap index → urlset discovery with chain filtering; chain pages with `rel=next` pagination; non-US country sections skipped; no-menu pages; duplicate addresses.
* **Retries and barriers:** retry with Retry-After (gap measured ≥ 1 s); over-long Retry-After stops the adapter; CAPTCHA, login wall, 403, 5xx exhaustion; off-site redirect refused (SSRF).
* **Fetch modes:** cache reuse with the original timestamp; Playwright rendering of a page whose JSON-LD is injected by JavaScript; pickup vs delivery URLs.
* **Run control:** per-run limit then resume; cancel mid-run, then resume after a simulated server restart, with nothing refetched and nothing lost; circuit breaker on one platform while the other completes.
* **Grubhub and the API:** Grubhub extraction and its robots-blocked fallback; HTTP API, CORS and purge.

The engine tests cover:

* integer-cent parsing that rejects float noise, comma decimals and more than two decimals
* size, piece-count and flavor matches and mismatches
* add-ons, removals and mandatory options; quantity × configured price; missing charges
* the USD-only rule; pickup/delivery separation
* stale data, unavailable items, non-US and other-chain exclusion; the location filter
* duplicate handling; same-branch baskets (never combined)
* checkout totals with unknown fees; import validation (JSON/CSV, including injection attempts); CSV formula-injection guarding
* receipt parsing; Grubhub URL parsing; the example files

The UI test clicks through every control: receipt paste, Grubhub extract, search, sort, Why?, tabs, cancel and resume, both exports, import (including markup in store names rendered as inert text), pickup/delivery, location filter, checkout ranking, multi-item baskets, adding and removing modifiers and items, demo isolation, probe, checkpoint delete, purge, and a disconnected backend. It also asserts there were no page errors.

**Live retrieval attempts** were made against the real sites through the real service, not test mode:

| Request | Result |
|---|---|
| `GET https://www.doordash.com/robots.txt` (job + probe) | **Blocked**: `egress_denied`, the build environment's proxy refused CONNECT (HTTP 403) |
| `GET https://www.ubereats.com/robots.txt` (job + probe) | **Blocked**: `egress_denied` (same) |
| Grubhub extract for `https://www.grubhub.com/restaurant/…?menuItemId=…` | **Blocked**: `egress_denied`; UI fell back to URL-derived chain guess + manual entry |
| Chromium (Playwright) navigation to all three homepages | **Blocked**: `net::ERR_TUNNEL_CONNECTION_FAILED` |
| Server-side web fetch tool on the three robots.txt files, and on third-party scraper write-ups | **Blocked** by the same egress policy |

Other ways of seeing a real page were also tried:

| Channel | Result |
|---|---|
| Common Crawl (archive of pages crawled under robots.txt) | `index.commoncrawl.org` and `data.commoncrawl.org` are blocked by egress. `commoncrawl.s3.amazonaws.com` is reachable but returns `AccessDenied` to anonymous requests. The only AWS credentials in the environment are 14-character placeholders, which were not used. |
| Internet Archive (Wayback Machine) | blocked by egress |
| npm and PyPI packages for these platforms (11 npm tarballs, 2 PyPI packages; read as data, never executed) | No captured store pages in any of them. Their code and docs did corroborate URL shapes and parameters (see the evidence log). |
| Web search for public repositories with saved store-page HTML | none found |

So **no request to DoorDash, Uber Eats or Grubhub succeeded from the build environment, and no real page was observed by any route.** Live extraction is not verified against real pages, and no live price was collected. `tests/test_real_captures.py` closes that gap once someone with access saves real pages with `inspect_page --save`.

### Evidence log

What each claim the adapters rely on rests on. All of it is secondary unless stated otherwise.

| Claim | Evidence | Strength |
|---|---|---|
| Store pages carry schema.org `Restaurant` → `hasMenu` → `MenuSection` → `MenuItem` → `Offer` JSON-LD | Browserless DoorDash skill write-up; Firecrawl DoorDash example code; Apify Uber Eats actor docs (it reads JSON-LD, plus `__REACT_QUERY_STATE__`) | Third-party, unverified here |
| Uber Eats store URL is `/store/{slug}/{storeId}` | `n8n-nodes-ubereats-menu` (June 2026) input spec, with a real-looking example ID | Third-party tool code |
| DoorDash store URL `/store/{id}/`, mode parameter `?pickup=false|true` | `doordash-cli` (April 2026) test fixture that mirrors DoorDash feed data: `store/24633898/?pickup=false` | Third-party fixture |
| DoorDash chain pages are `/business/{slug}-{businessId}/`; Cloudflare covers `/store/` but not `/business/` | `n8n-nodes-doordash-scraper` (June 2026): "These pages are CF-free and return full menu data" | Vendor claim. Chain-level menus are not branch prices, so the adapter uses them only to discover branches. |
| Uber Eats `?diningMode=` | General familiarity only; no source found | **Unverified.** The mode basis says so. |
| Uber Eats `/_p/api/*`, DoorDash `graphql/consumer` | Found in third-party clients that use logged-in sessions | **Not used**: private, undocumented, and session-bound |

## Access limitations and dependencies

1. **Network access (verified blocker in the build environment).** The service needs outbound HTTPS to `www.doordash.com`, `www.ubereats.com` and, for link extraction, `www.grubhub.com`. On a normal machine or server this is ordinary internet access. Run `python -m server.inspect_page --probe` first.
2. **Bot protection (reported, unverified).** Public scraper write-ups report that DoorDash store pages sit behind Cloudflare and block datacenter IPs. If that happens, the app reports `captcha` or `forbidden` barriers and stops the adapter. It will not try to defeat them. A residential connection may behave differently. That is outside this tool's control and is not assumed.
3. **Markup assumptions (unverified live).** Menu extraction depends on the stores publishing schema.org `Restaurant`/`Menu` JSON-LD. One vendor reports this is missing on a sizable share of DoorDash stores. Such stores show as "no menu data" rather than being guessed.
   * The URL patterns and the `pickup` / `diningMode` parameters come from third-party descriptions. Use `server.inspect_page` on real pages to confirm them before trusting results. If they differ, adjust the regexes in `server/adapters.py`.
   * Embedded app state (for example `__NEXT_DATA__`) is reported by the inspector but deliberately **not parsed**, because its schema could not be observed.
4. **What JSON-LD cannot tell you.** It does not say whether mandatory option groups exist, gives no fees or taxes, and has no account-dependent promotions. Live rows therefore carry the "mandatory paid options unknown" caveat, and checkout totals are only available for imported data that includes all destination charges.
5. **Order mode.** Live prices are fetched from an anonymous storefront with no address. The recorded `mode_basis` states how the mode was requested. Whether a platform shows different menu prices for pickup on its public page was not verifiable.
6. **Terms of service.** Grubhub's Terms of Use prohibit automated scraping without written authorization. DoorDash's and Uber's terms were not reviewed here. You are responsible for whether and how you run the scrapers. Robots.txt compliance is necessary, but it does not grant permission. The import path works with data obtained by other permitted means.
7. **Coverage.** Nationwide coverage is built up across bounded, resumable runs from seeds, chain pages and declared sitemaps. It is never exhaustive by construction, and the UI never claims "cheapest in the US".

## Files

```
food-price-compare/
├── index.html                 # complete frontend (UI + FPCEngine)
├── server/
│   ├── app.py                 # HTTP API + static index.html
│   ├── adapters.py            # DoorDash / Uber Eats adapters, Grubhub extraction, inspector
│   ├── jobs.py                # resumable, checkpointed, cancellable jobs
│   ├── http_client.py         # robots, rate limit, retry/backoff, cache, barriers, Playwright
│   ├── robots.py              # RFC 9309 robots.txt
│   ├── extract.py             # schema.org JSON-LD, addresses, prices, sitemaps
│   ├── inspect_page.py        # evidence harness CLI (--probe, --browser, --save)
│   └── config.py
├── examples/                  # import format examples (fictional chain)
├── tests/                     # pytest, node:test engine + Playwright UI e2e, synthetic fixtures,
│                              # test_real_captures.py + captures/ (real pages, empty here)
└── requirements.txt
```

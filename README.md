# Food Price Compare (US)

Enter a Grubhub item or order. The app looks for the **same item** (same chain, product, size, flavor, piece count, modifiers and quantity) on **DoorDash** and **Uber Eats** at branches anywhere in the 50 US states and DC. It then reports the lowest **verified** price among the locations it actually checked.

* `index.html` is the whole frontend: one file with embedded CSS and vanilla JS, and no build step.
* `server/` is a small Python companion service that does the scraping. A browser page cannot read these sites cross-origin, and the service keeps request policy and limits server-side.

> **Live status (verified 2026-10-08 against the real sites, see [Verification results](#verification-results)).**
> * **Uber Eats** works end to end. Real store pages publish the schema.org menu JSON-LD the extractor reads. Real sitemaps produced branch discovery, and three real US branches were priced. The shipped engine then ranked them, for example "Lowest verified price among 3 checked locations: $6.99, 10 pc. Chicken McNuggets®, Hammond IN".
> * **DoorDash** branch discovery works: 50 real branch links came from a chain page. Its store pages answered every request from a datacenter IP (14 of 14) with a Cloudflare challenge (HTTP 403), which the app reports and does not bypass. **No DoorDash price was obtained.**
> * **Grubhub:** robots.txt was reachable. Page extraction was not exercised live, because Grubhub's terms forbid automated scraping.
> * The Claude build sandbox itself cannot reach any of these sites. The live runs used this repository's GitHub Actions workflow.

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
| Branch discovery | (a) store or chain URLs you paste as seeds; (b) the **sitemaps the site declares in its own robots.txt**, read as sitemap index → urlsets, with gzip support and DTDs rejected; (c) store links on chain/brand pages, following `rel="next"` pagination. Store URLs are filtered by chain-name tokens in the slug. Seeds come first, then chain pages, then sitemaps. Store-, business- and brand-named sitemaps are read first. Non-US country sections (`/ca/`, `/fr-en/`, `/jp/`…) are recorded at discovery and never fetched. | **Verified live.** Uber Eats robots.txt declares gzipped `sitemap-store-*` files: 2 read, 315 McDonald's URLs found, 226 of them non-US. DoorDash declares store, business, `business_menu` and city sitemap indexes. A real DoorDash `/business/` page yielded 50 Chipotle branch links. |
| Menu + prices | schema.org **JSON-LD**: `Restaurant`/`FoodEstablishment` → `hasMenu` → `MenuSection` → `MenuItem` → `Offer` (also `priceSpecification`, `AggregateOffer` (a range, so never verified), `menuAddOn`, `availability`), and `PostalAddress` for the US check. Text is HTML-unescaped. Identical listings repeated across sections are collapsed. A published `0.00` is never treated as a price. | **Verified live on 6 real Uber Eats store pages** (167–335 items each): `hasMenu.hasMenuSection[].hasMenuItem[].offers.{price,priceCurrency:"USD"}` plus full `PostalAddress`. Uber Eats publishes no `menuAddOn` and no `availability`. DoorDash store pages could not be read (Cloudflare). |
| JS-rendered pages | **Auto** mode re-renders a page with Playwright/Chromium only when it answered normally but carried no structured data. A browser is never used to get past a block or challenge. | |
| URL shapes | DoorDash `/store/<slug>-<id>/` or `/store/<id>/`, chain pages `/business/<slug>-<id>/`. Uber Eats `/store/<slug>/<id>`, `/<cc>[-<lang>]/store/…`, `/brand/…`. Mode parameters: DoorDash `?pickup=false|true`, Uber Eats `?diningMode=DELIVERY|PICKUP`. | **URL shapes verified live** (real sitemaps and chain pages). Uber Eats pages answered normally with `diningMode=DELIVERY`, but whether the parameter changes the published prices is **unverified**. The same goes for DoorDash's `pickup`. Each observation records the mode basis. |

Pages without the standard markup produce an explicit **"no menu data"** outcome. The adapters do not guess selectors or call private endpoints.

**Politeness and robustness** (`server/http_client.py`, `server/jobs.py`):

* Each platform has a bounded worker pool (default 2), and each host has a minimum interval between requests (default 3 s; a larger `Crawl-delay` wins).
* Network errors, 429 and 5xx get up to 3 retries with exponential backoff and jitter. `Retry-After` is honored, in seconds or as an HTTP date. If it exceeds 300 s, the adapter stops instead of waiting.
* Barriers are classified and reported, never bypassed: `egress_denied`, `robots_disallow`, `captcha`, `login_wall`, `forbidden`, `rate_limited`, `offsite_redirect`, `network`.
  * `captcha` covers challenge interstitials and redirects to a challenge, e.g. Uber's 307 to `def.uber.com/…/challenge`, observed live.
  * A large, normal HTTP 200 page that merely references a challenge vendor is **not** a barrier. Real Uber Eats store pages do contain such references.
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

### Local test suites (build sandbox, 2026-10-08)

| Suite | Command | Result |
|---|---|---|
| Backend (pytest) | `python -m pytest -q` | **22 passed, 1 skipped**. The skipped test is `test_real_captures.py`; real captures exist only on the runner, where it **passed 5/5**. |
| Engine (Node, runs the exact `<script id="fpc-engine">` from index.html) | `node --test tests/engine.test.mjs` | **21 passed** |
| UI end-to-end (Playwright + Chromium, real backend, local fixture sites) | `node --test tests/ui.e2e.mjs` | **18 passed** |

The pipeline tests use **synthetic local fixture servers** (`tests/fake_platform.py`). They exercise the mechanics:

* **Discovery and dedup:** robots.txt gating (including `Disallow: /`) and Crawl-delay spacing; sitemap index → urlset discovery with chain filtering; chain pages with `rel=next` pagination; non-US country sections never queued; no-menu pages; duplicate addresses.
* **Retries and barriers:** retry with Retry-After (gap measured ≥ 1 s); over-long Retry-After stops the adapter; CAPTCHA, challenge redirect, login wall, 403, 5xx exhaustion; off-site redirect refused (SSRF).
* **Fetch modes:** cache reuse with the original timestamp; Playwright rendering of a page whose JSON-LD is injected by JavaScript; pickup vs delivery URLs.
* **Run control:** per-run limit then resume; cancel mid-run, then resume after a simulated server restart, with nothing refetched and nothing lost; circuit breaker on one platform while the other completes.
* **Grubhub and the API:** Grubhub extraction and its robots-blocked fallback; HTTP API, CORS and purge.
* **Real-page quirks:** the regressions for quirks found on real pages (below).

The engine tests cover:

* integer-cent parsing that rejects float noise, comma decimals and more than two decimals
* size, piece-count and flavor matches and mismatches
* add-ons, removals and mandatory options; quantity × configured price; missing charges; `$0.00` listings
* the USD-only rule; pickup/delivery separation
* stale data, unavailable items, non-US and other-chain exclusion; the location filter
* duplicate handling; same-branch baskets (never combined)
* checkout totals with unknown fees; import validation; CSV formula-injection guarding
* receipt parsing; Grubhub URL parsing; the example files

The UI test clicks through every control and asserts there were no page errors.

### Live runs against the real sites

The build sandbox's egress policy blocks all three sites; the attempts and alternatives tried are listed below. The live runs therefore use `.github/workflows/fpc-live-verification.yml`. It runs on pushes to this branch that change `scripts/live_verify.py` or the workflow, on a GitHub-hosted runner with ordinary internet access. It runs the unchanged service code with its robots.txt and rate limits (3 s per host).

Each run's budget is bounded:
* robots.txt for each site
* two jobs, each limited to 3 sitemap documents and 3–6 stores
* up to 7 inspected pages

It commits `live-verification/report.json`, which holds observations and structure summaries but no raw HTML, plus the real-capture test output. Raw pages are kept only as a 14-day workflow artifact.

| Run | What happened | Fixes made from it |
|---|---|---|
| [37707896611](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37707896611) | robots.txt 200 on all three sites. Uber Eats sitemaps gave 315 McDonald's URLs. A DoorDash `/business/` page gave 50 real Chipotle branches. DoorDash store pages returned 403 (Cloudflare "Just a moment…"). **0 prices**: the Uber Eats budget was spent on non-US URLs, and a real 3 MB Uber Eats store page was misread as a CAPTCHA. | Non-US URLs are no longer queued. The challenge classifier is now size- and title-aware. A structure probe was added. |
| [37708363041](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37708363041) | Real Uber Eats store pages parsed: **243 / 259 / 331 menu items**; real-capture test **5 passed**. First real price: Big Mac® $7.59, Hammond IN. Two stores hit a 307 to Uber's challenge host. | HTML entities in JSON-LD are unescaped. Repeated listings are collapsed. `$0.00` listings (Zucker's "Iced Latte", "Coffee") are never treated as prices. The challenge redirect is reported as `captcha`. |
| [37708779121](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37708779121) | Uber Eats: 3 US branches priced, 2 challenged (reported). DoorDash: 3/3 store pages hit Cloudflare 403; the chain page's menu has `storeId: $undefined`, so it isn't used as a branch price. Real-capture test **5 passed**. | — |

**Real prices from run 37708779121** (Uber Eats, delivery, anonymous session, no address, observed 2026-10-08 ~00:39 UTC), ranked by the shipped engine with `node scripts/rank_report.mjs live-verification/report.json "McDonald's" "10 pc Chicken McNuggets" 1 8.49`:

| Rank | Listing | Branch | Item subtotal |
|---|---|---|---|
| 1 | 10 pc. Chicken McNuggets® | McDonald's® (Columbia & 165th St), 6437 Columbia Ave, Hammond IN 46320 | **$6.99** |
| 2 | 10 pc. Chicken McNuggets® | McDonald's® (2324 Nesconset), Stony Brook NY 11790 | $7.59 |
| 3 | 10 pc. Chicken McNuggets® | McDonald's® (Market & Preston), Louisville KY 40202 | $8.29 |

* Winner label: **"Lowest verified price among 3 checked locations"**. Against a sample Grubhub subtotal of $8.49, the savings are $1.50.
* The engine rejected the real look-alikes with reasons: "20/40/4 pc … Piece count differs", "10 pc … Meal: candidate adds 'meal'", and "Happy Meal".
* With the piece count left out of the order, every candidate became "needs confirmation" and there was no winner.
* These are three branches in a bounded sample, not a nationwide result.

**Attempts from the build sandbox** (all blocked by its egress policy):
* direct HTTP and real Chromium to all three sites (`egress_denied` / `ERR_TUNNEL_CONNECTION_FAILED`);
* the server-side web fetch tool;
* Common Crawl: index and data hosts blocked, and S3 `AccessDenied` for anonymous reads (the environment's AWS variables are placeholders and were not used);
* the Wayback Machine.

The sandbox also checked npm and PyPI packages for these platforms (read as data, never executed). They held no saved pages, but their docs corroborated the URL shapes.

### Evidence log

| Claim | Evidence | Status |
|---|---|---|
| Uber Eats store pages publish `Restaurant → hasMenu → MenuSection → MenuItem → Offer{price, priceCurrency}` JSON-LD with `PostalAddress` | 6 real store pages fetched by the live runs | **Verified live** |
| Uber Eats JSON-LD strings contain HTML entities; listings repeat across sections; some prices are `0.00` because options set the price | Real pages (McDonald's "Columbia &amp; 165th St"; Zucker's "Iced Latte" $0.00) | **Verified live**; handled |
| Uber Eats bot checks arrive as a 307 to `def.uber.com/…/challenge` | Live runs 2 and 3 | **Verified live**; reported as `captcha` |
| DoorDash store pages are behind Cloudflare for datacenter IPs | all 14 DoorDash store-page requests across the three runs returned 403 "Just a moment…" | **Verified live** (from GitHub runners) |
| DoorDash `/business/{slug}-{id}/` is not challenged and lists branch links; its menu is chain-level (`storeId: $undefined`, Next.js stream, no JSON-LD) | Live runs | **Verified live**; used for discovery only |
| Uber Eats `/store/{slug}/{storeId}`, country sections `/<cc>[-<lang>]/store/` | Real sitemaps | **Verified live** |
| DoorDash `?pickup=false|true`, Uber Eats `?diningMode=` change the published prices | Pages load with them; no price comparison across modes was made | **Unverified** (recorded in `mode_basis`) |
| Uber Eats `__REACT_QUERY_STATE__` (2–3 MB, `\u0022`-escaped JSON) holds the app state | Structure probe | Observed; **not parsed** (undocumented) |
| Uber Eats `/_p/api/*`, DoorDash `graphql/consumer` | Third-party clients using logged-in sessions | **Not used**: private and session-bound |

## Access limitations and dependencies

1. **Network access.** The service needs outbound HTTPS to `www.doordash.com`, `www.ubereats.com` and, for link extraction, `www.grubhub.com`. The Claude build sandbox is denied all three. A GitHub-hosted runner and ordinary machines are not. Run `python -m server.inspect_page --probe` first.
2. **DoorDash prices (blocked, verified).** DoorDash store pages served a Cloudflare challenge to every request from GitHub's datacenter IPs. The app reports `captcha` barriers and its circuit breaker stops the adapter. It does not try to defeat the challenge. From other networks DoorDash may or may not serve the page; the run log shows which. Without readable store pages there are **no DoorDash branch prices**, and the DoorDash chain-page menu is not branch-specific.
3. **Uber Eats bot checks (intermittent, verified).** Two of five store fetches in run 3, and two of three in run 2, were redirected to Uber's challenge page. These are reported and skipped. Resume tries the remaining queue later.
4. **What the published data cannot tell you.**
   * Uber Eats JSON-LD has no add-on, option or availability data. An order with modifiers therefore always "needs confirmation", and the availability of live rows is "unknown".
   * `$0.00` listings are unverifiable.
   * No fees, taxes or account-dependent promotions are published, so checkout totals exist only for imported data that includes all destination charges.
5. **Order mode.** Live prices come from an anonymous storefront with no address. `mode_basis` states how the mode was requested. Whether pickup prices differ on the public page is unverified.
6. **Terms of service.** Grubhub's Terms of Use prohibit automated scraping without written authorization. DoorDash's and Uber's terms were not reviewed here. You are responsible for whether and how you run the scrapers. Robots.txt allowing crawling (verified for all three) is not the same as permission. The import path works with data obtained by other permitted means.
7. **Coverage.** Nationwide coverage is built up across bounded, resumable runs from seeds, chain pages and declared sitemaps. Uber Eats declares hundreds of sitemap files. It is never exhaustive by construction, and the UI never claims "cheapest in the US".

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
│   ├── structure_probe.py     # how a real page publishes its data (evidence gathering)
│   └── config.py
├── examples/                  # import format examples (fictional chain)
├── scripts/live_verify.py     # bounded live run (used by .github/workflows/fpc-live-verification.yml)
├── scripts/rank_report.mjs    # rank a live report's real observations with the shipped engine
├── live-verification/         # latest live report (committed by the workflow)
├── tests/                     # pytest, node:test engine + Playwright UI e2e, synthetic fixtures,
│                              # test_real_captures.py + captures/ (real pages; git-ignored)
└── requirements.txt
```

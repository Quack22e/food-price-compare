# Food Price Compare (US)

Enter a Grubhub item or order. The app looks for the **same item** (same chain, product, size, flavor, piece count, modifiers and quantity) on **DoorDash** and **Uber Eats** at branches anywhere in the 50 US states and DC. It then reports the lowest **verified** price among the locations it actually checked.

* `index.html` is the whole frontend: one file with embedded CSS and vanilla JS, and no build step. Without a backend it still ranks real prices from a live-run snapshot (`live-verification/snapshot.json`), prices you type in, and imports.
* `server/` is a small Python companion service that does the scraping. A browser page cannot read these sites cross-origin, and the service keeps request policy and limits server-side.
* `board/index.html` is a static **price board**: the same engine over real snapshots from the live runs, with no backend. It opens straight from disk and is also published as a private claude.ai artifact. See [The price board](#the-price-board).

> **Live status (verified 2026-10-08 against the real sites, see [Verification results](#verification-results)).**
> * **Uber Eats** works end to end. Real store pages publish the schema.org menu JSON-LD the extractor reads, and real sitemaps produce branch discovery. The 18:59 UTC snapshot priced 36 real US branches across three chains, for example "Lowest verified price among 22 checked locations: $5.19, 10 pc. Chicken McNuggets®, McDonald's, Abingdon VA" (the highest was $8.55 in Glendora, CA).
> * **Menus change by time of day.** At 07:14 UTC, 18 McDonald's branches published breakfast menus with no McNuggets. Each snapshot run is therefore kept separate, never merged.
> * **DoorDash** branch discovery works. It covers 131 per-state store sitemaps (California alone lists 106,228 store URLs) and chain pages (50 real Chipotle branches). Its store pages answered every request from a datacenter IP with a Cloudflare challenge (HTTP 403; 69 of 69 across 14 runs), which the app reports and does not bypass. Its `/business/…/menu` pages carry prices, but DoorDash labels them on the page as *"estimated based on your predicted location and may vary across store"*, so they are not branch prices. **No current DoorDash price was obtained by the live runs.**
>   * **What a DoorDash store page publishes is now known from real pages.** The Wayback Machine holds copies its own crawler captured. 15 of them (Sep 2025 to 7 Oct 2026; Taco Bell, Chipotle, Lou Malnati's, McDonald's and local stores) were read structure-only by `scripts/archive_probe.py`. Each has schema.org `Restaurant` + `Menu` JSON-LD with a street address and `addressCountry`, but **no `priceCurrency`** and **no postal code**. Prices are display strings such as `"$8.53"`, `"US$12.75"` or `"$13.59+"` (a starting price). The page's own app data states `"currency":"USD"` for US stores and `"CAD"` for Canadian ones. The DoorDash reader now handles that layout and was run on all 15 pages: **9 US stores read with USD prices**, 3 Canadian stores excluded, 3 pages without a Restaurant block reported as "no menu data". Archived prices are historical and are never used as prices.
>   * **To include DoorDash prices now,** type the prices you see in the DoorDash app ("Add prices you saw yourself", in the app and on the board). This involves no scraping at all.
> * **Grubhub** restaurant pages are a JavaScript shell with no structured data. Grubhub prices now come from two **Grubhub-owned menu sites** that link each page back to its Grubhub listing, and a page is used only when it links to the same Grubhub ID:
>   * **MenuPages** publishes the menu as JSON-LD. Verified live: Lou Malnati's, 6456 S Cicero Ave, Chicago (ID 434510), 162 priced listings. It covers a subset of Grubhub (28,133 restaurant pages).
>   * **AllMenus** is the fallback. It shows prices only in its HTML, so they are used only when its 217 HTML item names equal the page's own JSON-LD item names. Verified live: Lou Malnati's, 85 S Buffalo Grove Rd (ID 517712, not on MenuPages), 169 priced listings.
> * **End-to-end on real data:** a Small - Deep Dish Margherita is $21.55 on Grubhub at Lou Malnati's Midway (via MenuPages) and $20.35 at Buffalo Grove (via AllMenus). The lowest verified Uber Eats price is $23.94 among 5 checked Lou Malnati's branches, so here Grubhub was cheaper.
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
# FPC_DISABLE_GRUBHUB_MIRRORS=1 python -m server.app   # same, without the MenuPages/AllMenus lookups
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
   * **Grubhub link:** the backend fetches the page (robots.txt-checked) and reads schema.org data. Real Grubhub pages carry none, so it then looks the restaurant up on **MenuPages**, then **AllMenus** (both Grubhub-owned). MenuPages candidates come from its robots-declared restaurant sitemap (exact name-address slug first, then the same address). AllMenus candidates come from its 21 restaurant sitemaps (name begins and city ends the Grubhub slug; the first lookup reads them all, about a minute, then they are cached for 24 h). A page is used only if it links back to the **same Grubhub restaurant ID**. A menu picker then fills the item name and the Grubhub price × quantity. If no page qualifies, the chain is guessed from the URL slug and labeled as a guess, and you complete the rest by hand.
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

**Data sources panel:**
* **Live-run snapshot:** loads the real prices recorded by a live run (the server's `live-verification/snapshot.json` via **Load the server's snapshot**, or the file itself without a backend). One run at a time, never merged. Rows are labelled "Snapshot". A store counts as checked for an order item only if the snapshot search looked for that item. The "Stale after" setting decides whether its prices can win.
* **Add prices you saw yourself:** platform, store link (the app's Share → Copy link), store name and address, order mode, time seen, and lines like `Crunchwrap Supreme $7.31`. The store ID comes from the link. The record goes through the same validation as an import and is labelled "Imported", with "entered by you from what DoorDash showed you" as its price context. This is the scraping-free way to include DoorDash.

**Import** adds menus you collected yourself (JSON or CSV, schema below). Imported rows are labeled "Imported" and can be excluded with a checkbox. **Demo mode** is opt-in, shows a banner, uses a fictional chain, and switches the whole results view to demo data, which never mixes with real data.

### The price board

`board/index.html` needs no backend. It ranks real snapshot prices from the live runs with the engine copied verbatim from `index.html` (a test fails if they differ). Rebuild it after a live run with `python3 scripts/build_board.py`.

* **Example orders** from the snapshot searches. Ones with a real Grubhub price come first.
* **Grubhub link lookup and menu picker** for Grubhub menus read through MenuPages. Picking an item fills the chain, item and Grubhub subtotal, which follows the quantity.
* **One snapshot run at a time** ("Prices from"). Each run is a coherent moment, because menus change through the day. An example order opens on the newest run in which a checked store listed the item, and says so.
* Location, freshness (24 h / 7 days / 30 days) and platform filters; verified / needs confirmation / not the same item tabs with the engine's reasons; **coverage and blocks** per search.
* Platform status cards, the alternative-sources table, JSON/CSV import and the capture bookmarklet.
* It makes no network requests except optional web fonts.

`scripts/snapshot.py` produces the data. It runs in the live workflow with the app's own `Service` and the same robots.txt, rate limits and barrier handling, and writes `live-verification/snapshot.json` in the import schema with coverage counters and barriers. The builder reads that file and its committed history, plus older `report.json` versions.

## How live data is acquired

Each adapter (`server/adapters.py`) relies only on published standards and on what the page itself publishes:

| Step | Method | Evidence basis |
|---|---|---|
| Permission | `robots.txt` per RFC 9309 (wildcards, longest match, `Crawl-delay`). If robots.txt is unreachable (401/403/429/5xx or network failure), the site is treated as **disallowed**. A representative store path is tested before any crawl. | Standard |
| Branch discovery | (a) store or chain URLs you paste as seeds; (b) the **sitemaps the site declares in its own robots.txt**, read as sitemap index → urlsets, with gzip support and DTDs rejected; (c) store links on chain/brand pages, following `rel="next"` pagination. Store URLs are filtered by chain-name tokens in the slug. Seeds come first, then chain pages, then sitemaps. Store-, business- and brand-named sitemaps are read first. Non-US country sections (`/ca/`, `/fr-en/`, `/jp/`…) are recorded at discovery and never fetched. | **Verified live.** Uber Eats robots.txt declares gzipped `sitemap-store-*` files: 2 read, 315 McDonald's URLs found, 226 of them non-US. DoorDash declares store, business, `business_menu` and city sitemap indexes. A real DoorDash `/business/` page yielded 50 Chipotle branch links. |
| Menu + prices | schema.org **JSON-LD**: `Restaurant`/`FoodEstablishment` → `hasMenu` → `MenuSection` → `MenuItem` → `Offer` (also `priceSpecification`, `AggregateOffer` (a range, so never verified), `menuAddOn`, `availability`), and `PostalAddress` for the US check. Text is HTML-unescaped. Identical listings repeated across sections are collapsed. A published `0.00` is never treated as a price. | **Verified live on 6 real Uber Eats store pages** (167–335 items each): `hasMenu.hasMenuSection[].hasMenuItem[].offers.{price,priceCurrency:"USD"}` plus full `PostalAddress`. Uber Eats publishes no `menuAddOn` and no `availability`. DoorDash store pages could not be read live (Cloudflare). **Their layout was verified on 15 archived real pages:** `Restaurant` with `PostalAddress` (no `postalCode`), `hasMenu.hasMenuSection` as a list of lists, `Offer.price` as a display string (`"$8.53"`, `"US$12.75"`, `"$13.59+"`), no `priceCurrency`. |
| Grubhub prices | **MenuPages** (Grubhub-owned): robots.txt allows restaurant pages and declares `sitemap-index.xml` → `sitemap-restaurants.xml.gz`. A restaurant page has a `Restaurant` JSON-LD block (name, `PostalAddress`) and a separate `Menu` JSON-LD block with USD `Offer` prices, plus a link to `grubhub.com/restaurant/<slug>/<id>`. A page is accepted only if that link carries the same Grubhub ID (`server/grubhub_mirror.py`). | **Verified live** (runs 37741487660 and later): 162 priced listings for Grubhub ID 434510; 28,133 restaurant URLs in the sitemap. |
| Grubhub prices (fallback) | **AllMenus** (Grubhub-owned): robots.txt allows menu pages and declares `sitemap-index.xml` with 21 `sitemap-restaurants*.xml.gz` files (up to 40,000 URLs each, `/<st>/<city>/<id>-<name>/menu/`, unordered); city pages list no restaurants in their HTML. Its JSON-LD holds item names but no prices, name or address. Prices are read from `li.menu-items > span.item-title + span.item-price`, only when those names equal the JSON-LD names. Name and address come from the `<title>` and meta description, which must agree. The page must link to the same Grubhub ID. | **Verified live** (runs 37743302443–37827940323): 217 = 217 names, 169 priced listings for Grubhub ID 517712; the real-capture test passes on the saved page. |
| Currency when JSON-LD omits it | DoorDash prices count as USD only when the store's published address is in the US **and** the page's own data states `"currency":"USD"` and no other currency. The basis is written into the observation's `price_context`. Otherwise the prices are not USD and are excluded. The capture bookmarklet applies the same rule. | Archived DoorDash pages: US stores state `USD`, Canadian stores `CAD` |
| Starting prices | A price published as `"$13.59+"` is a starting price (options such as size set the final price). It is never an exact price: the item gets no price, a note saying why, and a required price-setting option, so it can only "need confirmation". | Archived DoorDash pages (Lou Malnati's deep dish, Taco Bell Chalupa Supreme), and the page shows the same `+` |
| Lenient JSON-LD | A block that fails strict JSON parsing only because of raw control characters inside strings is re-read with `strict=False`, and the page records that it was. Malformed JSON is still rejected. | AllMenus (Grubhub-owned) emits such a block (char 105,771 of 125 KB). |
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
  * Normalization handles only harmless differences: case, punctuation, `&` and `+` vs "and", `'N'` vs "and", `w/` vs "with", and inch marks. Calorie notes such as `[490-630 Cals]` or `(350 cal.)` (seen on archived DoorDash pages) are removed.
  * A **verified** match needs identical product words once size and piece-count words are extracted. A bare leading number ("10 Chicken McNuggets") counts as the piece count only when it equals the count the other side states, so "7 Layer Burrito" stays a name.
  * When the names differ:
    * by a **variant word** (meal, combo, bundle, supreme, deluxe, double, jr, spicy, crispy, grilled, gf, vegan, bowl, salad, wrap, with, no, extra …) or by a number: **not equivalent**;
    * by one or two other words, with one name containing the other ("Small Deep Dish Margherita **Pizza**", "**World Famous** Fries - Large", "**Chicken** Burrito Bowl"): **needs confirmation**, with the words shown. Such a listing is never verified on its own;
    * otherwise: **not equivalent**.
  * **Closest listings:** when no checked store lists an order item exactly, the results show up to five listings it may mean. Each shares a product word, has no variant-word difference, and is ranked by text similarity (character trigrams). These are only suggestions. **Same item** adds the listing to the item's confirmed alternate names (aliases), after which it is checked like any listing: size, piece count, flavor, modifiers, currency and price.
  * Aliases are used only when you enter or confirm them, and matches through them are labelled as yours.
  * No embedding model decides a match: one would score "10 pc" vs "20 pc", "Small" vs "Large", or an item vs its Meal as near-identical, which are exactly the differences this app must catch.
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
| `address.street/city/state/zip/country` | city, state | 50 states + DC (codes or names); ZIP optional (DoorDash pages publish none) but, if given, a 5-digit US ZIP; country `US` or omitted. A store without a ZIP cannot match a ZIP filter. |
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
| `GET /api/snapshot` | the live-run snapshot file (`FPC_SNAPSHOT`, default `live-verification/snapshot.json`), read-only; 404 if absent |
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
* `FPC_SNAPSHOT` (`./live-verification/snapshot.json`)
* `FPC_USER_AGENT`

CORS is limited to `null` (file://) and localhost origins.

**Privacy.** Receipts are parsed in the browser only. The backend stores only:
* chain and item names, aliases and flavor (needed to discover branches)
* discovered store URLs and their status
* public menu observations

It never stores receipts, Grubhub prices, quantities, delivery addresses or location filters. The browser keeps only the backend URL in `localStorage`. **Delete server cache & checkpoints** wipes the server side.

## Verification results

### Local test suites (build sandbox, 2026-10-09)

| Suite | Command | Result |
|---|---|---|
| Backend (pytest) | `python -m pytest -q` | **40 passed, 3 skipped**. The skipped tests are in `test_real_captures.py`; real pages exist only on the runners, where the live-page tests passed in run 37827940323 and the archived DoorDash pages **passed 15/15** in run 37893861203. |
| Engine (Node, runs the exact `<script id="fpc-engine">` from index.html) | `node --test tests/engine.test.mjs` | **23 passed** (includes the committed real snapshot) |
| Capture bookmarklet | `node --test tests/capture.test.mjs` | **6 passed** |
| UI end-to-end (Playwright + Chromium, real backend, local fixture sites) | `node --test tests/ui.e2e.mjs` | **21 passed** |
| Price board (Playwright + Chromium, committed real snapshot) | `node --test tests/board.e2e.mjs` | **13 passed** |

The pipeline tests use **synthetic local fixture servers** (`tests/fake_platform.py`). They exercise the mechanics:

* **Discovery and dedup:** robots.txt gating (including `Disallow: /`) and Crawl-delay spacing; sitemap index → urlset discovery with chain filtering; chain pages with `rel=next` pagination; non-US country sections never queued; no-menu pages; duplicate addresses.
* **Retries and barriers:** retry with Retry-After (gap measured ≥ 1 s); over-long Retry-After stops the adapter; CAPTCHA, challenge redirect, login wall, 403, 5xx exhaustion; off-site redirect refused (SSRF).
* **Fetch modes:** cache reuse with the original timestamp; Playwright rendering of a page whose JSON-LD is injected by JavaScript; pickup vs delivery URLs.
* **Run control:** per-run limit then resume; cancel mid-run, then resume after a simulated server restart, with nothing refetched and nothing lost; circuit breaker on one platform while the other completes.
* **Grubhub and the API:** Grubhub extraction and its robots-blocked fallback; the MenuPages path (exact slug, the live "-midway" layout with a same-address decoy that links to another Grubhub ID, a wrong-ID page refused, a MenuPages base outside the allowlist refused, a restaurant not listed); HTTP API, CORS and purge.
* **Real-page quirks:** the regressions for quirks found on real pages (below), including the DoorDash store-page layout recorded from archived pages (currency only in the page's data, no ZIP, nested sections, starting prices, Canadian and address-less pages excluded). These tests were mutation-checked.

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

The build sandbox's egress policy blocks all three sites; the attempts and alternatives tried are listed below. The live runs therefore use `.github/workflows/fpc-live-verification.yml`. It runs on pushes to this branch that change `scripts/live_verify.py`, `scripts/snapshot.py` or the workflow, on a GitHub-hosted runner with ordinary internet access. It runs the unchanged service code with its robots.txt and rate limits (3 s per host).

Each run's budget is bounded:
* robots.txt for each site
* two jobs, each limited to 3 sitemap documents and 3–6 stores
* up to 7 inspected pages
* the snapshot: three Uber Eats searches capped at 24, 10 and 12 store pages and 12 sitemap documents each, plus two Grubhub listings looked up on MenuPages

It commits `live-verification/report.json` and `live-verification/snapshot.json`, which hold observations, coverage, barriers and structure summaries, plus the real-capture test output. They contain no raw pages; structure summaries quote at most short snippets (a few hundred characters) around price and challenge markers. Raw pages are kept only as a 14-day workflow artifact. The snapshot step runs only when the unit tests passed, and the commit step rebases onto the branch if it moved during the run.

| Run | What happened | Fixes made from it |
|---|---|---|
| [37707896611](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37707896611) | robots.txt 200 on all three sites. Uber Eats sitemaps gave 315 McDonald's URLs. A DoorDash `/business/` page gave 50 real Chipotle branches. DoorDash store pages returned 403 (Cloudflare "Just a moment…"). **0 prices**: the Uber Eats budget was spent on non-US URLs, and a real 3 MB Uber Eats store page was misread as a CAPTCHA. | Non-US URLs are no longer queued. The challenge classifier is now size- and title-aware. A structure probe was added. |
| [37708363041](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37708363041) | Real Uber Eats store pages parsed: **243 / 259 / 331 menu items**; real-capture test **5 passed**. First real price: Big Mac® $7.59, Hammond IN. Two stores hit a 307 to Uber's challenge host. | HTML entities in JSON-LD are unescaped. Repeated listings are collapsed. `$0.00` listings (Zucker's "Iced Latte", "Coffee") are never treated as prices. The challenge redirect is reported as `captcha`. |
| [37708779121](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37708779121) | Uber Eats: 3 US branches priced, 2 challenged (reported). DoorDash: 3/3 store pages hit Cloudflare 403; the chain page's menu has `storeId: $undefined`, so it isn't used as a branch price. Real-capture test **5 passed**. | — |
| [37709380287](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37709380287) | DoorDash's robots-declared sitemaps redirect to `cdn.doordash.com/sitemaps/…`, and the host allowlist refused them, so DoorDash sitemap discovery could never have worked. The Grubhub homepage is a 14.6 KB JS shell with no restaurant links, and `/sitemap.xml` is a 404. | `cdn.doordash.com` is allowed for sitemap fetches only. A real Grubhub link is now taken from a Grubhub-powered AllMenus page. |
| [37709849182](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37709849182) | DoorDash store sitemap index: **131 per-state sitemaps**; the CA file lists **106,228** store URLs. `business_menu` index: 5 files (one lists 6,452 menu pages), but the pages say *"Prices are estimated based on your predicted location and may vary across store"*. Grubhub extractor on the real `…/lou-malnatis-pizzeria-85-s-buffalo-grove-rd-buffalo-grove/517712` (HTTP 200, no structured data): URL-only fields (ID 517712, chain flagged as a guess) plus a manual-entry note. Real-capture test **7 passed**. | A location filter's state now picks which DoorDash per-state store sitemap is read first. |
| [37713038503](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37713038503) | Grubhub-owned menu sites: AllMenus robots.txt allows restaurant pages; its Lou Malnati's Buffalo Grove page shows prices in HTML and links to Grubhub ID 517712. MenuPages says "powered by Grubhub". An Uber Eats brand-city page answered 200 with one store link. | — |
| [37713405667](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37713405667) | MenuPages: `sitemap-index.xml` → `sitemap-restaurants.xml.gz` with **28,133** restaurant URLs (9 Lou Malnati's in Chicago). The Midway page has `Restaurant` JSON-LD (6456 S Cicero Ave, Chicago IL 60638) and a separate `Menu` JSON-LD block with 198 USD-priced listings, and links to Grubhub ID 434510. A MenuPages URL derived for Buffalo Grove is a 404. | `server/grubhub_mirror.py`: Grubhub URL → MenuPages candidates → accepted only on a matching Grubhub ID. |
| [37741487660](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37741487660) | The app's Grubhub extractor on the real `…/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510`: **162 priced Grubhub listings** via MenuPages, chain and address filled. The sitemap lists `lou-malnatis-pizzeria/6456-…` while the page that links to Grubhub is `lou-malnatis-pizzeria-midway/6456-…`. Some MenuPages section names end in invalid UTF-8. | Candidates now include same-address pages; U+FFFD is dropped from text. |
| [37741924057](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37741924057) | First snapshot (07:14 UTC). Uber Eats: Lou Malnati's 5 branches priced (2 challenged), Taco Bell 7 priced (2 challenged), McDonald's 18 read, but their menus were **breakfast menus with no McNuggets**. Lou Malnati's Uber Eats names match MenuPages' ("Small - Deep Dish Margherita"). | The board keeps snapshot runs separate. |
| [37743302443](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37743302443) | AllMenus: `sitemap-index.xml` lists 29 files, 21 of them restaurant sitemaps (the first holds 40,000 URLs shaped `<state>/<city>/<id>-<name>/`). The Buffalo Grove page's 125 KB JSON-LD parsed only leniently: **217 item names, but no prices, and a blank name and address**. Prices are only in HTML `item-price` spans. Snapshot: Lou Malnati's 6, Taco Bell 9, McDonald's 17 branches read. | Lenient JSON-LD parsing. |
| [37744579395](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37744579395) | AllMenus markup: 217 `li.menu-items`, each with one `item-title` and one `item-price` ("$27.15"); sections in `menu-section-title`; the address only in the title and meta description. | AllMenus reader with name, address and Grubhub-ID cross-checks. |
| [37826699377](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37826699377) | AllMenus discovery: the city sitemap lists 4,031 cities, but the Buffalo Grove city page has no restaurant links in its HTML, and the restaurant sitemaps are unordered. Lunchtime snapshot (18:48 UTC): McDonald's 21 branches priced, McNuggets $5.19–$8.55. One real Uber Eats page (a military-base McDonald's) publishes Restaurant JSON-LD with no Menu. | Lookup by scanning the restaurant sitemaps; the real-capture test accepts a page with no Menu (reported as "no menu data"). |
| [37827940323](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37827940323) | The app's Grubhub extractor on the real Buffalo Grove listing (517712): not on MenuPages → **AllMenus page found and accepted, 169 priced listings**. The Midway listing still resolves via MenuPages. Real-capture tests **9 passed**, including the MenuPages and AllMenus pages. Snapshot: 36 branches. | — |
| [37862243737](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37862243737) | After the review fixes (barrier handling, exact-one Grubhub ID, rebase-before-push): both Grubhub lookups still succeed with no barriers (MenuPages 434510: 169 priced of 171; AllMenus 517712: 169 priced). MenuPages' price for a Small - Deep Dish Margherita moved from $21.55 (18:59 UTC) to $21.75 (00:01 UTC). Snapshot: 40 Uber Eats branches, 0 blocked. Real-capture tests 9 passed. | — |

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

**Real prices from snapshot run 37827940323** (Uber Eats, delivery, anonymous session, no address, observed 2026-10-08 ~18:59 UTC), ranked by the shipped engine:

| Order | Lowest verified price | Other verified branches |
|---|---|---|
| McDonald's, 10 pc. Chicken McNuggets | **$5.19**, McDonald's® (1105 Old Berry Dr), Abingdon VA, "among 22 checked locations" | 20 more, from $5.34 (Maplewood MN) and $5.49 (Cypress TX) up to $8.39 (Saratoga Springs NY) and $8.55 (Glendora CA) |
| Taco Bell, Crunchwrap Supreme | **$7.31**, Fulton MS and Fort Oglethorpe GA (tie), "among 9 checked locations" | $7.55 Mexia TX, $7.92 Moreno Valley CA, $7.92 Myrtle Beach SC, … $8.53 Clermont FL |
| Lou Malnati's Pizzeria, Small - Deep Dish Margherita. Grubhub: **$21.55** at 6456 S Cicero Ave, Chicago (MenuPages) and **$20.35** at 85 S Buffalo Grove Rd (AllMenus) | **$23.94**, 10706 Broadway, Crown Point IN, "among 5 checked locations". Grubhub is cheaper by $2.39 (vs Midway) | $24.42 Elgin IL, $25.86 Northbrook IL, $27.18 Surprise AZ; Flossmoor IL did not list it |

At 07:14 UTC the same McDonald's search found only breakfast menus (no McNuggets at any of 18 branches). Earlier, at 00:39 UTC: $6.99 Hammond IN, $7.59 Stony Brook NY, $8.29 Louisville KY.

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
| DoorDash store pages are behind Cloudflare for datacenter IPs | all 69 DoorDash store-page requests across 14 runs returned 403 "Just a moment…" | **Verified live** (from GitHub runners) |
| DoorDash store pages publish `Restaurant` + `Menu` JSON-LD: street address and `addressCountry` but no `postalCode`; prices as display strings (`"$8.53"`, `"US$12.75"`, `"$13.59+"`) without `priceCurrency`; the page's app data states `"currency":"USD"` (US) or `"CAD"` (Canada) | 15 Wayback Machine captures, Sep 2025 – 7 Oct 2026 (`scripts/archive_probe.py`, runs 37892678037, 37893226448, 37893861203; `live-verification/doordash-archive.json`) | **Verified on archived real pages**; reader handles it (9 US stores read with USD prices, 3 Canadian excluded, 3 without a Restaurant block reported as no menu data) |
| A DoorDash `+` price is a starting price | The same pages display `$$7.31+` for Taco Bell Chalupa Supreme and `$13.59+` for Lou Malnati's deep dish, next to the same JSON-LD string | **Verified on archived pages**; never treated as an exact price |
| Common Crawl's index for DoorDash store URLs | `index.commoncrawl.org/robots.txt` disallows the index queries for this client | **Honoured**; not read |
| DoorDash `/business/{slug}-{id}/` is not challenged and lists branch links; its menu is chain-level (`storeId: $undefined`, Next.js stream, no JSON-LD) | Live runs | **Verified live**; used for discovery only |
| DoorDash sitemaps are served from `cdn.doordash.com/sitemaps/`, with one store sitemap per state (`sitemap-doordash-<st>-stores.xml`, 131 in total) | Live run 37709849182 | **Verified live**; per-state files feed the location filter |
| DoorDash `/business/{slug}-{id}/menu` prices are not branch prices | The page text: "Prices are estimated based on your predicted location and may vary across store" | **Verified live**; not used for pricing |
| Grubhub restaurant pages are a JavaScript shell with no structured data; Grubhub declares no sitemap, and `/sitemap.xml` is 404 | Live runs 4–5 | **Verified live**; the extractor falls back to URL fields and manual entry |
| Uber Eats `/store/{slug}/{storeId}`, country sections `/<cc>[-<lang>]/store/` | Real sitemaps | **Verified live** |
| MenuPages (Grubhub-owned) publishes Grubhub restaurants' menus as JSON-LD with USD prices and links each page to its Grubhub listing | Runs 37713405667, 37741487660 | **Verified live**; used for Grubhub prices, matched by Grubhub ID |
| Published menus change by time of day | Run 37741924057: 18 McDonald's branches showed breakfast menus at 07:14 UTC; the same branches listed McNuggets at 00:39 UTC | **Verified live**; snapshot runs are kept separate |
| AllMenus (Grubhub-owned) JSON-LD has item names but no prices, name or address; prices only in HTML | Runs 37743302443, 37744579395 | **Verified live**; HTML prices used only when the HTML names equal the JSON-LD names |
| AllMenus restaurant sitemaps are unordered and its city pages list no restaurants in HTML | Run 37826699377 | **Verified live**; lookup scans the 21 sitemaps (cached 24 h) |
| Some real Uber Eats store pages publish Restaurant JSON-LD without a Menu | Run 37826699377 (military-base McDonald's) | **Verified live**; reported as "no menu data" |
| DoorDash `?pickup=false|true`, Uber Eats `?diningMode=` change the published prices | Uber Eats: 18 of 18 branches published the same price in both modes (runs 37886686091, 37888372974). DoorDash: store pages unreadable | Uber Eats: **no difference observed**; DoorDash: **unverified** (both recorded in `mode_basis`) |
| Uber Eats `__REACT_QUERY_STATE__` (2–3 MB, `\u0022`-escaped JSON) holds the app state | Structure probe | Observed; **not parsed** (undocumented) |
| Uber Eats `/_p/api/*`, DoorDash `graphql/consumer` | Third-party clients using logged-in sessions | **Not used**: private and session-bound |

### Live checks of the engine rules (real Uber Eats pages)

These ran on GitHub's runners against the real site (`scripts/snapshot.py`), and the results were ranked with the shipped engine (`node scripts/rank_snapshot.mjs`):

| Check | Run | Result |
|---|---|---|
| Cancel mid-run, then resume from the checkpoint in a new process (as after a restart) | [37887316803](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37887316803) | **Passed.** Cancelled after 2 store pages (status `cancelled`). The resumed job re-fetched none of the 4 handled stores, kept their timestamps, and read 20 new ones (18 menus). |
| Resume of a job that stopped at its per-run limit | [37886686091](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37886686091) | **Passed.** 6 stores done, 0 re-fetched, 6 new after resume. |
| Same-branch basket: Taco Bell Crunchwrap Supreme + Chalupa Supreme | 37886686091 | **6 complete baskets**, each at one branch. Lowest: **$14.01** ($7.31 + $6.70) at Fulton MS and Fort Oglethorpe GA (tie), "among 6 checked locations". |
| Pickup vs delivery (`?diningMode=PICKUP` vs `DELIVERY`), same item, same branches | 37886686091, [37888372974](https://github.com/Quack22e/prediction-market-analytics/actions/runs/37888372974) | Kept separate by the engine. **18 of 18 branches** read in both modes (9 McDonald's, 4 Lou Malnati's, 5 Taco Bell) published **identical** prices, e.g. $5.19 / $5.19 for 10 pc. McNuggets in Abingdon VA. Either these merchants price both modes alike, or Uber Eats' public page does not carry mode-specific prices; the data cannot tell which. Each observation's `mode_basis` says so. |
| Exact matching on real menus | all snapshot runs | 10 pc. McNuggets: 21–22 verified per run, $5.19–$8.55. Look-alikes rejected with reasons (20/40 pc, Meals, Happy Meal, breakfast items). |

## Alternative data sources

To find other ways to get this data, 24 methods were researched (October 2026). Each claim was re-checked by an independent search that tried to refute it, and a final pass looked for important methods the list missed (it added five, marked †). The same table, with links, is on the board. Main finding: **current prices for arbitrary US branches come only from public pages**. Every official API is limited to partners and to their own connected stores.

| Method | Platforms | Access | Branch prices | Verdict for this app |
|---|---|---|---|---|
| Uber Eats public store pages (schema.org JSON-LD) | Uber Eats | public; robots.txt allows | yes | **In use, verified live**. Uber's terms reportedly bar scraping scripts. |
| MenuPages | Grubhub | public; robots.txt allows | yes, for listed restaurants (28,133 pages) | **In use, verified live**. Grubhub terms: personal, non-commercial use. |
| AllMenus | Grubhub | public; robots.txt allows | partly (undated, sometimes stale) | **In use, verified live**. Used only when HTML and JSON-LD names agree. Same Grubhub terms. |
| Uber Eats brand-city pages | Uber Eats | public | no (sample prices not tied to a branch) | Discovery only |
| Capture bookmarklet (your browser) | DoorDash, Uber Eats | your action | yes for fixed prices; DoorDash publishes no modifiers, and `+` prices are starting prices | **Built**. Its DoorDash handling matches the real page layout (verified on archived pages). DoorDash's terms ban copying with scripts even when run by hand (high risk). |
| Prices you saw, typed in | DoorDash, Uber Eats | your action, your app | yes, for your branch, mode and account | **Built** (app and board). No scraping: you type what the platform shows you, with the store link for the store ID |
| Your order receipts | all three | your mailbox | no (past orders, no store ID) | **Built** (paste). Historical; good for your Grubhub side. |
| Personal data downloads ([Uber](https://help.uber.com/riders/article/whats-in-your-data-download?nodeId=3d476006-87a4-4404-ac1e-216825414e05), [DoorDash](https://help.doordash.com/en-us/consumers/article/i-want-to-delete-my-personal-information-data-from-doordash)) | Uber Eats, DoorDash | account holder, 1–3 days | no (no store address or URL) | Not built; your own past orders only |
| [DoorDash Marketplace API](https://developer.doordash.com/docs/marketplace/overview/about_marketplace) | DoorDash | approved POS/middleware partners, signed agreement | only the partner's own connected stores | Partnership only |
| [Uber Eats Marketplace APIs](https://developer.uber.com/docs/eats/guides/getting-started) | Uber Eats | NDA, API licence, business agreement | only stores that activated your app | Partnership only; Uber has used API terms against price comparison |
| [Grubhub partner/POS APIs](https://apis.io/apis/grubhub/grubhub-menu-retrieval-api/) | Grubhub | approved partners | integrated merchants only | Partnership only |
| [Menu middleware](https://developers.deliverect.com/docs/price-level-configuration) (Deliverect, Otter, Checkmate, Olo) | all three | partner agreement + each restaurant's consent | opted-in locations | The merchant's intended price, which the platform can override |
| [MealMe API](https://docs.mealme.ai/docs/menu-prices-faq) | all three | key request + signed licence | yes, but no DoorDash/Uber Eats listing | Cannot verify platform prices |
| [Edison Trends](https://datarade.ai/data-providers/edison-trends/profile) e-receipt panel | all three | enterprise licence | unconfirmed; lagged, sampled | Historical analytics only |
| [DoorDash CLI (dd-cli)](https://github.com/doordash-oss/doordash-cli) | DoorDash | invite-only, your login | partly | **Not usable**: its access terms forbid price comparison, aggregation and storing results |
| Unblocking scraping APIs (Zyte, ScraperAPI, Oxylabs, Bright Data …) | all three | self-serve | partly | **Not usable**: they defeat Cloudflare with fingerprint spoofing and CAPTCHA solving |
| Apify store actors | Uber Eats, Grubhub | self-serve | yes | **Not usable**: residential proxies or Grubhub's private api-gtm endpoints |
| DoorDash `/business/…/menu` | DoorDash | public | no ("estimated", verified live) | Discovery only |
| DoorDash Storefront (order.online) | DoorDash | public | first-party base price, not the DoorDash delivery price | **Not usable**; its terms ban scraping |
| Yelp menu pages | Grubhub | Places API has only a menu link | unknown | **Not usable**: DoorDash now powers most Yelp ordering; Yelp's terms forbid scraping |
| Wayback Machine, Common Crawl, Kaggle datasets | all three | free | historical only | Not current, so never used as prices. **Used as structure evidence:** 15 archived DoorDash store pages verified the DoorDash reader (`scripts/archive_probe.py`, existing captures only). Common Crawl's index robots.txt disallows the queries (honoured). Wayback is for research, non-commercial use; the Kaggle Uber Eats dump has no store ID, URL or timestamp and is for educational use |
| † Screenshots of the app's menu or cart (OCR, you confirm) | all three | your phone | yes, for your branch and mode | OCR not built; typing the prices in (row above) covers the same need |
| † Grubhub page captured in your own browser | Grubhub | your browser | yes (your Grubhub side) | Not built: the rendered Grubhub markup was never observed, so no selectors were written; Grubhub's terms forbid scraping |
| † [Uber Consumer Delivery APIs](https://developer.uber.com/docs/consumer-delivery/introduction) | Uber Eats | written approval from Uber | yes, if approved | Partnership only; the one official Uber surface that reaches arbitrary stores |
| † Distribution partnerships (Grubhub/Uber Eats in ChatGPT, Alexa+, Eater …) | all three | bilateral business deal | yes, for partners | Business deal |
| † [Universal Commerce Protocol food ordering](https://developers.google.com/actions-center/verticals/ordering/ucp) | DoorDash, Uber Eats | waitlist; food spec unpublished | unknown | Not available yet; worth watching |

## Access limitations and dependencies

1. **Network access.** The service needs outbound HTTPS to `www.doordash.com`, `www.ubereats.com` and, for link extraction, `www.grubhub.com`, `menupages.com` and `www.allmenus.com`. The Claude build sandbox is denied all three. A GitHub-hosted runner and ordinary machines are not. Run `python -m server.inspect_page --probe` first.
2. **DoorDash prices (blocked, verified).** DoorDash store pages served a Cloudflare challenge to every request from GitHub's datacenter IPs (69 of 69 across 14 runs). The app reports `captcha` barriers and its circuit breaker stops the adapter. It does not try to defeat the challenge. From other networks DoorDash may or may not serve the page; the run log shows which. Without readable store pages there are **no scraped DoorDash branch prices**, and the DoorDash chain-page menu is not branch-specific. When a store page is readable, the reader handles DoorDash's real layout (verified on 15 archived pages, see the evidence log). DoorDash prices you see in the app can always be typed in.
3. **Uber Eats bot checks (intermittent, verified).** Two of five store fetches in run 3, and two of three in run 2, were redirected to Uber's challenge page. These are reported and skipped. Resume tries the remaining queue later.
4. **What the published data cannot tell you.**
   * Uber Eats JSON-LD has no add-on, option or availability data. An order with modifiers therefore always "needs confirmation", and the availability of live rows is "unknown".
   * `$0.00` listings are unverifiable.
   * No fees, taxes or account-dependent promotions are published, so checkout totals exist only for imported data that includes all destination charges.
5. **Order mode.** Live prices come from an anonymous storefront with no address. `mode_basis` states how the mode was requested. On Uber Eats, 18 of 18 branches read in both modes published identical prices, so the public page may not carry mode-specific prices. DoorDash's archived pages carry the checkout message "Item prices may differ on Delivery and Pickup" among their interface strings, so its two modes can differ; its mode parameter is unverified.
6. **Grubhub prices.** Real Grubhub restaurant pages publish no structured data in their HTML; the content is rendered by JavaScript. Prices come from MenuPages, then AllMenus. Both are Grubhub-owned and together cover only a subset of Grubhub restaurants, and a page is used only when it links to the same Grubhub ID. AllMenus says its menus may not be current. MenuPages prices are menu prices; option charges and fees are not included, and they may differ from what a signed-in Grubhub checkout shows. Otherwise the extractor yields the restaurant ID and a chain-name guess from the URL, flags the guess, and asks for manual item entry.
7. **Terms of service.** Robots.txt allowing crawling (verified for all five sites) is not the same as permission, and you are responsible for whether and how you run the scrapers.
   * Grubhub's Terms of Use prohibit automated access without written authorization. The MenuPages terms say Grubhub's sites, MenuPages included, are "made available solely for your personal, non-commercial use" unless Grubhub agrees otherwise in writing, and AllMenus is covered by the same Grubhub terms. Set `FPC_DISABLE_GRUBHUB_MIRRORS=1` to switch both lookups off.
   * Uber's terms reportedly bar scripts that scrape or data-mine the service. The US wording was not confirmed.
   * DoorDash's terms bar accessing its sites "manually or with any robot, spider, script, … to scrape, copy, index, frame, or monitor" them. That covers the capture bookmarklet too, even though you run it yourself in your own browser.
   * The import path works with data obtained by other permitted means.
8. **Coverage.** Nationwide coverage is built up across bounded, resumable runs from seeds, chain pages and declared sitemaps. Uber Eats declares hundreds of sitemap files; DoorDash declares 131 per-state store files of up to ~100k URLs each. It is never exhaustive by construction, and the UI never claims "cheapest in the US".

## Files

```
food-price-compare/
├── index.html                 # complete frontend (UI + FPCEngine)
├── server/
│   ├── app.py                 # HTTP API + static index.html
│   ├── adapters.py            # DoorDash / Uber Eats adapters, Grubhub extraction, inspector
│   ├── grubhub_mirror.py      # Grubhub prices via MenuPages, then AllMenus (Grubhub-owned), matched by Grubhub ID
│   ├── jobs.py                # resumable, checkpointed, cancellable jobs
│   ├── http_client.py         # robots, rate limit, retry/backoff, cache, barriers, Playwright
│   ├── robots.py              # RFC 9309 robots.txt
│   ├── extract.py             # schema.org JSON-LD, addresses, prices, sitemaps
│   ├── inspect_page.py        # evidence harness CLI (--probe, --browser, --save)
│   ├── structure_probe.py     # how a real page publishes its data (evidence gathering)
│   └── config.py
├── examples/                  # import format examples (fictional chain)
├── board/                     # static price board: template.html, sources.json, built index.html
├── scripts/live_verify.py     # bounded live run (used by .github/workflows/fpc-live-verification.yml)
├── scripts/archive_probe.py   # DoorDash store-page structure from existing archive captures (.github/workflows/fpc-archive-probe.yml)
├── scripts/snapshot.py        # bounded live searches → live-verification/snapshot.json (board data)
├── scripts/build_board.py     # builds board/index.html from the template, engine and snapshots
├── scripts/rank_report.mjs    # rank a live report's real observations with the shipped engine
├── live-verification/         # latest live report (committed by the workflow)
├── tests/                     # pytest, node:test engine + Playwright UI e2e, synthetic fixtures,
│                              # test_real_captures.py + captures/ (real pages; git-ignored)
└── requirements.txt
```

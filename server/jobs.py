"""Resumable, cancellable, checkpointed search jobs.

A job discovers branches of the requested chain on each platform and fetches
their public menus with bounded concurrency. Progress is checkpointed to
disk after every store so a job can be resumed after cancellation, a per-run
limit, or a server restart. Each run is bounded (stores and sitemap documents
per run); nationwide coverage is built up across resumed runs rather than by
an uncontrolled crawl.

What is persisted: the chain/item names needed for discovery, discovered
store URLs and their status, and the public menu observations. Not persisted:
receipt text, Grubhub prices, delivery addresses or local-filter values.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import (
    PlatformAdapter, build_observation, make_adapters, order_prefilter, parse_store_page,
    store_links_from_page,
)
from .config import Config
from .extract import extract_menu_objects, loose_norm, parse_jsonld_blocks, parse_sitemap, scan_html, slug_tokens
from .http_client import Cancelled, PoliteClient, barrier, utcnow_iso

STORE_TERMINAL = {"checked", "no_menu", "non_us", "blocked", "failed", "duplicate"}
IMMEDIATE_STOP = {"egress_denied", "robots_disallow", "rate_limited", "not_allowed"}
MAX_EVENTS_PER_POLL = 400


def _sanitize_request(req: dict, cfg: Config) -> dict:
    order = req.get("order") or {}
    items = []
    for it in (order.get("items") or [])[:20]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:200]
        if not name:
            continue
        items.append({
            "name": name,
            "aliases": [str(a)[:200] for a in (it.get("aliases") or [])][:10],
            "flavor": str(it.get("flavor") or "")[:100],
        })
    platforms = [p for p in (req.get("platforms") or ["doordash", "ubereats"]) if p in ("doordash", "ubereats")]
    mode = req.get("order_mode") if req.get("order_mode") in ("delivery", "pickup") else "delivery"
    fetch_mode = req.get("fetch_mode") if req.get("fetch_mode") in ("http", "browser", "auto") else "http"
    try:
        max_stores = int(req.get("max_stores_per_platform") or 25)
    except (TypeError, ValueError):
        max_stores = 25
    seeds = [str(s).strip() for s in (req.get("seed_urls") or []) if str(s).strip()][:200]
    return {
        "order": {"chain": str(order.get("chain") or "").strip()[:200],
                  "chain_aliases": [str(a)[:200] for a in (order.get("chain_aliases") or [])][:10],
                  "items": items},
        "platforms": platforms,
        "order_mode": mode,
        "fetch_mode": fetch_mode,
        "max_stores_per_platform": max(1, min(cfg.max_stores_cap, max_stores)),
        "seed_urls": seeds,
    }


def _new_platform_state() -> dict:
    return {
        "state": "pending", "stop_reason": None, "seeded": False,
        "sitemaps": {"pending": [], "done": [], "failed": [], "declared": 0},
        "chain_pages": {"pending": [], "done": [], "failed": []},
        "stores": {}, "consecutive_failures": 0, "runs": 0, "barriers": [],
    }


class Job:
    def __init__(self, mgr: "JobManager", state: dict):
        self.mgr = mgr
        self.cfg = mgr.cfg
        self.client = mgr.client
        self.state = state
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_save = 0.0
        self.prioritize: dict = {}

    @property
    def id(self) -> str:
        return self.state["id"]

    # -- persistence & events ---------------------------------------------
    def emit(self, etype: str, **data) -> None:
        with self.lock:
            self.state["seq"] += 1
            self.state["events"].append({"seq": self.state["seq"], "ts": utcnow_iso(), "type": etype, **data})

    def save(self, force: bool = False) -> None:
        with self.lock:
            now = time.monotonic()
            if not force and now - self._last_save < 1.0:
                return
            self._last_save = now
            self.state["updated_at"] = utcnow_iso()
            path = self.mgr.job_path(self.id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, path)

    def counters(self, platform: str) -> dict:
        ps = self.state["platforms"][platform]
        c = {k: 0 for k in ("discovered", "queued", "checked", "no_menu", "non_us", "blocked", "failed",
                            "duplicate", "in_progress")}
        for s in ps["stores"].values():
            c["discovered"] += 1
            st = s["status"]
            if st in c:
                c[st] += 1
        c["remaining"] = c["queued"] + c["in_progress"]
        sm = ps["sitemaps"]
        c["sitemaps_pending"], c["sitemaps_done"], c["sitemaps_failed"] = len(sm["pending"]), len(sm["done"]), len(sm["failed"])
        c["chain_pages_pending"] = len(ps["chain_pages"]["pending"])
        # The sitemap universe is fully enumerated only when every declared
        # sitemap document was read without failure. That still does not prove
        # the sitemaps list every store currently on the marketplace.
        c["sitemap_enumeration_complete"] = bool(sm["declared"]) and not sm["pending"] and not sm["failed"]
        return c

    def summary(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "status": self.state["status"], "created_at": self.state["created_at"],
                "updated_at": self.state.get("updated_at"), "request": self.state["request"],
                "platforms": {p: {"state": ps["state"], "stop_reason": ps["stop_reason"], "runs": ps["runs"],
                                  "counters": self.counters(p)}
                              for p, ps in self.state["platforms"].items()},
                "seq": self.state["seq"],
                "coverage_note": ("Coverage is limited to branches discovered via supplied URLs, chain pages and "
                                  "robots.txt-declared sitemaps. The full set of US branches on each platform is "
                                  "not known, so results are never described as exhaustive."),
            }

    # -- running ------------------------------------------------------------
    def start(self, prioritize: dict | None = None) -> None:
        self.prioritize = prioritize or {}
        self.cancel_event = threading.Event()
        with self.lock:
            self.state["status"] = "running"
            for ps in self.state["platforms"].values():
                for s in ps["stores"].values():
                    if s["status"] == "in_progress":
                        s["status"] = "queued"
                if ps["state"] not in ("stopped",):
                    ps["state"] = "pending"
        self.emit("job", status="running")
        self.save(force=True)
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"job-{self.id}")
        self.thread.start()

    def cancel(self) -> None:
        with self.lock:
            if self.state["status"] == "running":
                self.state["status"] = "cancelling"
        self.cancel_event.set()
        self.emit("job", status="cancelling")

    def _run(self) -> None:
        req = self.state["request"]
        threads = []
        for p in req["platforms"]:
            t = threading.Thread(target=self._run_platform_safe, args=(p,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        with self.lock:
            states = [ps["state"] for ps in self.state["platforms"].values()]
            if self.cancel_event.is_set():
                status = "cancelled"
            elif any(s == "paused_limit" for s in states):
                status = "paused"
            elif all(s == "stopped" for s in states):
                status = "stopped"
            else:
                status = "completed"
            self.state["status"] = status
        self.emit("job", status=status)
        self.save(force=True)

    def _run_platform_safe(self, platform: str) -> None:
        try:
            self._run_platform(platform)
        except Cancelled:
            with self.lock:
                self.state["platforms"][platform]["state"] = "cancelled"
            self.emit("platform", platform=platform, state="cancelled")
        except Exception as exc:  # unexpected bug: report, keep checkpoint
            with self.lock:
                ps = self.state["platforms"][platform]
                ps["state"] = "stopped"
                ps["stop_reason"] = {"kind": "internal_error", "detail": f"{type(exc).__name__}: {exc}"}
            self.emit("log", level="error", platform=platform, message=traceback.format_exc()[-1500:])
            self.emit("platform", platform=platform, state="stopped", reason=ps["stop_reason"])
        finally:
            self.save(force=True)

    def _stop(self, platform: str, reason: dict) -> None:
        with self.lock:
            ps = self.state["platforms"][platform]
            ps["state"] = "stopped"
            ps["stop_reason"] = reason
        self.emit("platform", platform=platform, state="stopped", reason=reason)

    def _set_state(self, platform: str, state: str) -> None:
        with self.lock:
            self.state["platforms"][platform]["state"] = state
        self.emit("platform", platform=platform, state=state)

    def _record_barrier(self, platform: str, b: dict) -> None:
        with self.lock:
            bl = self.state["platforms"][platform]["barriers"]
            bl.append(b)
            del bl[:-50]
        self.emit("barrier", platform=platform, barrier=b)

    def _add_store(self, platform: str, adapter: PlatformAdapter, url: str, source: str, priority: int) -> bool:
        ref = adapter.parse_store_url(url)
        if not ref:
            return False
        with self.lock:
            stores = self.state["platforms"][platform]["stores"]
            key = ref["store_id"]
            if key in stores:
                if priority < stores[key]["priority"] and stores[key]["status"] == "queued":
                    stores[key]["priority"] = priority
                return False
            if ref.get("country") and ref["country"] != "us":
                # Non-US country section of the site: recorded, never queued
                # (so it cannot consume the per-run fetch budget).
                stores[key] = {**ref, "source": source, "priority": priority, "status": "non_us",
                               "detail": f"URL is in the '/{ref['country']}/' country section; not fetched"}
                return True
            stores[key] = {**ref, "source": source, "priority": priority, "status": "queued", "detail": ""}
        return True

    def _chain_tokens(self) -> list[str]:
        return slug_tokens(self.state["request"]["order"]["chain"])

    def _priority_for(self, slug: str, base: int) -> int:
        toks = set(slug_tokens(slug))
        city = loose_norm(self.prioritize.get("city") or "").split()
        if city and all(t in toks for t in city):
            return base - 1
        return base

    def _seed(self, platform: str, adapter: PlatformAdapter, rules) -> None:
        ps = self.state["platforms"][platform]
        chain_toks = self._chain_tokens()
        n = 0
        for url in self.state["request"]["seed_urls"]:
            try:
                host = urlsplit(url).netloc.lower()
            except ValueError:
                continue
            if host != adapter.host:
                continue
            if adapter.parse_store_url(url):
                n += self._add_store(platform, adapter, url, "seed", 0)
            else:
                ps["chain_pages"]["pending"].append(url)
        own = [s for s in rules.sitemaps if urlsplit(s).netloc.lower() == adapter.host]
        ps["sitemaps"]["declared"] = len(own)
        ps["sitemaps"]["pending"].extend(own)
        ps["seeded"] = True
        self.emit("log", level="info", platform=platform,
                  message=f"seeded {n} store URL(s), {len(ps['chain_pages']['pending'])} chain page(s), "
                          f"{len(own)} robots.txt-declared sitemap(s); chain tokens {chain_toks}")

    def _process_chain_page(self, platform: str, adapter: PlatformAdapter, url: str) -> None:
        ps = self.state["platforms"][platform]
        out = self.client.fetch(url, kind="chain", cancel=self.cancel_event, ttl=self.cfg.cache_ttl_sitemap_s)
        if out.barrier:
            self._record_barrier(platform, out.barrier)
        if not out.ok:
            ps["chain_pages"]["failed"].append(url)
            if out.barrier and out.barrier["kind"] in IMMEDIATE_STOP:
                self._stop(platform, out.barrier)
            return
        links, nxt = store_links_from_page(adapter, out.text, out.final_url or url)
        toks = self._chain_tokens()
        added = 0
        for link in links:
            ref = adapter.parse_store_url(link)
            if toks and ref["slug"] and not adapter.slug_matches_chain(ref["slug"], toks):
                continue
            added += self._add_store(platform, adapter, link, "chain_page", self._priority_for(ref["slug"], 2))
        ps["chain_pages"]["done"].append(url)
        pages_seen = len(ps["chain_pages"]["done"]) + len(ps["chain_pages"]["failed"])
        for u in nxt:
            if pages_seen < self.cfg.max_chain_pages and u not in ps["chain_pages"]["done"] and u not in ps["chain_pages"]["pending"]:
                ps["chain_pages"]["pending"].append(u)
        self.emit("discovered", platform=platform, source="chain_page", url=url, count=added)

    def _process_sitemap(self, platform: str, adapter: PlatformAdapter, url: str) -> None:
        ps = self.state["platforms"][platform]
        sm = ps["sitemaps"]
        out = self.client.fetch(url, kind="sitemap", cancel=self.cancel_event, ttl=self.cfg.cache_ttl_sitemap_s)
        if out.barrier:
            self._record_barrier(platform, out.barrier)
        if not out.ok:
            sm["failed"].append(url)
            if out.barrier and out.barrier["kind"] in IMMEDIATE_STOP:
                self._stop(platform, out.barrier)
            return
        parsed = parse_sitemap(out.body or b"", self.cfg.max_sitemap_bytes)
        if parsed["kind"] == "invalid":
            sm["failed"].append(url)
            self.emit("log", level="warn", platform=platform, message=f"sitemap {url}: {parsed['error']}")
            return
        toks = self._chain_tokens()
        added = 0
        if parsed["kind"] == "index":
            children = [l["loc"] for l in parsed["locs"] if urlsplit(l["loc"]).netloc.lower() == adapter.host]
            # Read store/brand sitemaps first; everything stays queued for resume.
            def rank(u):
                lu = u.lower()
                return (0 if any(k in lu for k in ("store", "business", "brand")) else 1, lu)
            for c in sorted(children, key=rank):
                if c not in sm["done"] and c not in sm["pending"] and c not in sm["failed"]:
                    sm["pending"].append(c)
                    sm["declared"] += 1
        else:
            for l in parsed["locs"]:
                loc = l["loc"]
                ref = adapter.parse_store_url(loc)
                if ref:
                    if adapter.slug_matches_chain(ref["slug"], toks):
                        added += self._add_store(platform, adapter, loc, "sitemap", self._priority_for(ref["slug"], 3))
                elif adapter.is_chain_page(loc) and toks:
                    m = adapter.chain_path_re.match(urlsplit(loc).path)
                    if m and adapter.slug_matches_chain(m.group("slug"), toks):
                        if loc not in ps["chain_pages"]["done"] and loc not in ps["chain_pages"]["pending"]:
                            ps["chain_pages"]["pending"].append(loc)
        sm["done"].append(url)
        self.emit("discovered", platform=platform, source="sitemap", url=url, count=added, kind=parsed["kind"])

    def _fetch_store(self, platform: str, adapter: PlatformAdapter, key: str, prefilter: set[str]) -> None:
        req = self.state["request"]
        with self.lock:
            ps = self.state["platforms"][platform]
            ref = dict(ps["stores"][key])
            if ps["state"] == "stopped":
                # Adapter was stopped (barrier/breaker) while this was queued.
                ps["stores"][key]["status"] = "queued"
                return
        mode = req["order_mode"]
        if ref.get("country") and ref["country"] != "us":
            # Non-US country section of the site: excluded without a request.
            with self.lock:
                s = self.state["platforms"][platform]["stores"][key]
                s["status"], s["detail"], s["checked_at"] = "non_us", f"URL is in the '/{ref['country']}/' country section; not fetched", utcnow_iso()
            self.emit("store", platform=platform, store_key=key, status="non_us", detail=s["detail"], url=ref["canonical"])
            return
        url, mode_basis = adapter.mode_url(ref["canonical"], mode)
        use_browser = req["fetch_mode"] == "browser"
        out = self.client.fetch(url, kind="page", cancel=self.cancel_event, use_browser=use_browser)
        parsed = None
        if out.ok:
            parsed = parse_store_page(out.text, out.final_url or url, ref["store_id"])
            if not parsed["restaurant_found"] and req["fetch_mode"] == "auto":
                # Page answered normally but carried no structured data: render
                # it with JavaScript. (Never used to get past a block/challenge.)
                rendered = self.client.fetch(url, kind="page", cancel=self.cancel_event, use_browser=True)
                if rendered.ok:
                    out, parsed = rendered, parse_store_page(rendered.text, rendered.final_url or url, ref["store_id"])
                elif rendered.barrier:
                    # The HTTP page itself was fine; only the render was blocked.
                    # Keep the store as "no menu data" and report the barrier.
                    self._record_barrier(platform, dict(rendered.barrier, during="browser render (auto mode)"))
                    parsed["render_barrier"] = rendered.barrier["kind"]
            if parsed and parsed["restaurant_found"] and not parsed["items"]:
                for murl in parsed["menu_urls"][:1]:
                    if urlsplit(murl).netloc.lower() == adapter.host:
                        mo = self.client.fetch(murl, kind="page", cancel=self.cancel_event, use_browser=use_browser)
                        if mo.ok:
                            objs, _ = parse_jsonld_blocks(scan_html(mo.text, murl).jsonld_raw)
                            parsed["items"] = extract_menu_objects(objs)[0]
        if out.barrier:
            status, detail, obs = "blocked", f"{out.barrier['kind']}: {out.barrier['detail']}", None
            self._record_barrier(platform, out.barrier)
        elif not out.ok:
            status, detail, obs = "failed", out.error or "fetch failed", None
        else:
            status, obs, detail = build_observation(adapter, ref, parsed, out, mode, mode_basis, prefilter)
        if obs:
            with self.lock:
                dup = self._find_duplicate(platform, key, obs)
            if dup:
                # Same physical branch listed twice on one platform. Counted
                # once for coverage, but the listing is kept (flagged) because
                # its price is a real, separately orderable price.
                status, detail = "duplicate", f"same street address and ZIP as store {dup}; listing kept and flagged"
                obs["duplicate_of"] = dup
        with self.lock:
            ps = self.state["platforms"][platform]
            s = ps["stores"][key]
            s["status"], s["detail"], s["checked_at"] = status, detail[:400], utcnow_iso()
            if obs:
                if status == "checked":
                    s["address_key"] = self._address_key(obs)
                self.state["observations"].append(obs)
            if status in ("blocked", "failed"):
                ps["consecutive_failures"] += 1
            else:
                ps["consecutive_failures"] = 0
        if obs:
            self.emit("observation", platform=platform, store_key=key, observation=obs)
        self.emit("store", platform=platform, store_key=key, status=status, detail=detail[:400], url=ref["canonical"])
        if out.barrier and out.barrier["kind"] in IMMEDIATE_STOP:
            self._stop(platform, out.barrier)
        elif ps["consecutive_failures"] >= self.cfg.breaker_threshold:
            self._stop(platform, {"kind": "circuit_breaker",
                                  "detail": f"{ps['consecutive_failures']} consecutive blocked/failed store fetches; "
                                            "adapter stopped to avoid hammering the site"})
        self.save()

    @staticmethod
    def _address_key(obs: dict) -> str:
        a = obs.get("address") or {}
        return f"{loose_norm(a.get('street', ''))}|{a.get('zip', '')}"

    def _find_duplicate(self, platform: str, key: str, obs: dict) -> str | None:
        ak = self._address_key(obs)
        if ak.startswith("|"):
            return None
        for k, s in self.state["platforms"][platform]["stores"].items():
            if k != key and s.get("address_key") == ak and s["status"] == "checked":
                return k
        return None

    def _run_platform(self, platform: str) -> None:
        adapter: PlatformAdapter = self.mgr.adapters[platform]
        req = self.state["request"]
        with self.lock:
            ps = self.state["platforms"][platform]
            if ps["state"] == "stopped":
                ps["state"], ps["stop_reason"], ps["consecutive_failures"] = "pending", None, 0
            ps["runs"] += 1
        self._set_state(platform, "discovering")
        rules, rb = self.client.robots(adapter.base, self.cancel_event)
        if rb and rb["kind"] in ("egress_denied", "network"):
            self._record_barrier(platform, rb)
            self._stop(platform, rb)
            return
        if rules.disallow_all or not rules.can_fetch(adapter.sample_store_url()):
            why = rules.note or "robots.txt disallows store pages for this crawler"
            b = barrier("robots_disallow", why, adapter.base + "/robots.txt", rules.source_status)
            if rb:
                b["cause"] = rb
            self._record_barrier(platform, b)
            self._stop(platform, b)
            return
        if not ps["seeded"]:
            with self.lock:
                self._seed(platform, adapter, rules)
        prefilter = order_prefilter(req["order"])
        budget_stores = req["max_stores_per_platform"]
        budget_sitemaps = self.cfg.max_sitemaps_per_run
        fetched = 0
        pool = ThreadPoolExecutor(max_workers=self.cfg.concurrency_per_platform)
        try:
            while not self.cancel_event.is_set():
                if ps["state"] == "stopped":
                    return
                with self.lock:
                    queued = sorted((s["priority"], k) for k, s in ps["stores"].items() if s["status"] == "queued")
                if queued and fetched < budget_stores:
                    self._set_state(platform, "fetching") if ps["state"] != "fetching" else None
                    batch = [k for _, k in queued[:min(self.cfg.concurrency_per_platform, budget_stores - fetched)]]
                    with self.lock:
                        for k in batch:
                            ps["stores"][k]["status"] = "in_progress"
                    futures = [pool.submit(self._fetch_store, platform, adapter, k, prefilter) for k in batch]
                    for f, k in zip(futures, batch):
                        try:
                            f.result()
                        except Cancelled:
                            with self.lock:
                                ps["stores"][k]["status"] = "queued"
                        except Exception as exc:
                            with self.lock:
                                ps["stores"][k]["status"] = "failed"
                                ps["stores"][k]["detail"] = f"internal error: {exc}"
                    fetched += len(batch)
                    continue
                if fetched >= budget_stores and (queued or ps["sitemaps"]["pending"] or ps["chain_pages"]["pending"]):
                    self._set_state(platform, "paused_limit")
                    self.emit("log", level="info", platform=platform,
                              message=f"per-run limit of {budget_stores} store fetches reached; resume to continue")
                    return
                if ps["chain_pages"]["pending"]:
                    url = ps["chain_pages"]["pending"].pop(0)
                    self._set_state(platform, "discovering") if ps["state"] != "discovering" else None
                    self._process_chain_page(platform, adapter, url)
                    self.save()
                    continue
                if ps["sitemaps"]["pending"] and budget_sitemaps > 0:
                    url = ps["sitemaps"]["pending"].pop(0)
                    budget_sitemaps -= 1
                    self._set_state(platform, "discovering") if ps["state"] != "discovering" else None
                    try:
                        self._process_sitemap(platform, adapter, url)
                    except Cancelled:
                        ps["sitemaps"]["pending"].insert(0, url)
                        raise
                    self.save()
                    continue
                if ps["sitemaps"]["pending"]:
                    self._set_state(platform, "paused_limit")
                    self.emit("log", level="info", platform=platform,
                              message="per-run sitemap budget reached; resume to continue discovery")
                    return
                self._set_state(platform, "done")
                return
            raise Cancelled()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


class JobManager:
    def __init__(self, cfg: Config, client: PoliteClient):
        self.cfg = cfg
        self.client = client
        self.adapters = make_adapters(cfg.platform_bases)
        self.jobs: dict[str, Job] = {}
        self.dir = cfg.data_dir / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def job_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def _load(self, job_id: str) -> Job | None:
        if not job_id.replace("-", "").isalnum():
            return None
        with self._lock:
            if job_id in self.jobs:
                return self.jobs[job_id]
            p = self.job_path(job_id)
            if not p.exists():
                return None
            state = json.loads(p.read_text())
            if state["status"] in ("running", "cancelling"):
                state["status"] = "interrupted"
            job = Job(self, state)
            self.jobs[job_id] = job
            return job

    def start(self, request: dict) -> Job:
        req = _sanitize_request(request, self.cfg)
        if not req["order"]["chain"]:
            raise ValueError("order.chain (restaurant/chain name) is required for branch discovery")
        if not req["order"]["items"]:
            raise ValueError("at least one order item with a name is required")
        if not req["platforms"]:
            raise ValueError("select at least one platform")
        job_id = uuid.uuid4().hex[:16]
        state = {"id": job_id, "version": 1, "created_at": utcnow_iso(), "status": "queued", "request": req,
                 "platforms": {p: _new_platform_state() for p in req["platforms"]},
                 "observations": [], "events": [], "seq": 0}
        job = Job(self, state)
        with self._lock:
            self.jobs[job_id] = job
        job.start(prioritize=(request.get("prioritize") or {}))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._load(job_id)

    def resume(self, job_id: str, prioritize: dict | None = None) -> Job:
        job = self._load(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state["status"] in ("running", "cancelling") and job.thread and job.thread.is_alive():
            raise ValueError("job is already running")
        job.start(prioritize=prioritize)
        return job

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            job = self._load(p.stem)
            if job:
                s = job.summary()
                out.append({"id": s["id"], "status": s["status"], "created_at": s["created_at"],
                            "updated_at": s["updated_at"], "chain": s["request"]["order"]["chain"],
                            "items": [i["name"] for i in s["request"]["order"]["items"]],
                            "order_mode": s["request"]["order_mode"],
                            "platforms": {k: v["counters"] for k, v in s["platforms"].items()}})
        return out

    def delete(self, job_id: str) -> bool:
        job = self._load(job_id)
        if job is None:
            return False
        job.cancel()
        if job.thread:
            job.thread.join(timeout=30)
        with self._lock:
            self.jobs.pop(job_id, None)
        try:
            self.job_path(job_id).unlink()
        except OSError:
            pass
        return True

    def purge(self) -> dict:
        ids = {p.stem for p in self.dir.glob("*.json")} | set(self.jobs)
        for jid in list(self.jobs):
            self.delete(jid)
        for p in self.dir.glob("*"):
            p.unlink()
        return {"jobs_deleted": len(ids), "cache_files_deleted": self.client.cache.purge()}

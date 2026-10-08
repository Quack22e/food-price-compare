"""Polite HTTP access: allowlist, robots.txt, rate limits, retries, cache,
and explicit classification of access barriers.

Barriers (login walls, CAPTCHA / bot challenges, 401/403, rate limiting,
robots.txt disallow, network-policy denial) are reported, never bypassed:
no stealth plugins, no TLS impersonation, no challenge solving, no proxies
beyond the operator's own configured HTTPS_PROXY.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .config import Config
from .robots import RobotsRules, rules_for_status

# Markers of interstitial bot-challenge pages served by common anti-bot
# vendors. Used only to *recognise and report* a challenge.
STRONG_CHALLENGE_MARKERS = (
    "cf-chl-", "challenges.cloudflare.com", "<title>just a moment...</title>",
    "attention required! | cloudflare", "px-captcha", "captcha-delivery.com",
    "access to this page has been denied", "_incapsula_resource", "perimeterx",
)
WEAK_CHALLENGE_MARKERS = ("g-recaptcha", "h-captcha", "hcaptcha.com", "recaptcha/api.js")
LOGIN_PATH_HINTS = ("/login", "/signin", "/sign-in", "/auth", "/consumer/login")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class FetchOutcome:
    url: str
    ok: bool = False
    status: int | None = None
    final_url: str | None = None
    body: bytes | None = None
    content_type: str = ""
    fetched_at: str = ""
    from_cache: bool = False
    via: str = "http"
    barrier: dict | None = None
    error: str | None = None
    attempts: int = 0

    @property
    def text(self) -> str:
        if self.body is None:
            return ""
        return self.body.decode("utf-8", errors="replace")

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("body", None)
        d["bytes"] = len(self.body or b"")
        return d


class Cancelled(Exception):
    pass


def barrier(kind: str, detail: str, url: str, status: int | None = None, **extra) -> dict:
    return {"kind": kind, "detail": detail, "url": url, "host": urlsplit(url).netloc,
            "status": status, "observed_at": utcnow_iso(), **extra}


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def classify_response(url: str, status: int, final_url: str, body: bytes, content_type: str) -> dict | None:
    """Return a barrier dict when the response is an access barrier."""
    head = body[:200_000].decode("utf-8", errors="replace").lower() if body else ""
    final_path = urlsplit(final_url or url).path.lower()
    if final_url and final_url != url and any(h in final_path for h in LOGIN_PATH_HINTS):
        return barrier("login_wall", f"redirected to a login page ({final_url})", url, status)
    if any(m in head for m in STRONG_CHALLENGE_MARKERS):
        return barrier("captcha", "bot-challenge / CAPTCHA interstitial returned", url, status)
    if status == 401:
        return barrier("login_wall", "HTTP 401: authentication required", url, status)
    if status == 403:
        if any(m in head for m in WEAK_CHALLENGE_MARKERS):
            return barrier("captcha", "HTTP 403 with CAPTCHA widget", url, status)
        return barrier("forbidden", "HTTP 403: access denied by site", url, status)
    if status == 429:
        return barrier("rate_limited", "HTTP 429: rate limited", url, status)
    return None


class DiskCache:
    def __init__(self, root: Path):
        self.root = root / "cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _key(self, kind: str, url: str) -> str:
        return hashlib.sha256(f"{kind}|{url}".encode()).hexdigest()

    def get(self, kind: str, url: str, ttl_s: int) -> FetchOutcome | None:
        k = self._key(kind, url)
        meta_p, body_p = self.root / f"{k}.json", self.root / f"{k}.bin"
        try:
            meta = json.loads(meta_p.read_text())
            if time.time() - meta["stored_epoch"] > ttl_s:
                return None
            body = body_p.read_bytes()
        except (OSError, ValueError, KeyError):
            return None
        return FetchOutcome(url=url, ok=True, status=meta["status"], final_url=meta.get("final_url"),
                            body=body, content_type=meta.get("content_type", ""),
                            fetched_at=meta["fetched_at"], from_cache=True, via=meta.get("via", "http"))

    def put(self, kind: str, out: FetchOutcome) -> None:
        k = self._key(kind, out.url)
        with self._lock:
            (self.root / f"{k}.bin").write_bytes(out.body or b"")
            (self.root / f"{k}.json").write_text(json.dumps({
                "url": out.url, "status": out.status, "final_url": out.final_url,
                "content_type": out.content_type, "fetched_at": out.fetched_at,
                "stored_epoch": time.time(), "via": out.via,
            }))

    def purge(self) -> int:
        n = 0
        for p in self.root.glob("*"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n


class HostRateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str, interval: float, cancel: threading.Event | None) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + interval
        delay = slot - time.monotonic()
        if delay > 0:
            if cancel is not None:
                if cancel.wait(delay):
                    raise Cancelled()
            else:
                time.sleep(delay)


class BrowserRenderer:
    """Optional Playwright renderer for pages that need JavaScript.

    Runs a single headless Chromium in its own thread (the sync Playwright API
    is not thread-safe). Each render uses a fresh context: no cookies, no login,
    images/media/fonts are not downloaded. The browser's own user agent is
    used unchanged.
    """

    def __init__(self, timeout_s: float):
        self.timeout_ms = int(timeout_s * 1000)
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_error: str | None = None
        self._ready = threading.Event()

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False, "Python package 'playwright' is not installed"
        return True, "playwright importable"

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            # Must be set before the driver starts: lets NO_PROXY loopback
            # hosts bypass the operator's proxy (Playwright proxies them by default).
            os.environ.setdefault("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
            pw = sync_playwright().start()
            launch = {"headless": True}
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:
                # Playwright proxies loopback by default; honour NO_PROXY.
                bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "localhost,127.0.0.1"
                launch["proxy"] = {"server": proxy, "bypass": bypass}
            if os.path.exists("/opt/pw-browsers/chromium") and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
                launch["executable_path"] = "/opt/pw-browsers/chromium"
            browser = pw.chromium.launch(**launch)
        except Exception as exc:  # pragma: no cover - environment dependent
            self._start_error = f"browser failed to start: {exc}"
            self._ready.set()
            return
        self._ready.set()
        while True:
            job = self._q.get()
            if job is None:
                break
            url, fut = job
            ctx = None
            try:
                ctx = browser.new_context(java_script_enabled=True)
                ctx.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "media", "font") else route.continue_())
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                fut.put(("ok", resp.status if resp else None, page.url, page.content()))
            except Exception as exc:
                fut.put(("error", None, None, str(exc)))
            finally:
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        browser.close()
        pw.stop()

    def render(self, url: str, cancel: threading.Event | None = None):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="fpc-browser")
            self._thread.start()
        self._ready.wait(60)
        if self._start_error:
            return ("error", None, None, self._start_error)
        fut: queue.Queue = queue.Queue(maxsize=1)
        self._q.put((url, fut))
        deadline = time.monotonic() + self.timeout_ms / 1000 + 20
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                return fut.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() > deadline:
                    return ("error", None, None, "browser render timed out")


class PoliteClient:
    def __init__(self, cfg: Config, cache: DiskCache):
        self.cfg = cfg
        self.cache = cache
        self.limiter = HostRateLimiter()
        self._robots: dict[str, tuple[float, RobotsRules, dict | None]] = {}
        self._robots_lock = threading.Lock()
        self._tls = threading.local()
        self.renderer = BrowserRenderer(cfg.timeout_s)

    # -- helpers ---------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            self._tls.session = s
        return s

    def check_allowed_url(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return "malformed URL"
        if parts.scheme != "https" and not (self.cfg.test_mode and parts.scheme == "http"):
            return "only https URLs are fetched"
        if parts.netloc.lower() not in self.cfg.allowed_hosts:
            return f"host '{parts.netloc}' is not a supported platform host"
        if parts.username or parts.password:
            return "credentials in URLs are not accepted"
        return None

    def _interval(self, host: str) -> float:
        rules = self._robots.get(host)
        delay = rules[1].crawl_delay if rules else None
        return max(self.cfg.min_interval_s, delay or 0.0)

    # -- robots ----------------------------------------------------------
    def robots(self, base: str, cancel: threading.Event | None = None) -> tuple[RobotsRules, dict | None]:
        host = urlsplit(base).netloc.lower()
        with self._robots_lock:
            hit = self._robots.get(host)
            if hit:
                # Network-level failures are re-checked after a minute; real
                # robots.txt answers are kept for the configured TTL.
                ttl = 60 if (hit[2] and hit[2].get("kind") in ("egress_denied", "network")) else self.cfg.cache_ttl_robots_s
                if time.time() - hit[0] < ttl:
                    return hit[1], hit[2]
        url = f"{urlsplit(base).scheme}://{host}/robots.txt"
        out = self._fetch_raw(url, kind="robots", cancel=cancel, ttl=self.cfg.cache_ttl_robots_s, check_robots=False)
        net_barrier = out.barrier if out.barrier and out.barrier["kind"] in ("egress_denied", "network") else None
        status = None if net_barrier else out.status
        rules = rules_for_status(status, out.text if out.ok else None, self.cfg.robots_token)
        with self._robots_lock:
            self._robots[host] = (time.time(), rules, net_barrier or out.barrier)
        return rules, (net_barrier or out.barrier)

    # -- public fetch ----------------------------------------------------
    def fetch(self, url: str, *, kind: str = "page", cancel: threading.Event | None = None,
              ttl: int | None = None, use_browser: bool = False) -> FetchOutcome:
        bad = self.check_allowed_url(url)
        if bad:
            return FetchOutcome(url=url, error=bad, barrier=barrier("not_allowed", bad, url))
        return self._fetch_raw(url, kind=kind, cancel=cancel, ttl=ttl, check_robots=True, use_browser=use_browser)

    def _fetch_raw(self, url: str, *, kind: str, cancel, ttl, check_robots: bool, use_browser: bool = False) -> FetchOutcome:
        ttl = self.cfg.cache_ttl_page_s if ttl is None else ttl
        cache_kind = f"{kind}:{'browser' if use_browser else 'http'}"
        cached = self.cache.get(cache_kind, url, ttl) if ttl > 0 else None
        if cached:
            return cached
        host = urlsplit(url).netloc.lower()
        if check_robots:
            rules, rb = self.robots(f"{urlsplit(url).scheme}://{host}", cancel)
            if rb and rb["kind"] in ("egress_denied", "network"):
                return FetchOutcome(url=url, barrier=dict(rb, url=url), error=rb["detail"])
            if not rules.can_fetch(url):
                why = rules.note or "path disallowed by robots.txt"
                return FetchOutcome(url=url, barrier=barrier("robots_disallow", why, url, rules.source_status))
        if use_browser:
            return self._render(url, cache_kind, cancel)

        attempts = 0
        last_error = None
        while True:
            attempts += 1
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self.limiter.wait(host, self._interval(host), cancel)
            try:
                resp, final_url, redirect_barrier = self._get_following_redirects(url, cancel)
                if redirect_barrier:
                    return FetchOutcome(url=url, attempts=attempts, final_url=final_url, barrier=redirect_barrier,
                                        error=redirect_barrier["detail"])
                limit = self.cfg.max_sitemap_bytes if kind == "sitemap" else self.cfg.max_page_bytes
                body = resp.raw.read(limit + 1, decode_content=True)
                resp.close()
            except requests.exceptions.ProxyError as exc:
                msg = str(exc)
                if "403" in msg or "407" in msg:
                    detail = (f"network egress policy of the machine running the service denied {host} "
                              f"(proxy CONNECT refused)")
                    return FetchOutcome(url=url, attempts=attempts, error=detail,
                                        barrier=barrier("egress_denied", detail, url))
                last_error = f"proxy error: {msg[:200]}"
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_error = f"network error: {type(exc).__name__}: {str(exc)[:200]}"
            else:
                if len(body) > limit:
                    return FetchOutcome(url=url, status=resp.status_code, attempts=attempts,
                                        error=f"response exceeds {limit} bytes; not processed")
                status = resp.status_code
                out = FetchOutcome(url=url, status=status, final_url=final_url, body=body,
                                   content_type=resp.headers.get("Content-Type", ""),
                                   fetched_at=utcnow_iso(), attempts=attempts)
                b = classify_response(url, status, final_url, body, out.content_type)
                if status == 429 or status in (500, 502, 503, 504):
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    if b and b["kind"] == "captcha":
                        out.barrier = b
                        return out
                    if retry_after is not None and retry_after > self.cfg.max_retry_after_s:
                        out.barrier = barrier("rate_limited", f"Retry-After {retry_after:.0f}s exceeds the "
                                              f"{self.cfg.max_retry_after_s:.0f}s limit; stopping", url, status,
                                              retry_after_s=retry_after)
                        return out
                    if attempts > self.cfg.max_retries:
                        out.barrier = b or barrier("server_error", f"HTTP {status} after {attempts} attempts", url, status)
                        if retry_after is not None:
                            out.barrier["retry_after_s"] = retry_after
                        return out
                    self._sleep_backoff(attempts, retry_after, cancel)
                    continue
                if b:
                    out.barrier = b
                    return out
                if status in (404, 410):
                    out.error = f"HTTP {status}: not found"
                    return out
                if not (200 <= status < 300):
                    out.error = f"HTTP {status}"
                    return out
                out.ok = True
                if ttl > 0:
                    self.cache.put(cache_kind, out)
                return out
            if attempts > self.cfg.max_retries:
                return FetchOutcome(url=url, attempts=attempts, error=last_error,
                                    barrier=barrier("network", last_error or "network error", url))
            self._sleep_backoff(attempts, None, cancel)

    def _get_following_redirects(self, url: str, cancel):
        """GET with manual redirects: every hop must stay on an allowed
        platform host and be permitted by that host's robots.txt."""
        current = url
        for _ in range(6):
            resp = self._session().get(current, timeout=self.cfg.timeout_s, allow_redirects=False, stream=True)
            loc = resp.headers.get("Location")
            if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                return resp, current, None
            resp.close()
            nxt = urljoin(current, loc)
            if any(h in urlsplit(nxt).path.lower() for h in LOGIN_PATH_HINTS):
                return None, nxt, barrier("login_wall", f"redirected to a login page ({nxt})", url, resp.status_code)
            bad = self.check_allowed_url(nxt)
            if bad:
                return None, nxt, barrier("offsite_redirect", f"redirect to {nxt} not followed: {bad}", url, resp.status_code)
            parts = urlsplit(nxt)
            rules, _ = self.robots(f"{parts.scheme}://{parts.netloc}", cancel)
            if not rules.can_fetch(nxt):
                return None, nxt, barrier("robots_disallow", f"redirect target {nxt} is disallowed by robots.txt", url)
            current = nxt
        return None, current, barrier("redirect_loop", "more than 5 redirects", url)

    def _sleep_backoff(self, attempts: int, retry_after: float | None, cancel) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self.cfg.backoff_cap_s, self.cfg.backoff_base_s * (2 ** (attempts - 1)))
            delay = delay * (0.75 + random.random() * 0.5)
        if cancel is not None:
            if cancel.wait(delay):
                raise Cancelled()
        else:
            time.sleep(delay)

    def _render(self, url: str, cache_kind: str, cancel) -> FetchOutcome:
        ok, why = BrowserRenderer.available()
        if not ok:
            return FetchOutcome(url=url, via="browser", error=why)
        host = urlsplit(url).netloc.lower()
        self.limiter.wait(host, self._interval(host), cancel)
        state, status, final_url, payload = self.renderer.render(url, cancel)
        if state != "ok":
            text = payload or ""
            if "ERR_TUNNEL_CONNECTION_FAILED" in text or "ERR_PROXY" in text:
                detail = f"network egress policy denied {host} (browser tunnel refused)"
                return FetchOutcome(url=url, via="browser", error=detail, barrier=barrier("egress_denied", detail, url))
            return FetchOutcome(url=url, via="browser", error=text[:300],
                                barrier=barrier("network", f"browser navigation failed: {text[:200]}", url))
        body = payload.encode("utf-8")
        out = FetchOutcome(url=url, status=status, final_url=final_url, body=body, content_type="text/html",
                           fetched_at=utcnow_iso(), via="browser", attempts=1)
        b = classify_response(url, status or 200, final_url, body, "text/html")
        if b:
            out.barrier = b
            return out
        if status and not (200 <= status < 300):
            out.error = f"HTTP {status}"
            return out
        out.ok = True
        self.cache.put(cache_kind, out)
        return out

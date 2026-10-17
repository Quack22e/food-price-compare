"""Companion HTTP service for index.html.

Run:  python -m server.app            (from the food-price-compare directory)
Then open http://127.0.0.1:8765/

Endpoints (JSON):
  GET  /api/health                      service + adapter capabilities
  POST /api/probe        {platforms}    live reachability check (robots.txt)
  POST /api/grubhub/extract {url}       Grubhub URL -> order fields
  POST /api/inspect      {url, browser} structured-data report for one page
  POST /api/jobs         {...}          start a search job
  GET  /api/jobs                        list checkpoints (resumable jobs)
  GET  /api/jobs/<id>?since=N           job summary + incremental events
  POST /api/jobs/<id>/cancel
  POST /api/jobs/<id>/resume
  DELETE /api/jobs/<id>
  POST /api/purge                       delete all checkpoints and cache
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .adapters import extract_grubhub, inspect_url
from .config import APP_ROOT, Config
from .http_client import BrowserRenderer, DiskCache, PoliteClient, utcnow_iso
from .jobs import MAX_EVENTS_PER_POLL, JobManager

VERSION = "1.0.0"
MAX_BODY = 1_000_000
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]"}
_WILDCARD_BINDS = {"", "0.0.0.0", "::", "[::]"}


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache = DiskCache(cfg.data_dir)
        self.client = PoliteClient(cfg, self.cache)
        self.jobs = JobManager(cfg, self.client)

    def health(self) -> dict:
        ok, why = BrowserRenderer.available()
        return {
            "ok": True, "version": VERSION, "time": utcnow_iso(),
            "platforms": {"doordash": "DoorDash", "ubereats": "Uber Eats"},
            "fetch_modes": {"http": True, "browser": ok, "auto": ok, "browser_note": why},
            "limits": {"min_interval_s": self.cfg.min_interval_s,
                       "concurrency_per_platform": self.cfg.concurrency_per_platform,
                       "max_stores_cap": self.cfg.max_stores_cap,
                       "max_sitemaps_per_run": self.cfg.max_sitemaps_per_run,
                       "max_retries": self.cfg.max_retries, "breaker_threshold": self.cfg.breaker_threshold},
            "user_agent": self.cfg.user_agent,
            "test_mode": self.cfg.test_mode,
        }

    def probe(self, platforms: list[str]) -> dict:
        out = {}
        for p in platforms:
            base = self.cfg.platform_bases.get(p)
            if not base:
                continue
            rules, b = self.client.robots(base)
            out[p] = {
                "base": base,
                "reachable": b is None or b.get("kind") not in ("egress_denied", "network"),
                "barrier": b,
                "robots_status": rules.source_status,
                "robots_note": rules.note,
                "crawling_allowed": not rules.disallow_all,
                "declared_sitemaps": rules.sitemaps[:20],
                "crawl_delay": rules.crawl_delay,
                "checked_at": utcnow_iso(),
            }
        return out


def make_handler(svc: Service):
    index_path = APP_ROOT / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "FoodPriceCompare/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter, no query strings
            sys.stderr.write("%s %s\n" % (self.command, urlsplit(self.path).path))

        def _origin_allowed(self, origin: str | None) -> bool:
            if origin is None:  # curl, scripts, same-origin GETs
                return True
            if origin == "null":
                return svc.cfg.allow_null_origin
            return bool(_LOCAL_ORIGIN_RE.match(origin))

        def _host_allowed(self) -> bool:
            # Defeats DNS rebinding: a page on evil.example re-pointed at
            # 127.0.0.1 still sends "Host: evil.example".
            bind = getattr(svc, "bind_host", "127.0.0.1")
            if bind in _WILDCARD_BINDS:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name in _LOOPBACK_NAMES or name == bind.lower()

        def _refuse(self) -> bool:
            """Answers 403 and returns True when this request must not run."""
            if not self._host_allowed():
                self._send(403, {"error": "Host header not allowed; use http://127.0.0.1 or http://localhost"})
                return True
            origin = self.headers.get("Origin")
            if self.command in ("POST", "DELETE") and not self._origin_allowed(origin):
                why = ("file:// pages send Origin null; open the page from this service instead, "
                       "or start it with FPC_ALLOW_NULL_ORIGIN=1") if origin == "null" else "only localhost pages may change jobs"
                self._send(403, {"error": f"Origin {origin} not allowed: {why}"})
                return True
            if self.command == "POST":
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    self._send(415, {"error": "Content-Type must be application/json"})
                    return True
            return False

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin is not None and self._origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, status: int, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self._refuse():
                return
            parts = urlsplit(self.path)
            path = parts.path
            try:
                if path in ("/", "/index.html"):
                    body = index_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/health":
                    return self._send(200, svc.health())
                if path == "/api/jobs":
                    return self._send(200, {"jobs": svc.jobs.list()})
                if path == "/api/snapshot":
                    snap = svc.cfg.snapshot_path
                    if not snap.is_file():
                        return self._send(404, {"error": "no snapshot file on this server"})
                    return self._send(200, snap.read_bytes())
                m = re.match(r"^/api/jobs/([a-f0-9]{16})$", path)
                if m:
                    job = svc.jobs.get(m.group(1))
                    if not job:
                        return self._send(404, {"error": "job not found"})
                    try:
                        since = int((parse_qs(parts.query).get("since") or ["0"])[0])
                    except ValueError:
                        since = 0
                    with job.lock:
                        events = [e for e in job.state["events"] if e["seq"] > since][:MAX_EVENTS_PER_POLL]
                    summary = job.summary()
                    return self._send(200, {"job": summary, "events": events,
                                            "next_since": events[-1]["seq"] if events else since,
                                            "more": bool(events) and events[-1]["seq"] < summary["seq"]})
                return self._send(404, {"error": "not found"})
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_DELETE(self):
            if self._refuse():
                return
            m = re.match(r"^/api/jobs/([a-f0-9]{16})$", urlsplit(self.path).path)
            if not m:
                return self._send(404, {"error": "not found"})
            ok = svc.jobs.delete(m.group(1))
            return self._send(200 if ok else 404, {"deleted": ok})

        def do_POST(self):
            if self._refuse():
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": f"invalid request body: {exc}"})
            try:
                if path == "/api/probe":
                    plats = [p for p in body.get("platforms") or ["doordash", "ubereats", "grubhub"]
                             if p in ("doordash", "ubereats", "grubhub")]
                    return self._send(200, {"probe": svc.probe(plats)})
                if path == "/api/grubhub/extract":
                    url = str(body.get("url") or "")[:2000]
                    return self._send(200, extract_grubhub(svc.client, url, svc.cfg.platform_bases["grubhub"],
                                                           svc.cfg.platform_bases.get("menupages")))
                if path == "/api/inspect":
                    url = str(body.get("url") or "")[:2000]
                    return self._send(200, inspect_url(svc.client, url, bool(body.get("browser"))))
                if path == "/api/jobs":
                    try:
                        job = svc.jobs.start(body)
                    except ValueError as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(201, {"job": job.summary()})
                m = re.match(r"^/api/jobs/([a-f0-9]{16})/(cancel|resume)$", path)
                if m:
                    job = svc.jobs.get(m.group(1))
                    if not job:
                        return self._send(404, {"error": "job not found"})
                    if m.group(2) == "cancel":
                        job.cancel()
                        return self._send(202, {"job": job.summary()})
                    try:
                        svc.jobs.resume(m.group(1), prioritize=body.get("prioritize"))
                    except ValueError as exc:
                        return self._send(409, {"error": str(exc)})
                    return self._send(202, {"job": job.summary()})
                if path == "/api/purge":
                    return self._send(200, svc.jobs.purge())
                return self._send(404, {"error": "not found"})
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(host: str, port: int, cfg: Config | None = None) -> ThreadingHTTPServer:
    svc = Service(cfg or Config.from_env())
    svc.bind_host = host
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    httpd.daemon_threads = True
    httpd.service = svc  # type: ignore[attr-defined]
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="Food Price Compare companion service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    httpd = serve(args.host, args.port)
    print(f"Food Price Compare service on http://{args.host}:{args.port}/  (data dir: {httpd.service.cfg.data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

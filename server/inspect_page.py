"""Evidence harness: report what structured data a platform page publishes.

    python -m server.inspect_page https://www.doordash.com/store/<slug>-<id>/
    python -m server.inspect_page --browser https://www.ubereats.com/store/<slug>/<id>
    python -m server.inspect_page --probe        # robots.txt reachability for all platforms

Use this wherever network access to the platforms exists to confirm (or
refute) the extraction paths the adapters rely on before trusting results.
It obeys the same robots.txt, rate-limit and barrier rules as the service.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import inspect_url
from .app import Service
from .config import Config


def save_capture(svc: Service, url: str, browser: bool, out_dir: Path) -> None:
    """Store a real page (HTML + provenance) for tests/test_real_captures.py."""
    out = svc.client.fetch(url, kind="inspect", ttl=0, use_browser=browser)
    save_capture_outcome(svc, url, out, out_dir)


def save_capture_outcome(svc: Service, url: str, out, out_dir: Path, suffix: str = "", force: bool = False) -> None:
    if not out.ok and not (force and out.body):
        print(f"not saved: {out.barrier or out.error}", file=sys.stderr)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", urlsplit(url).netloc + urlsplit(url).path).strip("_")[:120] + suffix
    (out_dir / f"{stem}.html").write_bytes(out.body or b"")
    (out_dir / f"{stem}.json").write_text(json.dumps({
        "url": url, "final_url": out.final_url, "status": out.status, "fetched_at": out.fetched_at,
        "via": out.via, "user_agent": svc.cfg.user_agent, "barrier": out.barrier}, indent=2))
    print(f"saved {out_dir / (stem + '.html')}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--browser", action="store_true", help="render with Playwright/Chromium")
    ap.add_argument("--probe", action="store_true", help="check robots.txt reachability of all platforms")
    ap.add_argument("--save", metavar="DIR", help="also save the fetched page to DIR (e.g. tests/captures) so the "
                    "test-suite verifies the extractors against it")
    args = ap.parse_args(argv)
    svc = Service(Config.from_env())
    if args.probe:
        print(json.dumps(svc.probe(["doordash", "ubereats", "grubhub"]), indent=2))
    if args.url:
        report = inspect_url(svc.client, args.url, args.browser)
        print(json.dumps(report, indent=2, default=str))
        if args.save:
            save_capture(svc, args.url, args.browser, Path(args.save))
    if not args.url and not args.probe:
        ap.print_help()


if __name__ == "__main__":
    main()

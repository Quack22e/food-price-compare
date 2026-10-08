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

from .adapters import inspect_url
from .app import Service
from .config import Config


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--browser", action="store_true", help="render with Playwright/Chromium")
    ap.add_argument("--probe", action="store_true", help="check robots.txt reachability of all platforms")
    args = ap.parse_args(argv)
    svc = Service(Config.from_env())
    if args.probe:
        print(json.dumps(svc.probe(["doordash", "ubereats", "grubhub"]), indent=2))
    if args.url:
        print(json.dumps(inspect_url(svc.client, args.url, args.browser), indent=2, default=str))
    if not args.url and not args.probe:
        ap.print_help()


if __name__ == "__main__":
    main()

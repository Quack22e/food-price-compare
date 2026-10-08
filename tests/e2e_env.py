"""Start synthetic platform servers + the real backend for the UI e2e test.

Prints one JSON line {"app": ..., "doordash": ..., ...} then serves until
killed. SYNTHETIC FIXTURES ONLY (see tests/fake_platform.py).
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.app import serve  # noqa: E402
from server.config import Config  # noqa: E402
from fake_platform import FakeSite, build_doordash, build_menupages, build_ubereats, restaurant_page  # noqa: E402


def main():
    dd, ue, gh, mp = FakeSite(), FakeSite(), FakeSite(), FakeSite()
    build_menupages(mp)
    build_doordash(dd, n_stores=6, slow=float(os.environ.get("E2E_SLOW", "0.35")))
    build_ubereats(ue)
    gh.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    gh.route("/restaurant/mcdonalds-1-main-st-chicago/555", restaurant_page(
        "McDonald's", gh.base + "/restaurant/mcdonalds-1-main-st-chicago/555", "1 Main St", "Chicago", "IL", "60601",
        [("Chicken", "Chicken McNuggets", "6.29", "USD", None, {"identifier": "menuitem-777"})]))
    cfg = Config()
    gh.route("/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510",
             "<html><head><title>Prepare your taste buds...</title></head><body><div id=app></div></body></html>")
    cfg.platform_bases = {"doordash": dd.base, "ubereats": ue.base, "grubhub": gh.base, "menupages": mp.base}
    cfg.test_mode = True
    cfg.data_dir = __import__("pathlib").Path(tempfile.mkdtemp(prefix="fpc-e2e-"))
    cfg.min_interval_s = 0.02
    cfg.concurrency_per_platform = 1
    cfg.cache_ttl_page_s = 0  # every search really fetches (cancel test needs in-flight work)
    httpd = serve("127.0.0.1", 0, cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(json.dumps({"app": f"http://127.0.0.1:{httpd.server_address[1]}", "doordash": dd.base,
                      "ubereats": ue.base, "grubhub": gh.base, "data_dir": str(cfg.data_dir)}), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

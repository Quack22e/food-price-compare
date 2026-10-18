"""Build board/index.html: the static price board.

Inputs (all in this repository):
  * board/template.html            page markup, styles and UI script
  * index.html                     the matching engine (<script id="fpc-engine">)
                                   and capture bookmarklet (<script id="fpc-capture">),
                                   copied verbatim so both pages rank identically
  * live-verification/snapshot.json and report.json, current and every
                                   committed version (git history): one entry per
                                   live run, never merged across runs, because
                                   menus change through the day
  * board/sources.json             platform status and alternative data sources

    python scripts/build_board.py [--out board/index.html]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def script_body(html: str, attrs: str) -> str:
    m = re.search(r"<script " + re.escape(attrs) + r">(.*?)</script>", html, re.S)
    if not m:
        raise SystemExit(f"index.html has no <script {attrs}>")
    return m.group(1)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _clean(obj):
    """Scraped text as it is safe to publish: U+FFFD (invalid UTF-8 in a source
    page) and lone surrogates (a string cut mid-emoji) carry no information and
    would make the page unwritable as UTF-8, so both are dropped."""
    if isinstance(obj, str):
        return _SURROGATE.sub("", obj.replace("\ufffd", ""))
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, dict):
        return {_clean(k): _clean(v) for k, v in obj.items()}
    return obj


def embed_json(obj) -> str:
    # Safe inside <script type="application/json">: no "<" can end the element.
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def fill_template(template: str, values: dict[str, str]) -> str:
    """Replace each placeholder, which must occur exactly once, in one pass over
    the template only, so text inside inserted data is never taken for one."""
    for key in values:
        if template.count(key) != 1:
            raise SystemExit(f"template must contain {key} exactly once")
    return re.sub("|".join(map(re.escape, values)), lambda m: values[m.group(0)], template)


def snapshot_from_report(report: dict) -> dict:
    """Older runs: build the same shape from report.json (menus trimmed to a sample)."""
    searches = []
    for j in report.get("jobs", []):
        order = j["request"]["order"]
        plats = j.get("summary", {}).get("platforms", {})
        searches.append({"label": f"{order['chain']} {order['items'][0]['name']}", "request": {"order": order},
                         "status": j.get("summary", {}).get("status"),
                         "coverage": {p: v["counters"] for p, v in plats.items()},
                         "barriers": j.get("barriers", []), "observations": j.get("observations", [])})
    menus = []
    mp = (report.get("grubhub") or {}).get("menupages_path")
    gm = mp and mp["result"].get("grubhub_menu")
    # live_verify keeps only a sample of the menu in report.json; a sample is
    # never offered as the menu.
    if gm and len(gm.get("items") or []) >= (gm.get("item_count") or 0):
        menus.append({"grubhub_url": mp["url"], "ok": True, "menu": gm})
    return {"generated_at": report.get("finished_at"), "github_run_id": report.get("runner", {}).get("github_run_id"),
            "searches": searches, "grubhub_menus": menus}


def board_snapshot(snap: dict) -> dict:
    """Keep only what the page uses."""
    keep_search = ("label", "request", "status", "coverage", "barriers", "observations", "seconds", "error")
    out = {k: snap.get(k) for k in ("generated_at", "finished_at", "github_run_id", "price_basis")}
    out["searches"] = []
    for s in snap.get("searches", []):
        e = {k: s.get(k) for k in keep_search if s.get(k) is not None}
        e["request"] = {"order": (s.get("request") or {}).get("order")}
        e["barriers"] = [{"kind": b.get("kind"), "detail": str(b.get("detail") or "")[:160]} for b in s.get("barriers") or [] if isinstance(b, dict)]
        out["searches"].append(e)
    out["grubhub_menus"] = [{"grubhub_url": g.get("grubhub_url"), "ok": g.get("ok"), "menu": g.get("menu"), "notes": g.get("notes"),
                             "error": g.get("error"),
                             "barriers": [{"kind": b.get("kind"), "detail": str(b.get("detail") or "")[:160]}
                                          for b in g.get("barriers") or [] if isinstance(b, dict)]}
                            for g in snap.get("grubhub_menus", [])]
    return out


def versions(rel: str) -> list[dict]:
    """The working-tree file plus every committed version of it."""
    out = []
    path = ROOT / rel
    if path.exists():
        out.append(json.loads(path.read_text(encoding="utf-8")))
    try:
        shas = subprocess.run(["git", "log", "--format=%H", "--", rel], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        shas = []
    for sha in shas:
        r = subprocess.run(["git", "show", f"{sha}:./{rel}"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            try:
                out.append(json.loads(r.stdout))
            except json.JSONDecodeError:
                pass
    return out


def collect_runs(max_runs: int) -> list[dict]:
    runs: dict[str, dict] = {}
    for snap in versions("live-verification/snapshot.json"):
        rid = str(snap.get("github_run_id") or snap.get("generated_at"))
        runs.setdefault(rid, board_snapshot(snap))
    for rep in versions("live-verification/report.json"):
        snap = snapshot_from_report(rep)
        rid = str(snap.get("github_run_id") or snap.get("generated_at"))
        runs.setdefault(rid, board_snapshot(snap))  # a run's snapshot.json wins over its report
    # A run that found no prices is kept too: its blocks and barriers are news.
    useful = [r for r in runs.values() if r["searches"] or r["grubhub_menus"]]
    useful.sort(key=lambda r: str(r.get("generated_at")), reverse=True)
    return useful[:max_runs]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "board" / "index.html"))
    ap.add_argument("--max-runs", type=int, default=6)
    ap.add_argument("--fragment", action="store_true",
                    help="write the page without <!doctype>/<head>/<body> (for hosts that add their own skeleton)")
    args = ap.parse_args()
    app = (ROOT / "index.html").read_text(encoding="utf-8")
    template = (ROOT / "board" / "template.html").read_text(encoding="utf-8")
    runs = collect_runs(args.max_runs)
    sources = json.loads((ROOT / "board" / "sources.json").read_text(encoding="utf-8"))
    values = {"@SNAPSHOT@": embed_json({"runs": runs}), "@SOURCES@": embed_json(sources),
              "@CAPTURE@": script_body(app, 'type="text/plain" id="fpc-capture"'),
              "@ENGINE@": script_body(app, 'id="fpc-engine"')}
    page = fill_template(template, values)
    if not args.fragment:
        head, sep, body = page.partition('<div class="wrap">')
        page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                + head.strip() + '\n</head>\n<body>\n' + sep + body + '\n</body>\n</html>\n')
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out}: {len(page):,} bytes")
    for r in runs:
        obs = sum(len(s.get("observations") or []) for s in r["searches"])
        print(f"  run {r.get('github_run_id')} {r.get('generated_at')}: {obs} store menus, "
              f"{sum(1 for g in r['grubhub_menus'] if g.get('ok'))} Grubhub menus")


if __name__ == "__main__":
    main()

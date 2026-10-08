"""scripts/build_board.py: embedding and template filling (review findings)."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("build_board", Path(__file__).parent.parent / "scripts" / "build_board.py")
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)


def test_embedded_data_is_writable_and_cannot_close_the_script():
    out = bb.embed_json({"name": "10 pc. Chicken McNuggets \ud83c", "x": "a�</script><b>", "\ud800k": 1})
    out.encode("utf-8")                                   # lone surrogates removed
    assert "</" not in out and "�" not in out
    assert json.loads(out) == {"name": "10 pc. Chicken McNuggets ", "x": "a</script><b>", "k": 1}


def test_placeholders_inside_data_are_left_alone():
    page = bb.fill_template("<a>@A@</a><b>@B@</b>", {"@A@": "Combo @B@ and @A@", "@B@": "ok"})
    assert page == "<a>Combo @B@ and @A@</a><b>ok</b>"
    try:
        bb.fill_template("@A@ @A@", {"@A@": "x"})
    except SystemExit as exc:
        assert "exactly once" in str(exc)
    else:
        raise AssertionError("duplicate placeholder accepted")


def test_report_menu_samples_are_not_offered_as_menus():
    rep = {"finished_at": "2026-10-08T07:09:54Z", "runner": {"github_run_id": "1"}, "jobs": [],
           "grubhub": {"menupages_path": {"url": "u", "result": {"grubhub_menu": {"items": [{"name": "a"}] * 12, "item_count": 162}}}}}
    assert bb.snapshot_from_report(rep)["grubhub_menus"] == []
    rep["grubhub"]["menupages_path"]["result"]["grubhub_menu"]["item_count"] = 12
    assert len(bb.snapshot_from_report(rep)["grubhub_menus"]) == 1


def test_runs_without_prices_keep_their_barriers():
    snap = {"generated_at": "2026-10-09T00:00:00Z", "github_run_id": "9",
            "searches": [{"label": "x", "request": {"order": {"chain": "C", "items": [{"name": "I"}]}}, "observations": [],
                          "barriers": [{"kind": "captcha", "detail": "challenge"}]}],
            "grubhub_menus": [{"grubhub_url": "u", "ok": False, "barriers": [{"kind": "captcha", "detail": "d"}]}]}
    b = bb.board_snapshot(snap)
    assert b["searches"][0]["barriers"] == [{"kind": "captcha", "detail": "challenge"}]
    assert b["grubhub_menus"][0]["barriers"] == [{"kind": "captcha", "detail": "d"}]

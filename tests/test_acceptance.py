"""The independent acceptance checks pass on a correct answer and fail on wrong ones."""
import json
from pathlib import Path

import pytest
from acceptance import check
from make_fixture import build
from reference_line3 import solve

import csv
import io


@pytest.fixture(scope="module")
def data():
    rows, expected = build()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["ts", "machine", "vibration_mm_s", "spindle_temp_c"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode(), json.loads(json.dumps(expected))


def test_fixture_is_deterministic_and_committed(data):
    committed = Path(__file__).resolve().parents[1] / "examples" / "line3" / "fixture" / "line3_export.csv"
    assert committed.read_bytes() == data[0]


def test_reference_passes(data):
    res = check(solve(data[0]), data[1], data[0])
    assert all(r["passed"] for r in res), [r for r in res if not r["passed"]]


def _mutate(files, how):
    import csv as _csv
    f = dict(files)
    if how == "alert_figures":          # verdict mutation 1
        a = json.loads(f["alerts.json"])
        a["alerts"][0]["baseline"], a["alerts"][0]["level_after"] = 12345, -6789
        f["alerts.json"] = json.dumps(a).encode()
    if how == "duplicate_and_drop":     # verdict mutation 2: 1,440 rows, 1,439 unique keys
        rows = list(_csv.DictReader(io.StringIO(f["normalized.csv"].decode())))
        rows[1] = dict(rows[0])
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        f["normalized.csv"] = buf.getvalue().encode()
    if how == "empty_code_and_plot":    # verdict mutation 3
        f["pipeline.py"] = b""
        f["test_pipeline.py"] = b""
        f["vibration.svg"] = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    return f


@pytest.mark.parametrize("how, must_fail", [
    ("alert_figures", {"alert_figures"}),
    ("duplicate_and_drop", {"normalized_unique", "normalized_keys"}),
    ("empty_code_and_plot", {"pipeline_present", "svg_plotted"}),
])
def test_verdict_mutations_fail_acceptance(data, how, must_fail):
    res = {r["id"]: r["passed"] for r in check(_mutate(solve(data[0]), how), data[1], data[0])}
    assert all(res[i] is False for i in must_fail), res


def test_first_recording_fails_the_blank_value_rule(data):
    """The 29/09 13:35 run left temperatures on 7 missing rows; the strengthened suite sees it."""
    run1 = Path(__file__).resolve().parents[1] / "runs" / "line3-run1"
    files = {p.name: p.read_bytes() for p in run1.iterdir() if p.name != "changes.diff"}
    res = {r["id"]: r for r in check(files, data[1], data[0])}
    assert res["missing_rows_blank"]["passed"] is False and "7 missing rows" in res["missing_rows_blank"]["detail"]


@pytest.mark.parametrize("mutation, failing", [
    ("shift_late", "alert_start"),
    ("spike_alert", "no_spike_alert"),
    ("first_wins", "conflicts_last_wins"),
    ("script_in_svg", "svg_inert"),
])
def test_wrong_answers_fail(data, mutation, failing):
    files = solve(data[0])
    a = json.loads(files["alerts.json"])
    if mutation == "shift_late":
        a["alerts"][0]["start_ts"] = "2026-09-14T13:20:00Z"
    if mutation == "spike_alert":
        a["alerts"].append({"machine": "M1", "signal": "vibration_mm_s", "start_ts": "2026-09-14T09:25:00Z"})
    files["alerts.json"] = json.dumps(a).encode()
    if mutation == "first_wins":
        text = files["normalized.csv"].decode()
        c = data[1]["conflicts"][0]
        text = text.replace(f"{c['ts']},{c['machine']},{c['keep']}", f"{c['ts']},{c['machine']},9.999")
        files["normalized.csv"] = text.encode()
    if mutation == "script_in_svg":
        files["vibration.svg"] = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    res = {r["id"]: r["passed"] for r in check(files, data[1], data[0])}
    assert res[failing] is False

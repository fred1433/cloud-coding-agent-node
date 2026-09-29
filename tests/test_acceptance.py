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
    res = check(solve(data[0]), data[1])
    assert all(r["passed"] for r in res), [r for r in res if not r["passed"]]


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
    res = {r["id"]: r["passed"] for r in check(files, data[1])}
    assert res[failing] is False

import csv
import json
import os
import statistics
import tempfile

import pytest

import pipeline as p


WINDOW_START = p.WINDOW_START
WINDOW_END = p.WINDOW_END
ALL_MINUTES = p.minute_range(WINDOW_START, WINDOW_END)


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "machine", "vibration_mm_s", "spindle_temp_c"])
        for r in rows:
            w.writerow(r)


def make_full_series(machine, vib_fn, minutes=ALL_MINUTES):
    """vib_fn(i, ts) -> vibration value (or '' for empty)."""
    rows = []
    for i, ts in enumerate(minutes):
        v = vib_fn(i, ts)
        rows.append([ts, machine, v, 60.0])
    return rows


# ---------------------------------------------------------------------------
# minute_range
# ---------------------------------------------------------------------------

def test_minute_range_length_and_bounds():
    mins = p.minute_range(WINDOW_START, WINDOW_END)
    assert len(mins) == p.WINDOW_MINUTES
    assert mins[0] == WINDOW_START
    assert mins[-1] == WINDOW_END


# ---------------------------------------------------------------------------
# Normalize: dedup keeps LAST row in file order
# ---------------------------------------------------------------------------

def test_dedup_keeps_last_row_in_file_order(tmp_path):
    rows = make_full_series("M1", lambda i, ts: 2.0)
    rows += make_full_series("M2", lambda i, ts: 2.0)
    rows += make_full_series("M3", lambda i, ts: 2.0)
    # duplicate the first M1 minute with a different (later, thus "last") value
    rows.append([ALL_MINUTES[0], "M1", 99.0, 61.0])

    infile = tmp_path / "in.csv"
    write_csv(infile, rows)
    all_rows = p.read_rows(str(infile))
    normalized_rows, stats = p.build_normalized(all_rows, ALL_MINUTES)

    first = [r for r in normalized_rows if r["machine"] == "M1" and r["ts"] == ALL_MINUTES[0]]
    assert len(first) == 1
    assert first[0]["vibration_mm_s"] == "99.0"
    assert first[0]["status"] == "ok"
    assert stats["M1"]["duplicates_dropped"] == 1


def test_missing_row_and_empty_vibration_are_both_missing(tmp_path):
    def vib_fn(i, ts):
        if i == 5:
            return ""  # empty vibration -> missing
        return 2.0

    rows = make_full_series("M1", vib_fn)
    # drop minute index 10 entirely -> missing (no row)
    rows = [r for i, r in enumerate(rows) if i != 10]
    rows += make_full_series("M2", lambda i, ts: 2.0)
    rows += make_full_series("M3", lambda i, ts: 2.0)

    infile = tmp_path / "in.csv"
    write_csv(infile, rows)
    all_rows = p.read_rows(str(infile))
    normalized_rows, stats = p.build_normalized(all_rows, ALL_MINUTES)

    m1 = {r["ts"]: r for r in normalized_rows if r["machine"] == "M1"}
    assert m1[ALL_MINUTES[5]]["status"] == "missing"
    assert m1[ALL_MINUTES[5]]["vibration_mm_s"] == ""
    assert m1[ALL_MINUTES[10]]["status"] == "missing"
    assert stats["M1"]["missing"] == 2
    assert stats["M1"]["rows"] == p.WINDOW_MINUTES


def test_out_of_window_rows_are_dropped_as_duplicates(tmp_path):
    rows = make_full_series("M1", lambda i, ts: 2.0)
    rows += make_full_series("M2", lambda i, ts: 2.0)
    rows += make_full_series("M3", lambda i, ts: 2.0)
    # a row far outside the window for M1
    rows.append(["2026-09-15T00:00:00Z", "M1", 5.0, 60.0])

    infile = tmp_path / "in.csv"
    write_csv(infile, rows)
    all_rows = p.read_rows(str(infile))
    normalized_rows, stats = p.build_normalized(all_rows, ALL_MINUTES)

    assert stats["M1"]["duplicates_dropped"] == 1
    assert stats["M1"]["missing"] == 0


def test_normalized_output_sorted_by_machine_then_ts(tmp_path):
    rows = make_full_series("M2", lambda i, ts: 2.0)
    rows += make_full_series("M1", lambda i, ts: 2.0)
    rows += make_full_series("M3", lambda i, ts: 2.0)

    infile = tmp_path / "in.csv"
    outfile = tmp_path / "normalized.csv"
    write_csv(infile, rows)
    all_rows = p.read_rows(str(infile))
    normalized_rows, _ = p.build_normalized(all_rows, ALL_MINUTES)
    p.write_normalized_csv(str(outfile), normalized_rows)

    with open(outfile, newline="") as f:
        out_rows = list(csv.DictReader(f))
    machines_seen = [r["machine"] for r in out_rows]
    assert machines_seen == sorted(machines_seen)
    # within a machine, ts should be non-decreasing
    m1_ts = [r["ts"] for r in out_rows if r["machine"] == "M1"]
    assert m1_ts == sorted(m1_ts)
    assert len(out_rows) == p.WINDOW_MINUTES * 3


# ---------------------------------------------------------------------------
# Detect: shift detection
# ---------------------------------------------------------------------------

def _rows_for_single_machine(machine, vibs):
    assert len(vibs) == len(ALL_MINUTES)
    return [
        {"ts": ts, "machine": machine, "vibration_mm_s": ("" if v is None else str(v)),
         "spindle_temp_c": "60.0", "status": ("missing" if v is None else "ok")}
        for ts, v in zip(ALL_MINUTES, vibs)
    ]


def test_shift_detected_when_run_long_enough_and_above_threshold():
    baseline_val = 2.0
    vibs = [baseline_val] * len(ALL_MINUTES)
    shift_start = 100
    for i in range(shift_start, shift_start + 15):
        vibs[i] = baseline_val + 1.0  # exceeds 0.8 threshold
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["machine"] == "M1"
    assert a["start_ts"] == ALL_MINUTES[shift_start]
    assert abs(a["baseline"] - baseline_val) < 1e-6
    assert abs(a["level_after"] - (baseline_val + 1.0)) < 1e-6


def test_short_run_is_not_reported():
    baseline_val = 2.0
    vibs = [baseline_val] * len(ALL_MINUTES)
    shift_start = 100
    for i in range(shift_start, shift_start + 9):  # only 9 -> below min run of 10
        vibs[i] = baseline_val + 1.0
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    assert alerts == []


def test_missing_minutes_do_not_break_a_run():
    baseline_val = 2.0
    vibs = [baseline_val] * len(ALL_MINUTES)
    shift_start = 100
    run_len = 12
    for i in range(shift_start, shift_start + run_len):
        vibs[i] = baseline_val + 1.0
    # punch a missing hole inside the run; it should not break the run
    vibs[shift_start + 5] = None
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    assert len(alerts) == 1
    assert alerts[0]["start_ts"] == ALL_MINUTES[shift_start]


def test_baseline_uses_first_60_valid_readings_median():
    # first 60 valid readings vary, rest constant; baseline should be their median.
    first60 = [1.0, 2.0, 3.0] * 20  # median 2.0
    vibs = first60 + [2.0] * (len(ALL_MINUTES) - 60)
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    # no shift expected since rest stays at baseline
    assert alerts == []


def test_at_threshold_exactly_does_not_count_as_shift():
    baseline_val = 2.0
    vibs = [baseline_val] * len(ALL_MINUTES)
    shift_start = 100
    for i in range(shift_start, shift_start + 15):
        vibs[i] = baseline_val + p.SHIFT_THRESHOLD  # exactly at threshold, not "more than"
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    assert alerts == []


def test_run_at_end_of_window_is_still_reported():
    baseline_val = 2.0
    vibs = [baseline_val] * len(ALL_MINUTES)
    n = len(ALL_MINUTES)
    start = n - 10
    for i in range(start, n):
        vibs[i] = baseline_val + 1.0
    rows = _rows_for_single_machine("M1", vibs)
    alerts = p.detect_shifts(rows)
    assert len(alerts) == 1
    assert alerts[0]["start_ts"] == ALL_MINUTES[start]


# ---------------------------------------------------------------------------
# End-to-end run() against the real input file
# ---------------------------------------------------------------------------

def test_end_to_end_run_on_real_export(tmp_path):
    src = "line3_export.csv"
    if not os.path.exists(src):
        pytest.skip("line3_export.csv not present")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        import shutil
        shutil.copy(os.path.join(cwd, src), src)
        normalized_rows, stats, alerts = p.run(src)
        assert os.path.exists("normalized.csv")
        assert os.path.exists("alerts.json")
        assert os.path.exists("vibration.svg")

        with open("alerts.json") as f:
            report = json.load(f)
        assert report["window"] == {"start": WINDOW_START, "end": WINDOW_END}
        for m in p.MACHINES:
            assert report["machines"][m]["rows"] == p.WINDOW_MINUTES

        with open("normalized.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == p.WINDOW_MINUTES * len(p.MACHINES)
        for m in p.MACHINES:
            m_rows = [r for r in rows if r["machine"] == m]
            ok_count = sum(1 for r in m_rows if r["status"] == "ok")
            missing_count = sum(1 for r in m_rows if r["status"] == "missing")
            assert ok_count + missing_count == p.WINDOW_MINUTES
            assert missing_count == report["machines"][m]["missing"]

        with open("vibration.svg") as f:
            svg = f.read()
        assert svg.strip().startswith("<svg")
        assert svg.strip().endswith("</svg>")
    finally:
        os.chdir(cwd)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

"""Pytest tests for pipeline.py.

Run with: pytest test_pipeline.py -v
"""
import csv
import io
import json
import statistics

import pytest

import pipeline as pl


def make_csv_text(rows, header="ts,machine,vibration_mm_s,spindle_temp_c"):
    lines = [header]
    for r in rows:
        lines.append(",".join(r))
    return "\n".join(lines) + "\n"


def load_csv_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


# ---------------------------------------------------------------------------
# 1. Normalize
# ---------------------------------------------------------------------------

def test_full_window_no_missing_no_duplicates():
    """A perfectly clean single-minute, single-machine slice normalizes cleanly."""
    rows = [
        {"ts": "2026-09-14T06:00:00Z", "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"},
    ]
    # Build full window for M1 only, all present, others fully missing.
    all_rows = []
    for i in range(pl.MINUTES):
        ts = pl.minute_range(pl.WINDOW_START, pl.MINUTES)[i]
        all_rows.append({"ts": ts, "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"})
    normalized, missing, dup, input_count = pl.normalize(all_rows)
    assert missing["M1"] == 0
    assert dup["M1"] == 0
    assert missing["M2"] == pl.MINUTES
    assert missing["M3"] == pl.MINUTES
    m1_rows = [r for r in normalized if r["machine"] == "M1"]
    assert len(m1_rows) == pl.MINUTES
    assert all(r["status"] == "ok" for r in m1_rows)


def test_duplicate_minute_keeps_last_in_file_order():
    """When two rows share machine+minute, the LAST one in file order wins."""
    ts = pl.minute_range(pl.WINDOW_START, pl.MINUTES)[0]
    rows = [
        {"ts": ts, "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"},
        {"ts": ts, "machine": "M1", "vibration_mm_s": "9.0", "spindle_temp_c": "99.0"},
    ]
    normalized, missing, dup, input_count = pl.normalize(rows)
    row = next(r for r in normalized if r["machine"] == "M1" and r["ts"] == ts)
    assert row["vibration_mm_s"] == "9.0"
    assert row["spindle_temp_c"] == "99.0"
    assert row["status"] == "ok"
    assert dup["M1"] == 1  # one duplicate row dropped


def test_missing_minute_has_no_row():
    """A minute entirely absent from the input is reported as missing with empty values."""
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    rows = [
        {"ts": ts, "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"}
        for ts in ts_list if ts != ts_list[5]
    ]
    normalized, missing, dup, input_count = pl.normalize(rows)
    row = next(r for r in normalized if r["machine"] == "M1" and r["ts"] == ts_list[5])
    assert row["status"] == "missing"
    assert row["vibration_mm_s"] == ""
    assert row["spindle_temp_c"] == ""
    assert missing["M1"] == 1


def test_empty_vibration_value_is_missing_and_clears_temp_too():
    """A kept row with an empty vibration value is 'missing' and BOTH columns are blanked."""
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    rows = [
        {"ts": ts, "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"}
        for ts in ts_list
    ]
    # blank out vibration for one minute, but keep temperature present
    rows[3]["vibration_mm_s"] = ""
    normalized, missing, dup, input_count = pl.normalize(rows)
    row = next(r for r in normalized if r["machine"] == "M1" and r["ts"] == ts_list[3])
    assert row["status"] == "missing"
    assert row["vibration_mm_s"] == ""
    assert row["spindle_temp_c"] == ""  # both blanked per spec
    assert missing["M1"] == 1


def test_normalized_csv_sorted_by_machine_then_ts(tmp_path):
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    rows = []
    for m in ["M3", "M1", "M2"]:  # deliberately out of order
        for ts in ts_list:
            rows.append({"ts": ts, "machine": m, "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"})
    normalized, *_ = pl.normalize(rows)
    sorted_rows = pl.write_normalized_csv(tmp_path / "normalized.csv", normalized)
    keys = [(r["machine"], r["ts"]) for r in sorted_rows]
    assert keys == sorted(keys)


def test_out_of_order_input_rows_still_normalize_correctly():
    """Rows out of chronological order in the file should still normalize fine."""
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    shuffled = list(reversed(ts_list))
    rows = [
        {"ts": ts, "machine": "M1", "vibration_mm_s": "1.0", "spindle_temp_c": "60.0"}
        for ts in shuffled
    ]
    normalized, missing, dup, input_count = pl.normalize(rows)
    assert missing["M1"] == 0
    assert dup["M1"] == 0


# ---------------------------------------------------------------------------
# 2. Detect shifts
# ---------------------------------------------------------------------------

def _build_normalized(machine, values, ts_list=None):
    """Build a list of normalized-style rows (status ok) for one machine given a list of
    vibration values (floats or None for missing)."""
    if ts_list is None:
        ts_list = pl.minute_range(pl.WINDOW_START, len(values))
    out = []
    for ts, v in zip(ts_list, values):
        if v is None:
            out.append({"ts": ts, "machine": machine, "vibration_mm_s": "", "spindle_temp_c": "", "status": "missing"})
        else:
            out.append({"ts": ts, "machine": machine, "vibration_mm_s": str(v), "spindle_temp_c": "60.0", "status": "ok"})
    return out


def _fill_other_machines(rows_by_len):
    """Return a full normalized set for MACHINES, filling absent machines with all-missing."""
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    result = []
    for m in pl.MACHINES:
        if m in rows_by_len:
            result.extend(rows_by_len[m])
        else:
            result.extend(_build_normalized(m, [None] * pl.MINUTES, ts_list))
    return result


def test_baseline_is_median_of_first_60_valid_readings():
    values = [2.0] * 60 + [10.0] * 20  # baseline should be 2.0 regardless of the spike tail
    values += [2.0] * (pl.MINUTES - len(values))
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values)})
    alerts, baselines = pl.detect_shifts(normalized)
    assert baselines["M1"] == pytest.approx(2.0)


def test_shift_requires_at_least_ten_consecutive_valid_readings():
    baseline_vals = [2.0] * 60
    # A run of only 9 elevated readings should NOT trigger an alert.
    short_spike = [3.0] * 9
    tail = [2.0] * (pl.MINUTES - len(baseline_vals) - len(short_spike))
    values = baseline_vals + short_spike + tail
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = [a for a in alerts if a["machine"] == "M1"]
    assert m1_alerts == []


def test_shift_of_exactly_ten_consecutive_readings_triggers_alert():
    baseline_vals = [2.0] * 60
    spike = [3.0] * 10  # 1.0 above baseline of 2.0, i.e. > 0.8 threshold
    tail = [2.0] * (pl.MINUTES - len(baseline_vals) - len(spike))
    values = baseline_vals + spike + tail
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values, ts_list)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = [a for a in alerts if a["machine"] == "M1"]
    assert len(m1_alerts) == 1
    assert m1_alerts[0]["start_ts"] == ts_list[60]
    assert m1_alerts[0]["baseline"] == pytest.approx(2.0)
    assert m1_alerts[0]["level_after"] == pytest.approx(3.0)


def test_missing_minutes_within_a_run_do_not_break_it():
    baseline_vals = [2.0] * 60
    # Elevated run interrupted by missing minutes; still >= 10 valid elevated readings.
    spike_with_gaps = [3.0, 3.0, 3.0, 3.0, 3.0, None, 3.0, 3.0, 3.0, 3.0, 3.0, None, 3.0]
    tail_len = pl.MINUTES - len(baseline_vals) - len(spike_with_gaps)
    values = baseline_vals + spike_with_gaps + [2.0] * tail_len
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values, ts_list)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = [a for a in alerts if a["machine"] == "M1"]
    assert len(m1_alerts) == 1
    # 11 valid elevated readings in the run (13 entries minus 2 None)
    assert m1_alerts[0]["start_ts"] == ts_list[60]


def test_shift_exactly_at_threshold_boundary_not_triggered():
    """Vibration must be MORE than baseline + 0.8; exactly baseline+0.8 does not count."""
    baseline_vals = [2.0] * 60
    at_threshold = [2.8] * 15  # exactly baseline + 0.8, not "more than"
    tail = [2.0] * (pl.MINUTES - len(baseline_vals) - len(at_threshold))
    values = baseline_vals + at_threshold + tail
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = [a for a in alerts if a["machine"] == "M1"]
    assert m1_alerts == []


def test_two_separate_shifts_each_reported_once():
    baseline_vals = [2.0] * 60
    spike1 = [3.0] * 12
    gap = [2.0] * 20
    spike2 = [3.0] * 15
    tail_len = pl.MINUTES - len(baseline_vals) - len(spike1) - len(gap) - len(spike2)
    values = baseline_vals + spike1 + gap + spike2 + [2.0] * tail_len
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values, ts_list)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = sorted((a for a in alerts if a["machine"] == "M1"), key=lambda a: a["start_ts"])
    assert len(m1_alerts) == 2
    assert m1_alerts[0]["start_ts"] == ts_list[60]
    assert m1_alerts[1]["start_ts"] == ts_list[60 + 12 + 20]


def test_level_after_is_mean_of_first_ten_readings_of_run():
    baseline_vals = [2.0] * 60
    spike = [3.0, 3.0, 3.0, 3.0, 3.0, 4.0, 4.0, 4.0, 4.0, 4.0, 3.0, 3.0]  # 12 readings
    tail = [2.0] * (pl.MINUTES - len(baseline_vals) - len(spike))
    values = baseline_vals + spike + tail
    normalized = _fill_other_machines({"M1": _build_normalized("M1", values)})
    alerts, baselines = pl.detect_shifts(normalized)
    m1_alerts = [a for a in alerts if a["machine"] == "M1"]
    assert len(m1_alerts) == 1
    expected_level_after = statistics.mean(spike[:10])
    assert m1_alerts[0]["level_after"] == pytest.approx(expected_level_after)


# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------

def test_alerts_json_structure(tmp_path):
    input_path = tmp_path / "in.csv"
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    lines = ["ts,machine,vibration_mm_s,spindle_temp_c"]
    for m in pl.MACHINES:
        for ts in ts_list:
            lines.append(f"{ts},{m},2.0,60.0")
    input_path.write_text("\n".join(lines) + "\n")
    report = pl.run(str(input_path), out_dir=str(tmp_path))
    assert report["window"] == {"start": pl.WINDOW_START, "end": pl.WINDOW_END}
    for m in pl.MACHINES:
        assert report["machines"][m]["rows"] == pl.MINUTES
        assert report["machines"][m]["missing"] == 0
        assert report["machines"][m]["duplicates_dropped"] == 0
    assert report["alerts"] == []
    assert (tmp_path / "normalized.csv").exists()
    assert (tmp_path / "alerts.json").exists()
    assert (tmp_path / "vibration.svg").exists()


def test_alerts_json_is_valid_json_roundtrip(tmp_path):
    input_path = tmp_path / "in.csv"
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    lines = ["ts,machine,vibration_mm_s,spindle_temp_c"]
    for m in pl.MACHINES:
        for i, ts in enumerate(ts_list):
            v = 2.0 if i < 60 else 5.0  # baseline 2.0, then a long shift
            lines.append(f"{ts},{m},{v},60.0")
    input_path.write_text("\n".join(lines) + "\n")
    pl.run(str(input_path), out_dir=str(tmp_path))
    with open(tmp_path / "alerts.json") as f:
        data = json.load(f)
    assert len(data["alerts"]) == 3  # one per machine
    for a in data["alerts"]:
        assert a["signal"] == "vibration_mm_s"
        assert a["baseline"] == pytest.approx(2.0)
        assert a["level_after"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 4. Plot
# ---------------------------------------------------------------------------

def test_svg_output_is_well_formed_xml(tmp_path):
    import xml.etree.ElementTree as ET

    input_path = tmp_path / "in.csv"
    ts_list = pl.minute_range(pl.WINDOW_START, pl.MINUTES)
    lines = ["ts,machine,vibration_mm_s,spindle_temp_c"]
    for m in pl.MACHINES:
        for ts in ts_list:
            lines.append(f"{ts},{m},2.0,60.0")
    input_path.write_text("\n".join(lines) + "\n")
    pl.run(str(input_path), out_dir=str(tmp_path))
    tree = ET.parse(tmp_path / "vibration.svg")
    assert tree.getroot().tag.endswith("svg")


# ---------------------------------------------------------------------------
# End-to-end sanity check against the real export file (if present)
# ---------------------------------------------------------------------------

def test_end_to_end_on_real_export(tmp_path):
    import os

    src = "line3_export.csv"
    if not os.path.exists(src):
        pytest.skip("real export file not present")
    report = pl.run(src, out_dir=str(tmp_path))
    assert report["window"] == {"start": pl.WINDOW_START, "end": pl.WINDOW_END}
    for m in pl.MACHINES:
        assert report["machines"][m]["rows"] == pl.MINUTES
        assert report["machines"][m]["missing"] >= 0
        assert report["machines"][m]["duplicates_dropped"] >= 0
    # We know from manual inspection that there is exactly one alert, on M2.
    assert len(report["alerts"]) == 1
    assert report["alerts"][0]["machine"] == "M2"

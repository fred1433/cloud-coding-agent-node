#!/usr/bin/env python3
"""Pipeline: normalize, detect shifts, report, and plot CNC spindle vibration data.

Usage:
    python pipeline.py line3_export.csv
"""
import sys
import csv
import json
import statistics
from datetime import datetime, timedelta, timezone

WINDOW_START = "2026-09-14T06:00:00Z"
WINDOW_END = "2026-09-14T13:59:00Z"
MACHINES = ["M1", "M2", "M3"]
MINUTES = 480

BASELINE_N = 60
SHIFT_MIN_RUN = 10
SHIFT_THRESHOLD = 0.8


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_range(start_ts, n):
    start = parse_ts(start_ts)
    return [fmt_ts(start + timedelta(minutes=i)) for i in range(n)]


def read_rows(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def normalize(rows):
    """Return dict: machine -> {ts: row}, keeping last row per (machine, ts) in file order.
    Also returns duplicates_dropped per machine (rows not kept, i.e. input rows for a
    machine minus number of distinct minutes kept that came from this machine, restricted
    to minutes actually within the window; rows outside the window entirely are also
    considered dropped-as-duplicate-input rows per the spec's intent of accounting for all
    input rows for that machine that were not kept)."""
    all_minutes = set(minute_range(WINDOW_START, MINUTES))

    kept = {m: {} for m in MACHINES}  # machine -> ts -> row (last wins)
    input_count = {m: 0 for m in MACHINES}

    for row in rows:
        m = row.get("machine")
        if m not in kept:
            # Unknown machine; ignore entirely (not counted anywhere per spec scope).
            continue
        input_count[m] += 1
        ts = row["ts"]
        kept[m][ts] = row  # last one wins naturally due to file order

    duplicates_dropped = {}
    normalized_rows = []
    missing_count = {}

    for m in MACHINES:
        machine_kept = kept[m]
        # Count rows actually used: one per timestamp that's within our window and kept.
        used_rows = 0
        missing = 0
        for ts in minute_range(WINDOW_START, MINUTES):
            row = machine_kept.get(ts)
            if row is not None:
                used_rows += 1
                vib = row.get("vibration_mm_s", "")
                temp = row.get("spindle_temp_c", "")
                if vib is None or vib == "":
                    status = "missing"
                    vib_out = ""
                    temp_out = ""
                    missing += 1
                else:
                    status = "ok"
                    vib_out = vib
                    temp_out = temp
            else:
                status = "missing"
                vib_out = ""
                temp_out = ""
                missing += 1
            normalized_rows.append({
                "ts": ts,
                "machine": m,
                "vibration_mm_s": vib_out,
                "spindle_temp_c": temp_out,
                "status": status,
            })
        # duplicates_dropped: input rows for this machine that were not kept.
        # "kept" = one row per minute in window that has data present in machine_kept
        # restricted to window minutes (used_rows). Any input row not part of that final
        # kept set counts as dropped (earlier duplicates for same minute, or rows outside
        # window, or... all accounted for by input_count - used_rows... but used_rows only
        # counts window minutes with a row; rows for timestamps outside the window that
        # exist in machine_kept dict are extra input rows not used at all).
        # Rows outside window entirely:
        outside_window_rows = 0
        for ts, row in machine_kept.items():
            if ts not in all_minutes:
                outside_window_rows += 1
        # Rows that were duplicates for a timestamp within the window: for each ts in window
        # count how many raw input rows had that ts for this machine, minus 1 (the kept one),
        # summed. We need raw counts per ts.
        pass
        duplicates_dropped[m] = None  # placeholder; computed below with raw counts
        missing_count[m] = missing

    # Compute duplicates_dropped precisely using raw per-(machine,ts) counts.
    raw_ts_counts = {m: {} for m in MACHINES}
    raw_outside = {m: 0 for m in MACHINES}
    for row in rows:
        m = row.get("machine")
        if m not in raw_ts_counts:
            continue
        ts = row["ts"]
        raw_ts_counts[m][ts] = raw_ts_counts[m].get(ts, 0) + 1

    for m in MACHINES:
        dropped = 0
        for ts, cnt in raw_ts_counts[m].items():
            if ts in all_minutes:
                # one is kept, rest are dropped duplicates
                dropped += (cnt - 1)
            else:
                # entirely outside window: all such rows are dropped (not kept at all)
                dropped += cnt
        duplicates_dropped[m] = dropped

    return normalized_rows, missing_count, duplicates_dropped, input_count


def write_normalized_csv(path, normalized_rows):
    normalized_rows_sorted = sorted(normalized_rows, key=lambda r: (r["machine"], r["ts"]))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["ts", "machine", "vibration_mm_s", "spindle_temp_c", "status"]
        )
        writer.writeheader()
        for row in normalized_rows_sorted:
            writer.writerow(row)
    return normalized_rows_sorted


def detect_shifts(normalized_rows_sorted):
    """For each machine, compute baseline (median of first 60 valid readings) and detect
    shift runs: consecutive valid readings (skipping missing) with vibration >
    baseline + 0.8, run length >= 10. Report each run once with start_ts = ts of first
    reading in the run, baseline, and level_after = mean of first 10 readings of the run."""
    by_machine = {m: [] for m in MACHINES}
    for row in normalized_rows_sorted:
        by_machine[row["machine"]].append(row)

    alerts = []
    baselines = {}

    for m in MACHINES:
        rows = by_machine[m]  # already sorted by ts
        valid = [(r["ts"], float(r["vibration_mm_s"])) for r in rows if r["status"] == "ok"]
        if len(valid) < BASELINE_N:
            baseline = statistics.median([v for _, v in valid]) if valid else None
        else:
            baseline = statistics.median([v for _, v in valid[:BASELINE_N]])
        baselines[m] = baseline

        if baseline is None:
            continue

        threshold = baseline + SHIFT_THRESHOLD

        # Walk through valid readings (in ts order) to find runs of values > threshold.
        run = []
        runs = []  # list of list of (ts, val)
        for ts, val in valid:
            if val > threshold:
                run.append((ts, val))
            else:
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)

        for run in runs:
            if len(run) >= SHIFT_MIN_RUN:
                start_ts = run[0][0]
                level_after = statistics.mean([v for _, v in run[:10]])
                alerts.append({
                    "machine": m,
                    "signal": "vibration_mm_s",
                    "start_ts": start_ts,
                    "baseline": round(baseline, 6),
                    "level_after": round(level_after, 6),
                })

    return alerts, baselines


def write_alerts_json(path, missing_count, duplicates_dropped, alerts):
    machines_report = {}
    for m in MACHINES:
        machines_report[m] = {
            "rows": MINUTES,
            "missing": missing_count[m],
            "duplicates_dropped": duplicates_dropped[m],
        }

    report = {
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "machines": machines_report,
        "alerts": alerts,
    }
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    return report


def write_svg_plot(path, normalized_rows_sorted, alerts):
    """Write a simple SVG line plot of vibration over time for the three machines,
    marking alert start times."""
    by_machine = {m: [] for m in MACHINES}
    for row in normalized_rows_sorted:
        by_machine[row["machine"]].append(row)

    width, height = 1200, 500
    margin_left, margin_right, margin_top, margin_bottom = 60, 20, 40, 60
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    all_vals = []
    for m in MACHINES:
        for r in by_machine[m]:
            if r["status"] == "ok":
                all_vals.append(float(r["vibration_mm_s"]))
    if all_vals:
        vmin, vmax = min(all_vals), max(all_vals)
    else:
        vmin, vmax = 0.0, 1.0
    if vmax - vmin < 1e-9:
        vmax = vmin + 1.0
    pad = (vmax - vmin) * 0.05
    vmin -= pad
    vmax += pad

    n = MINUTES

    def x_for_index(i):
        return margin_left + (plot_w * i / (n - 1))

    def y_for_val(v):
        return margin_top + plot_h * (1 - (v - vmin) / (vmax - vmin))

    colors = {"M1": "#1f77b4", "M2": "#ff7f0e", "M3": "#2ca02c"}

    svg_parts = []
    svg_parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">'
    )
    svg_parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>')

    # Axes
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{margin_top + plot_h}" stroke="black" stroke-width="1"/>'
    )
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{margin_top + plot_h}" '
        f'x2="{margin_left + plot_w}" y2="{margin_top + plot_h}" stroke="black" stroke-width="1"/>'
    )

    # Y axis ticks (5 ticks)
    for k in range(6):
        v = vmin + (vmax - vmin) * k / 5
        y = y_for_val(v)
        svg_parts.append(
            f'<line x1="{margin_left - 4}" y1="{y:.1f}" x2="{margin_left}" '
            f'y2="{y:.1f}" stroke="black"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 8}" y="{y + 4:.1f}" text-anchor="end">{v:.2f}</text>'
        )

    # X axis ticks: every 60 minutes
    ts_list = minute_range(WINDOW_START, MINUTES)
    for i in range(0, n, 60):
        x = x_for_index(i)
        svg_parts.append(
            f'<line x1="{x:.1f}" y1="{margin_top + plot_h}" x2="{x:.1f}" '
            f'y2="{margin_top + plot_h + 4}" stroke="black"/>'
        )
        label = ts_list[i][11:16]
        svg_parts.append(
            f'<text x="{x:.1f}" y="{margin_top + plot_h + 18}" text-anchor="middle">{label}</text>'
        )

    # Title
    svg_parts.append(
        f'<text x="{width/2}" y="20" text-anchor="middle" font-size="16">'
        f'Spindle Vibration (mm/s) - {WINDOW_START} to {WINDOW_END}</text>'
    )

    # Legend
    lx = margin_left + 10
    ly = margin_top - 15
    for i, m in enumerate(MACHINES):
        cx = lx + i * 100
        svg_parts.append(
            f'<line x1="{cx}" y1="{ly}" x2="{cx+20}" y2="{ly}" stroke="{colors[m]}" stroke-width="2"/>'
        )
        svg_parts.append(f'<text x="{cx+24}" y="{ly+4}">{m}</text>')

    # Data lines per machine (skip gaps at missing readings)
    ts_index = {ts: i for i, ts in enumerate(ts_list)}
    for m in MACHINES:
        points = []
        for r in by_machine[m]:
            if r["status"] == "ok":
                i = ts_index[r["ts"]]
                x = x_for_index(i)
                y = y_for_val(float(r["vibration_mm_s"]))
                points.append((x, y))
        # Build path, breaking at gaps (non-consecutive index) - simplest: just connect
        # all valid points in order; small gaps are fine since missing values are dropped.
        if points:
            d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            svg_parts.append(
                f'<path d="{d}" fill="none" stroke="{colors[m]}" stroke-width="1.5"/>'
            )

    # Alert markers
    for alert in alerts:
        m = alert["machine"]
        i = ts_index[alert["start_ts"]]
        x = x_for_index(i)
        svg_parts.append(
            f'<line x1="{x:.1f}" y1="{margin_top}" x2="{x:.1f}" '
            f'y2="{margin_top + plot_h}" stroke="{colors[m]}" stroke-width="1" '
            f'stroke-dasharray="4,3"/>'
        )
        svg_parts.append(
            f'<circle cx="{x:.1f}" cy="{margin_top - 2}" r="4" fill="{colors[m]}"/>'
        )

    svg_parts.append("</svg>")

    with open(path, "w") as f:
        f.write("\n".join(svg_parts))


def run(input_path, out_dir="."):
    rows = read_rows(input_path)
    normalized_rows, missing_count, duplicates_dropped, input_count = normalize(rows)
    normalized_rows_sorted = write_normalized_csv(f"{out_dir}/normalized.csv", normalized_rows)
    alerts, baselines = detect_shifts(normalized_rows_sorted)
    report = write_alerts_json(f"{out_dir}/alerts.json", missing_count, duplicates_dropped, alerts)
    write_svg_plot(f"{out_dir}/vibration.svg", normalized_rows_sorted, alerts)
    return report


def main():
    if len(sys.argv) != 2:
        print("Usage: python pipeline.py <input.csv>", file=sys.stderr)
        sys.exit(1)
    report = run(sys.argv[1])
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

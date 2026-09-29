#!/usr/bin/env python3
"""Pipeline: normalize noisy CNC spindle export, detect vibration shifts, report and plot."""
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone

WINDOW_START = "2026-09-14T06:00:00Z"
WINDOW_END = "2026-09-14T13:59:00Z"
MACHINES = ["M1", "M2", "M3"]
WINDOW_MINUTES = 480

BASELINE_N = 60
SHIFT_MIN_RUN = 10
SHIFT_THRESHOLD = 0.8


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_range(start_ts, end_ts):
    start = parse_ts(start_ts)
    end = parse_ts(end_ts)
    out = []
    t = start
    while t <= end:
        out.append(fmt_ts(t))
        t += timedelta(minutes=1)
    return out


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def normalize(rows):
    """Return dict: machine -> {ts: row}, kept via last-in-file-order, and duplicates_dropped counts."""
    kept = {m: {} for m in MACHINES}
    seen_order = {m: {} for m in MACHINES}  # ts -> count of rows seen (for dup counting)
    total_seen = {m: 0 for m in MACHINES}

    for r in rows:
        m = r["machine"]
        if m not in kept:
            continue
        ts = r["ts"]
        total_seen[m] += 1
        # last one wins
        kept[m][ts] = r

    return kept, total_seen


def build_normalized(rows, all_minutes):
    kept, total_seen = normalize(rows)

    normalized_rows = []
    machine_stats = {}

    for m in MACHINES:
        machine_rows = kept[m]
        rows_in_window = 0
        missing = 0
        for ts in all_minutes:
            row = machine_rows.get(ts)
            if row is None:
                status = "missing"
                vib = ""
                temp = ""
                missing += 1
            else:
                vib = row["vibration_mm_s"]
                temp = row["spindle_temp_c"]
                if vib == "" or vib is None:
                    status = "missing"
                    missing += 1
                else:
                    status = "ok"
            normalized_rows.append({
                "ts": ts,
                "machine": m,
                "vibration_mm_s": vib,
                "spindle_temp_c": temp,
                "status": status,
            })
            rows_in_window += 1

        # duplicates_dropped: input rows for that machine that were not kept
        # kept count = number of distinct ts actually used as the final row (i.e. len(machine_rows)
        # but only those ts within window count as "kept"; rows outside window are also dropped)
        # "kept" here means: the row object that ended up being used for a ts. Rows sharing the same
        # ts that were overwritten, plus rows with ts outside the window entirely, are duplicates_dropped.
        kept_ts_in_window = set(all_minutes) & set(machine_rows.keys())
        # count of raw rows for this machine that are the ones actually retained
        # For each ts kept, only 1 row of possibly many is retained -> the rest are "dropped"
        # We need per-ts counts of how many raw rows existed for that machine.
        dup_dropped = 0
        # recompute per ts counts
        ts_counts = {}
        for r in rows:
            if r["machine"] != m:
                continue
            ts_counts[r["ts"]] = ts_counts.get(r["ts"], 0) + 1
        kept_count = 0
        for ts, cnt in ts_counts.items():
            if ts in kept_ts_in_window:
                dup_dropped += (cnt - 1)
                kept_count += 1
            else:
                # entirely out-of-window ts: all its rows are dropped
                dup_dropped += cnt

        machine_stats[m] = {
            "rows": WINDOW_MINUTES,
            "missing": missing,
            "duplicates_dropped": dup_dropped,
        }

    return normalized_rows, machine_stats


def write_normalized_csv(path, normalized_rows):
    normalized_rows_sorted = sorted(normalized_rows, key=lambda r: (r["machine"], r["ts"]))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "machine", "vibration_mm_s", "spindle_temp_c", "status"])
        for r in normalized_rows_sorted:
            w.writerow([r["ts"], r["machine"], r["vibration_mm_s"], r["spindle_temp_c"], r["status"]])


def detect_shifts(normalized_rows):
    """Detect shift alerts per machine. Returns list of alerts and per-machine baselines."""
    by_machine = {m: [] for m in MACHINES}
    for r in normalized_rows:
        by_machine[r["machine"]].append(r)

    alerts = []
    for m in MACHINES:
        series = sorted(by_machine[m], key=lambda r: r["ts"])
        valid = [(r["ts"], float(r["vibration_mm_s"])) for r in series if r["status"] == "ok"]
        if len(valid) < BASELINE_N:
            baseline_vals = [v for _, v in valid]
        else:
            baseline_vals = [v for _, v in valid[:BASELINE_N]]
        if not baseline_vals:
            continue
        baseline = statistics.median(baseline_vals)

        # walk valid readings (in ts order), track consecutive runs above threshold
        run = []  # list of (ts, vib)
        i = 0
        n = len(valid)
        reported_starts = set()
        while i < n:
            ts, vib = valid[i]
            if vib > baseline + SHIFT_THRESHOLD:
                run.append((ts, vib))
            else:
                if len(run) >= SHIFT_MIN_RUN:
                    start_ts = run[0][0]
                    level_after = statistics.mean(v for _, v in run[:SHIFT_MIN_RUN])
                    alerts.append({
                        "machine": m,
                        "signal": "vibration_mm_s",
                        "start_ts": start_ts,
                        "baseline": round(baseline, 6),
                        "level_after": round(level_after, 6),
                    })
                run = []
            i += 1
        # flush trailing run
        if len(run) >= SHIFT_MIN_RUN:
            start_ts = run[0][0]
            level_after = statistics.mean(v for _, v in run[:SHIFT_MIN_RUN])
            alerts.append({
                "machine": m,
                "signal": "vibration_mm_s",
                "start_ts": start_ts,
                "baseline": round(baseline, 6),
                "level_after": round(level_after, 6),
            })

    return alerts


def write_alerts_json(path, machine_stats, alerts):
    out = {
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "machines": machine_stats,
        "alerts": alerts,
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2)


def write_svg(path, normalized_rows, alerts):
    W, H = 1200, 500
    margin_l, margin_r, margin_t, margin_b = 60, 20, 30, 40
    plot_w = W - margin_l - margin_r
    plot_h = H - margin_t - margin_b

    by_machine = {m: [] for m in MACHINES}
    for r in normalized_rows:
        by_machine[r["machine"]].append(r)
    for m in MACHINES:
        by_machine[m].sort(key=lambda r: r["ts"])

    all_minutes = minute_range(WINDOW_START, WINDOW_END)
    ts_index = {ts: i for i, ts in enumerate(all_minutes)}
    n = len(all_minutes)

    all_vibs = [float(r["vibration_mm_s"]) for r in normalized_rows if r["status"] == "ok"]
    vmin, vmax = min(all_vibs), max(all_vibs)
    vpad = (vmax - vmin) * 0.05 or 0.1
    vmin -= vpad
    vmax += vpad

    def x_for(i):
        return margin_l + (i / (n - 1)) * plot_w

    def y_for(v):
        return margin_t + (1 - (v - vmin) / (vmax - vmin)) * plot_h

    colors = {"M1": "#1f77b4", "M2": "#ff7f0e", "M3": "#2ca02c"}

    parts = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="white"/>')
    parts.append(f'<text x="{W/2}" y="18" font-size="16" text-anchor="middle" font-family="sans-serif">Vibration (mm/s) over time — line3</text>')

    # axes
    parts.append(f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{H-margin_b}" stroke="black"/>')
    parts.append(f'<line x1="{margin_l}" y1="{H-margin_b}" x2="{W-margin_r}" y2="{H-margin_b}" stroke="black"/>')

    # y ticks
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = y_for(v)
        parts.append(f'<line x1="{margin_l-4}" y1="{y:.1f}" x2="{margin_l}" y2="{y:.1f}" stroke="black"/>')
        parts.append(f'<text x="{margin_l-8}" y="{y+4:.1f}" font-size="10" text-anchor="end" font-family="sans-serif">{v:.2f}</text>')

    # x ticks: every 60 minutes
    for i in range(0, n, 60):
        x = x_for(i)
        parts.append(f'<line x1="{x:.1f}" y1="{H-margin_b}" x2="{x:.1f}" y2="{H-margin_b+4}" stroke="black"/>')
        label = all_minutes[i][11:16]
        parts.append(f'<text x="{x:.1f}" y="{H-margin_b+16}" font-size="9" text-anchor="middle" font-family="sans-serif">{label}</text>')

    # series lines
    for m in MACHINES:
        pts = []
        for r in by_machine[m]:
            if r["status"] != "ok":
                continue
            i = ts_index[r["ts"]]
            x = x_for(i)
            y = y_for(float(r["vibration_mm_s"]))
            pts.append(f"{x:.1f},{y:.1f}")
        if pts:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{colors[m]}" stroke-width="1.2"/>')

    # legend
    lx, ly = W - 150, margin_t
    for i, m in enumerate(MACHINES):
        yy = ly + i * 16
        parts.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+20}" y2="{yy}" stroke="{colors[m]}" stroke-width="2"/>')
        parts.append(f'<text x="{lx+24}" y="{yy+4}" font-size="11" font-family="sans-serif">{m}</text>')

    # alert markers
    for a in alerts:
        i = ts_index.get(a["start_ts"])
        if i is None:
            continue
        x = x_for(i)
        parts.append(f'<line x1="{x:.1f}" y1="{margin_t}" x2="{x:.1f}" y2="{H-margin_b}" stroke="red" stroke-dasharray="4,3" stroke-width="1"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{margin_t+4}" r="3" fill="red"/>')

    parts.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(parts))


def run(input_path, out_dir="."):
    rows = read_rows(input_path)
    all_minutes = minute_range(WINDOW_START, WINDOW_END)
    normalized_rows, machine_stats = build_normalized(rows, all_minutes)
    write_normalized_csv(f"{out_dir}/normalized.csv", normalized_rows)
    alerts = detect_shifts(normalized_rows)
    write_alerts_json(f"{out_dir}/alerts.json", machine_stats, alerts)
    write_svg(f"{out_dir}/vibration.svg", normalized_rows, alerts)
    return normalized_rows, machine_stats, alerts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python pipeline.py <input.csv>", file=sys.stderr)
        sys.exit(1)
    _, stats, alerts = run(sys.argv[1])
    print(json.dumps({"machines": stats, "alerts": alerts}, indent=2))

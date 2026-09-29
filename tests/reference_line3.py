"""A reference implementation of the line 3 contract. It exists only to prove that
the acceptance checks can pass (and fail when an output is wrong). The agent never
sees it."""
import csv
import datetime as dt
import io
import json
import statistics


def solve(csv_bytes):
    start = dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.timezone.utc)
    grid = [(start + dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(480)]
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode())))
    last, count = {}, {}
    for r in rows:
        last[(r["machine"], r["ts"])] = r
        count[r["machine"]] = count.get(r["machine"], 0) + 1
    machines = sorted(count)
    norm, report, alerts = [], {}, []
    for m in machines:
        valid = []
        missing = 0
        for t in grid:
            r = last.get((m, t))
            if r is None or r["vibration_mm_s"] == "":
                missing += 1
                norm.append({"ts": t, "machine": m, "vibration_mm_s": "", "spindle_temp_c": "", "status": "missing"})
            else:
                norm.append({**r, "status": "ok"})
                valid.append((t, float(r["vibration_mm_s"])))
        kept = sum(1 for (mm, _) in last if mm == m)
        report[m] = {"rows": 480, "missing": missing, "duplicates_dropped": count[m] - kept}
        base = statistics.median(v for _, v in valid[:60])
        run = []
        for t, v in valid + [(None, float("-inf"))]:
            if v > base + 0.8:
                run.append((t, v))
                continue
            if len(run) >= 10:
                alerts.append({"machine": m, "signal": "vibration_mm_s", "start_ts": run[0][0], "baseline": base,
                               "level_after": statistics.mean(x for _, x in run[:10])})
            run = []
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["ts", "machine", "vibration_mm_s", "spindle_temp_c", "status"])
    w.writeheader()
    w.writerows(norm)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M0 0L10 10"/></svg>'
    return {
        "alerts.json": json.dumps({"window": {"start": grid[0], "end": grid[-1]}, "machines": report,
                                   "alerts": alerts}).encode(),
        "normalized.csv": out.getvalue().encode(),
        "vibration.svg": svg,
        "pipeline.py": b"# reference",
        "test_pipeline.py": b"# reference",
    }

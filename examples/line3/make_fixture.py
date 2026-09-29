"""Synthetic machine export for the demo task, with its defects injected on purpose.

Three CNC spindles on one line, one reading per minute from 06:00 to 13:59 UTC.
Injected defects, all recorded in expected.json so the acceptance checks know the
right answer without trusting anything the agent wrote:
- exact duplicate rows, and duplicate timestamps with conflicting values;
- rows out of order;
- missing minutes and empty vibration cells;
- a two-minute spike on M1 that is NOT a sustained shift;
- a sustained vibration shift on M2 starting at a known minute.
This file is synthetic. No real machine data is involved.
"""
import csv
import datetime as dt
import json
import random
import sys
from pathlib import Path

START = dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.timezone.utc)
MINUTES = 480
MACHINES = {"M1": (2.10, 61.0), "M2": (1.85, 58.5), "M3": (2.40, 63.2)}   # vibration mm/s, temp C
SHIFT = {"machine": "M2", "minute": 431, "delta": 1.6}                    # 13:11 UTC
SPIKE = {"machine": "M1", "minutes": [205, 206], "delta": 5.0}
M3_GAP = range(300, 320)


def ts(minute):
    return (START + dt.timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(seed=20260914):
    rnd = random.Random(seed)
    rows = []
    for m, (vib, temp) in MACHINES.items():
        for i in range(MINUTES):
            if m == "M3" and i in M3_GAP:
                continue
            v = rnd.gauss(vib, 0.12)
            if m == SHIFT["machine"] and i >= SHIFT["minute"]:
                v += SHIFT["delta"]
            if m == SPIKE["machine"] and i in SPIKE["minutes"]:
                v += SPIKE["delta"]
            t = rnd.gauss(temp, 0.4) + (0.004 * i)
            rows.append({"ts": ts(i), "machine": m, "vibration_mm_s": f"{v:.3f}", "spindle_temp_c": f"{t:.2f}"})

    # scattered missing minutes on M1 and M2
    missing = {}
    for m in ("M1", "M2"):
        drop = set(rnd.sample([i for i in range(60, MINUTES) if i not in SPIKE["minutes"]
                               and abs(i - SHIFT["minute"]) > 12], 9))
        rows = [r for r in rows if not (r["machine"] == m and int(_minute(r["ts"])) in drop)]
        missing[m] = sorted(drop)
    missing["M3"] = list(M3_GAP)

    # empty vibration cells (row present, value missing)
    empty = []
    for r in rnd.sample([r for r in rows if _minute(r["ts"]) > 70 and abs(_minute(r["ts"]) - SHIFT["minute"]) > 12
                         and not (r["machine"] == "M1" and _minute(r["ts"]) in SPIKE["minutes"])], 7):
        r["vibration_mm_s"] = ""
        empty.append((r["machine"], r["ts"]))

    # conflicting duplicates: an earlier, wrong reading for the same minute appears first in the file
    conflicts = []
    for r in rnd.sample([r for r in rows if r["vibration_mm_s"] and abs(_minute(r["ts"]) - SHIFT["minute"]) > 12
                         and not (r["machine"] == "M1" and _minute(r["ts"]) in SPIKE["minutes"])], 6):
        wrong = dict(r, vibration_mm_s=f"{float(r['vibration_mm_s']) + 0.9:.3f}")
        rows.insert(rows.index(r), wrong)
        conflicts.append({"machine": r["machine"], "ts": r["ts"], "keep": r["vibration_mm_s"]})

    # exact duplicates
    exact = rnd.sample(rows, 25)
    for r in exact:
        rows.insert(rnd.randrange(len(rows)), dict(r))

    # a few rows out of order
    for _ in range(12):
        i, j = rnd.randrange(len(rows)), rnd.randrange(len(rows))
        rows[i], rows[j] = rows[j], rows[i]

    # expected values are computed from the final file, the way the contract defines them
    last = {}
    count = {m: 0 for m in MACHINES}
    for r in rows:                      # file order: the last row for a (machine, minute) wins
        last[(r["machine"], r["ts"])] = r
        count[r["machine"]] += 1
    dups = {m: count[m] - sum(1 for (mm, _) in last if mm == m) for m in MACHINES}
    missing_counts = {m: sum(1 for i in range(MINUTES) if (m, ts(i)) not in last
                             or last[(m, ts(i))]["vibration_mm_s"] == "") for m in MACHINES}
    conflicts = [dict(c, keep=last[(c["machine"], c["ts"])]["vibration_mm_s"]) for c in conflicts]
    expected = {
        "window": {"start": ts(0), "end": ts(MINUTES - 1), "rows_per_machine": MINUTES},
        "machines": {m: {"rows": MINUTES, "missing": missing_counts[m], "duplicates_dropped": dups[m]}
                     for m in MACHINES},
        "alerts": [{"machine": SHIFT["machine"], "signal": "vibration_mm_s", "start_ts": ts(SHIFT["minute"])}],
        "conflicts": conflicts,
        "not_alerts": [{"machine": SPIKE["machine"], "ts": ts(SPIKE["minutes"][0]), "why": "two-minute spike"}],
    }
    return rows, expected


def _minute(t):
    d = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return int((d - START).total_seconds() // 60)


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows, expected = build()
    with open(out / "line3_export.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "machine", "vibration_mm_s", "spindle_temp_c"])
        w.writeheader()
        w.writerows(rows)
    (out / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(f"{len(rows)} rows written to {out / 'line3_export.csv'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "fixture")

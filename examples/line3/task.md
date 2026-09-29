You receive `line3_export.csv`, a raw export from three CNC spindles (M1, M2, M3) on one production line.
Columns: `ts` (UTC, ISO 8601, one reading per minute), `machine`, `vibration_mm_s`, `spindle_temp_c`.
The export window is 2026-09-14T06:00:00Z to 2026-09-14T13:59:00Z inclusive (480 minutes per machine).
The export is messy: rows can be out of order, repeated, missing, or have an empty vibration value.

Write a small pipeline, in Python with the standard library only, that implements this contract:

1. Normalize. For each machine, keep exactly one row per minute of the window. If several rows share the same
   machine and minute, keep the LAST one in file order. A minute with no row, or whose kept row has an empty
   vibration value, is `missing`. Write `normalized.csv` with columns
   `ts,machine,vibration_mm_s,spindle_temp_c,status`, sorted by machine then ts, where `status` is `ok` or
   `missing`. On a `missing` row, leave BOTH value columns empty.
2. Detect. For each machine, the baseline is the median vibration of its first 60 valid (non-missing) readings.
   A shift is a run of at least 10 consecutive valid readings (missing minutes are skipped, they do not break the
   run) whose vibration is more than 0.8 mm/s above the baseline. Report each shift once, with `start_ts` = the ts
   of the first reading of the run.
3. Report. Write `alerts.json`:
   `{"window": {"start": ..., "end": ...},
     "machines": {"M1": {"rows": 480, "missing": <int>, "duplicates_dropped": <int>}, ...},
     "alerts": [{"machine": ..., "signal": "vibration_mm_s", "start_ts": ..., "baseline": <float>,
                 "level_after": <float, mean of the first 10 readings of the run>}]}`
   `duplicates_dropped` counts input rows for that machine that were not kept.
4. Plot. Write `vibration.svg`: vibration over time for the three machines, with each alert's start marked.
   Plain SVG written by your code, no scripts.
5. Test. Write `test_pipeline.py` with pytest tests for the rules above, and run it.

Put the code in `pipeline.py` so that `python pipeline.py line3_export.csv` regenerates all outputs.
When you are done, reply with a short summary: the alerts found and the per-machine counts.

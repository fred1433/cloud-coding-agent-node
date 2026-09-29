"""Independent acceptance checks for the line 3 task.

These run on the controller side, after the sandbox is gone, against the exported
files and expected.json from the fixture generator. The agent never sees this file
and cannot change it, so a run cannot pass by editing its own tests.
"""
import ast
import csv
import datetime as dt
import io
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent


def reference_from_source(csv_bytes, window_start, minutes):
    """Recompute what the contract implies, directly from the source export."""
    start = dt.datetime.strptime(window_start, "%Y-%m-%dT%H:%M:%SZ")
    grid = [(start + dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(minutes)]
    last = {}
    for r in csv.DictReader(io.StringIO(csv_bytes.decode())):
        last[(r["machine"], r["ts"])] = r
    machines = sorted({m for m, _ in last})
    kept, baselines = {}, {}
    for m in machines:
        valid = []
        for t in grid:
            r = last.get((m, t))
            if r is not None and r["vibration_mm_s"] != "":
                kept[(m, t)] = r
                valid.append((t, float(r["vibration_mm_s"])))
        base = statistics.median(v for _, v in valid[:60])
        runs, cur = [], []
        for t, v in valid + [(None, float("-inf"))]:
            if v > base + 0.8:
                cur.append((t, v))
                continue
            if len(cur) >= 10:
                runs.append({"machine": m, "start_ts": cur[0][0], "baseline": base,
                             "level_after": statistics.mean(x for _, x in cur[:10])})
            cur = []
        baselines[m] = (base, runs)
    return grid, machines, kept, baselines


def check(files, expected, source_csv=None):
    """files: {name: bytes}. Returns a list of {id, what, passed, detail}.
    With source_csv, every kept reading and the alert figures are recomputed from the source."""
    out = []

    def add(cid, what, ok, detail=""):
        out.append({"id": cid, "what": what, "passed": bool(ok), "detail": detail})

    # alerts.json ------------------------------------------------------------
    try:
        alerts = json.loads(files["alerts.json"])
        add("alerts_json", "alerts.json is valid JSON", True)
    except Exception as e:  # noqa: BLE001
        add("alerts_json", "alerts.json is valid JSON", False, str(e)[:120])
        alerts = {}
    got = alerts.get("alerts") or []
    exp = expected["alerts"]
    add("alert_count", "exactly one sustained shift is reported", len(got) == len(exp), f"{len(got)} reported")
    if got:
        a, e = got[0], exp[0]
        add("alert_machine", "the shift is on M2 vibration",
            a.get("machine") == e["machine"] and a.get("signal") == e["signal"],
            f"{a.get('machine')} {a.get('signal')}")
        add("alert_start", "the shift starts at the injected minute", a.get("start_ts") == e["start_ts"],
            f"reported {a.get('start_ts')}, injected {e['start_ts']}")
    spike = expected["not_alerts"][0]
    add("no_spike_alert", "the two-minute spike on M1 is not reported",
        not any(x.get("machine") == spike["machine"] for x in got))
    for m, em in expected["machines"].items():
        gm = (alerts.get("machines") or {}).get(m, {})
        ok = all(gm.get(k) == em[k] for k in ("rows", "missing", "duplicates_dropped"))
        add(f"counts_{m}", f"{m}: rows, missing and dropped duplicates match", ok,
            f"got {gm.get('rows')}/{gm.get('missing')}/{gm.get('duplicates_dropped')}, "
            f"expected {em['rows']}/{em['missing']}/{em['duplicates_dropped']}")

    # normalized.csv ---------------------------------------------------------
    try:
        rows = list(csv.DictReader(io.StringIO(files["normalized.csv"].decode())))
        n_exp = sum(m["rows"] for m in expected["machines"].values())
        add("normalized_rows", "normalized.csv has one row per machine and minute", len(rows) == n_exp,
            f"{len(rows)} rows, expected {n_exp}")
        keyed = {(r.get("machine"), r.get("ts")): r for r in rows}
        miss_ok = all(sum(1 for r in rows if r.get("machine") == m and r.get("status") == "missing") == em["missing"]
                      for m, em in expected["machines"].items())
        add("normalized_missing", "missing minutes are marked missing", miss_ok)
        conf_ok = all(abs(float(keyed.get((c["machine"], c["ts"]), {}).get("vibration_mm_s") or "nan")
                          - float(c["keep"])) < 1e-6 for c in expected["conflicts"])
        add("conflicts_last_wins", "conflicting duplicates keep the last row in file order", conf_ok,
            f"{len(expected['conflicts'])} conflicting minutes checked")
        order = [(r.get("machine"), r.get("ts")) for r in rows]
        add("normalized_sorted", "rows are sorted by machine then ts", order == sorted(order))
        add("normalized_unique", "no machine-minute appears twice", len(set(order)) == len(order),
            f"{len(order) - len(set(order))} duplicates")
        add("normalized_status", "status is only ok or missing",
            all(r.get("status") in ("ok", "missing") for r in rows))
        blank = [r for r in rows if r.get("status") == "missing"
                 and ((r.get("vibration_mm_s") or "") != "" or (r.get("spindle_temp_c") or "") != "")]
        add("missing_rows_blank", "missing rows leave both value columns empty", not blank,
            f"{len(blank)} missing rows still carry a value" + (f", e.g. {blank[0]}" if blank else ""))
        if source_csv is not None:
            w = expected["window"]
            grid, machines, kept, base = reference_from_source(source_csv, w["start"], w["rows_per_machine"])
            want = {(m, t) for m in machines for t in grid}
            add("normalized_keys", "exactly one row for every machine and minute of the window",
                set(order) == want and len(order) == len(want),
                f"{len(want - set(order))} missing keys, {len(set(order) - want)} unexpected keys")
            wrong = 0
            for key, r in keyed.items():
                src = kept.get(key)
                if src is None:
                    wrong += r.get("status") != "missing"
                    continue
                try:
                    wrong += (r.get("status") != "ok"
                              or abs(float(r["vibration_mm_s"]) - float(src["vibration_mm_s"])) > 1e-9
                              or abs(float(r["spindle_temp_c"]) - float(src["spindle_temp_c"])) > 1e-9)
                except (TypeError, ValueError):
                    wrong += 1
            add("readings_match_source", "every kept reading equals the last source row for its minute",
                wrong == 0, f"{wrong} rows differ from the source")
            exp_alerts = [a for m in machines for a in base[m][1]]
            ok_fig = len(got) == len(exp_alerts) and all(
                g.get("machine") == e["machine"] and g.get("start_ts") == e["start_ts"]
                and abs(float(g.get("baseline", "nan")) - e["baseline"]) < 1e-3
                and abs(float(g.get("level_after", "nan")) - e["level_after"]) < 1e-3
                for g, e in zip(sorted(got, key=lambda x: str(x.get("machine"))), exp_alerts))
            add("alert_figures", "baseline and post-shift level match an independent recomputation", ok_fig,
                "; ".join(f"{e['machine']} baseline {e['baseline']:.4f}, level {e['level_after']:.4f}"
                          for e in exp_alerts))
    except Exception as e:  # noqa: BLE001
        add("normalized_csv", "normalized.csv is readable", False, str(e)[:120])

    # vibration.svg ----------------------------------------------------------
    svg = files.get("vibration.svg", b"")
    try:
        if len(svg) > 2_000_000 or b"<!DOCTYPE" in svg or b"<!ENTITY" in svg:
            raise ValueError("oversized or has a DOCTYPE/ENTITY")
        root = ET.fromstring(svg)
        is_svg = root.tag.endswith("svg")
        active = [el.tag for el in root.iter() if el.tag.split("}")[-1] in ("script", "foreignObject")]
        handlers = [k for el in root.iter() for k in el.attrib if k.lower().startswith("on")]
        ext = [v for el in root.iter() for k, v in el.attrib.items()
               if k.endswith("href") and not v.startswith("#")]
        add("svg_valid", "vibration.svg is an SVG document", is_svg)
        numbers = sum(len(re.findall(r"-?\d+(?:\.\d+)?", " ".join(el.attrib.values())))
                      for el in root.iter())
        add("svg_plotted", "vibration.svg actually plots the series (hundreds of coordinates)", numbers > 1000,
            f"{numbers} numeric coordinates")
        add("svg_inert", "vibration.svg has no scripts, event handlers or external links",
            not active and not handlers and not ext)
    except Exception as e:  # noqa: BLE001
        add("svg_valid", "vibration.svg is an SVG document", False, str(e)[:120])
    def real_python(name):
        try:
            tree = ast.parse(files.get(name, b"").decode())
        except (SyntaxError, UnicodeDecodeError):
            return False
        return any(isinstance(n, ast.FunctionDef) for n in ast.walk(tree))

    add("pipeline_present", "pipeline.py and test_pipeline.py are non-empty Python with functions",
        real_python("pipeline.py") and real_python("test_pipeline.py"))
    return out


def reexecute(files, source_csv, runtime="runc"):
    """Re-run the delivered code in a NEW disposable sandbox (never on the controller): it must
    regenerate the delivered outputs byte for byte, and its own tests must pass."""
    sys.path.insert(0, str(HERE.parents[1]))
    from codenode.sandbox import Sandbox
    out = []
    limits = {"pids": 128, "memory_mb": 1024, "cpus": 1, "workspace_mb": 256, "command_timeout_s": 120,
              "deadline_s": 300}
    sb = Sandbox("acceptance", limits, runtime=runtime).start()
    try:
        sb.put("line3_export.csv", source_csv)
        for name in ("pipeline.py", "test_pipeline.py"):
            if name in files:
                sb.put(name, files[name])
        run = sb.bash("python pipeline.py line3_export.csv", timeout=120)
        same = []
        for name in ("alerts.json", "normalized.csv", "vibration.svg"):
            try:
                same.append(sb.get(name) == files.get(name))
            except Exception:  # noqa: BLE001
                same.append(False)
        out.append({"id": "rerun_reproduces", "what": "re-running pipeline.py in a fresh sandbox reproduces "
                    "alerts.json, normalized.csv and vibration.svg exactly",
                    "passed": run["exit_code"] == 0 and all(same),
                    "detail": f"exit {run['exit_code']}, identical files {sum(same)} of 3"})
        tests = sb.bash("python -m pytest -q test_pipeline.py 2>&1 | tail -1", timeout=120)
        out.append({"id": "own_tests_pass", "what": "the delivered tests pass in the fresh sandbox",
                    "passed": " passed" in tests["output"] and "failed" not in tests["output"]
                    and "error" not in tests["output"], "detail": tests["output"].strip()[:120]})
    finally:
        sb.destroy()
    return out


def full_check(files, expected, source_csv, runtime="runc"):
    return check(files, expected, source_csv) + reexecute(files, source_csv, runtime)


def main(out_dir):
    out = Path(out_dir)
    files = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    expected = json.loads((HERE / "fixture" / "expected.json").read_text())
    res = full_check(files, expected, (HERE / "fixture" / "line3_export.csv").read_bytes())
    for r in res:
        print(("PASS " if r["passed"] else "FAIL ") + r["what"] + (f"  ({r['detail']})" if r["detail"] else ""))
    return 0 if all(r["passed"] for r in res) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

"""Independent acceptance checks for the line 3 task.

These run on the controller side, after the sandbox is gone, against the exported
files and expected.json from the fixture generator. The agent never sees this file
and cannot change it, so a run cannot pass by editing its own tests.
"""
import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent


def check(files, expected):
    """files: {name: bytes}. Returns a list of {id, what, passed, detail}."""
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
        add("svg_inert", "vibration.svg has no scripts, event handlers or external links",
            not active and not handlers and not ext)
    except Exception as e:  # noqa: BLE001
        add("svg_valid", "vibration.svg is an SVG document", False, str(e)[:120])
    add("pipeline_present", "pipeline.py and test_pipeline.py are delivered",
        "pipeline.py" in files and "test_pipeline.py" in files)
    return out


def main(out_dir):
    out = Path(out_dir)
    files = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    expected = json.loads((HERE / "fixture" / "expected.json").read_text())
    res = check(files, expected)
    for r in res:
        print(("PASS " if r["passed"] else "FAIL ") + r["what"] + (f"  ({r['detail']})" if r["detail"] else ""))
    return 0 if all(r["passed"] for r in res) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

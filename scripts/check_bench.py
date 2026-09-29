"""Gate for an informational suite run: the result artifact must exist and be valid, and every
FAILED case must be a known, documented outcome for that runtime. New failures, crashed cases,
missing cases or leftovers fail the job. Writes a short summary for the CI page.

    python scripts/check_bench.py bench/results-runsc.json bench/known-runsc.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codenode.attacks import ATTACKS  # noqa: E402


def main(results_path, known_path):
    try:
        doc = json.load(open(results_path))
        results = {r["id"]: r for r in doc["results"]}
    except Exception as e:  # noqa: BLE001
        print(f"::error::no valid result artifact: {e}")
        return 1
    known = json.load(open(known_path))["known_failures"]
    problems = []
    missing = [a[0] for a in ATTACKS if a[0] not in results]
    if missing:
        problems.append(f"cases missing from the artifact: {missing}")
    for aid, r in results.items():
        if r["outcome"] == "FAILED" and aid not in known:
            problems.append(f"new failure: {aid}: {r['observed'][:200]}")
        if r["outcome"] == "FAILED" and "suite error" in r["observed"]:
            problems.append(f"case crashed: {aid}")
        if "containers left: 1" in r["observed"] or "containers left: 2" in r["observed"]:
            problems.append(f"leftover container reported by {aid}")
    now_holding = [k for k in known if results.get(k, {}).get("outcome") == "held"]
    held = sum(r["outcome"] == "held" for r in results.values())
    failed = [k for k, r in results.items() if r["outcome"] == "FAILED"]
    summary = ""
    if not problems:
        summary = (f"{held} of {len(results)} cases held. Known outcomes observed: "
                   + (", ".join(f"{k} ({known[k]})" for k in failed if k in known) or "none")
                   + (f". Known failures that now hold: {now_holding}" if now_holding else "")
                   + ". No leftover containers reported.")
    with open("bench-summary.md", "w") as f:
        f.write(("### " + doc.get("runtime", "?") + " attack suite\n\n")
                + (summary if not problems else "Unexpected: " + "; ".join(problems)) + "\n")
    print(summary or "\n".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))

"""Run the line 3 task end to end: fixture -> coding node -> independent acceptance checks.

    python examples/line3/run_demo.py            # real model (needs ANTHROPIC_API_KEY on the host)
    python examples/line3/run_demo.py --scripted # no API call: a scripted agent writes a known answer

Writes runs/<name>.jsonl (the event stream, unedited), runs/<name>/ (exported outputs)
and runs/<name>.acceptance.json.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from acceptance import full_check  # noqa: E402
from make_fixture import main as make_fixture  # noqa: E402

from codenode.models import ScriptedModel  # noqa: E402
from codenode.node import TenantContext, run_node  # noqa: E402

NODE = {
    "tools": ["bash", "editor", "grep", "glob"],
    "effort": "medium",
    "limits": {"max_steps": 40, "budget_usd": 2.0, "deadline_s": 1200, "command_timeout_s": 120,
               "max_tokens_per_turn": 16000, "memory_mb": 1024, "workspace_mb": 256},
}
OUTPUTS = ["pipeline.py", "test_pipeline.py", "normalized.csv", "alerts.json", "vibration.svg"]


def scripted():
    ref = (ROOT / "tests" / "reference_line3.py").read_text()
    prog = ref + "\nimport sys\nfor k, v in solve(open(sys.argv[1], 'rb').read()).items():\n" \
                 "    open(k, 'wb').write(v) if k not in ('pipeline.py', 'test_pipeline.py') else None\n"
    return ScriptedModel([
        [{"type": "tool_use", "name": "bash", "input": {"command": "head -3 line3_export.csv; wc -l line3_export.csv"}}],
        [{"type": "tool_use", "name": "str_replace_based_edit_tool",
          "input": {"command": "create", "path": "pipeline.py", "file_text": prog}}],
        [{"type": "tool_use", "name": "str_replace_based_edit_tool",
          "input": {"command": "create", "path": "test_pipeline.py", "file_text": "def test_ok():\n    assert True\n"}}],
        [{"type": "tool_use", "name": "bash", "input": {"command": "python pipeline.py line3_export.csv && pytest -q"}}],
        [{"type": "text", "text": "Done (scripted)."}],
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    name = a.name or ("line3-scripted" if a.scripted else "line3")
    here = Path(__file__).parent
    make_fixture(here / "fixture")
    task = (here / "task.md").read_text()
    csv_bytes = (here / "fixture" / "line3_export.csv").read_bytes()
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    expected = json.loads((here / "fixture" / "expected.json").read_text())

    def acceptance(outputs, inputs):     # runs after the sandbox is gone; re-execution in a fresh one
        return full_check(outputs, expected, csv_bytes)

    res = run_node(TenantContext("demo-tenant"), NODE, task, {"line3_export.csv": csv_bytes}, outputs=OUTPUTS,
                   model=scripted() if a.scripted else None, events_path=runs / f"{name}.jsonl",
                   out_dir=runs / name, run_id=name, acceptance=acceptance)
    acc = res["acceptance"]
    (runs / f"{name}.acceptance.json").write_text(json.dumps(
        {"run_id": name, "status": res["status"], "outputs_accepted": res["outputs_accepted"], "checks": acc},
        indent=2) + "\n")
    led = res["ledger"]
    print(f"status {res['status']} {res.get('reason', '')}")
    print(f"requests {len(led['requests'])}, tool calls {led['tool_calls']}, cost estimate ${led['cost_usd']:.4f}")
    for r in acc:
        print(("PASS " if r["passed"] else "FAIL ") + r["what"] + (f"  ({r['detail']})" if r["detail"] else ""))
    return 0 if res["status"] == "succeeded" and res["outputs_accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())

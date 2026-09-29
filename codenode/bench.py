"""Run the attack suite and write bench/results.json (python -m codenode.bench [runtime] [ids...])."""
import datetime as dt
import json
import platform
import subprocess
import sys
from pathlib import Path

from .attacks import run_all
from .sandbox import IMAGE


def docker_info():
    p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}} {{.OperatingSystem}} {{.KernelVersion}} "
                        "{{.CgroupVersion}} {{.SecurityOptions}}"], capture_output=True)
    return p.stdout.decode().strip()


def main():
    runtime = sys.argv[1] if len(sys.argv) > 1 else "runc"
    only = sys.argv[2:] or None
    results = run_all(runtime, only)
    digest = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], capture_output=True)
    doc = {
        "label": "Scripted tool requests against the real runtime. These tests measure enforcement, "
                 "not the model's resistance to prompt injection.",
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runtime": runtime,
        "host": {"docker": docker_info(), "controller": f"{platform.system()} {platform.machine()} "
                                                        f"Python {platform.python_version()}"},
        "image_digest": digest.stdout.decode().strip(),
        "results": results,
    }
    out = Path("bench") / (f"results-{runtime}.json" if not only else f"partial-{runtime}.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    for r in results:
        print(f"{r['outcome']:8} {r['id']:18} {r['seconds']:6.1f}s  {r['observed'][:150]}")
    failed = [r for r in results if r["outcome"] == "FAILED"]
    print(f"\n{len(results) - len(failed)} of {len(results)} not failed ({sum(r['outcome'] == 'SKIPPED' for r in results)} skipped); wrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Node behaviour a workflow engine relies on (Docker required, no API key)."""
from conftest import needs_docker

from codenode.models import ScriptedModel
from codenode.node import TenantContext, run_node


def bash(cmd):
    return {"type": "tool_use", "name": "bash", "input": {"command": cmd}}


@needs_docker
def test_bash_state_persists_between_calls_and_restart_clears_it(runtime):
    m = ScriptedModel([
        [bash("mkdir -p sub && cd sub && export FOO=bar && greet() { echo hello-$1; }")],
        [bash("pwd; echo FOO=$FOO; greet x")],
        [{"type": "tool_use", "name": "bash", "input": {"restart": True}}],
        [bash("pwd; echo FOO=$FOO")],
    ])
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t", model=m)
    out = [e["untrusted_output"] for e in run["events"] if e["type"] == "tool.result"]
    assert "/workspace/sub" in out[1] and "FOO=bar" in out[1] and "hello-x" in out[1]
    assert "/workspace\n" in out[3] and "FOO=\n" in out[3]


@needs_docker
def test_budget_reservation_stops_before_the_call(runtime):
    big = {"input_tokens": 200_000, "output_tokens": 8000, "cache_creation_input_tokens": 0,
           "cache_read_input_tokens": 0}
    m = ScriptedModel([lambda i: [bash("echo again")]], usage=big)
    run = run_node(TenantContext("t"), {"runtime": runtime, "limits": {"budget_usd": 1.5, "max_steps": 50}},
                   "loop forever", model=m)
    assert run["status"] == "policy_blocked" and "budget" in run["reason"]
    assert run["ledger"]["cost_usd"] <= 1.5
    assert m.calls == len(run["ledger"]["requests"])


@needs_docker
def test_terminal_states(runtime):
    ok = run_node(TenantContext("t"), {"runtime": runtime}, "t", model=ScriptedModel([[bash("true")]]))
    assert ok["status"] == "succeeded"
    steps = run_node(TenantContext("t"), {"runtime": runtime, "limits": {"max_steps": 2}}, "t",
                     model=ScriptedModel([lambda i: [bash("true")]]))
    assert steps["status"] == "policy_blocked" and "max_steps" in steps["reason"]
    blocked = run_node(TenantContext("t"), {"runtime": runtime, "limits": {"budget_usd": 9}}, "t",
                       model=ScriptedModel([]))
    assert blocked["status"] == "policy_blocked" and "ceiling" in blocked["reason"]


@needs_docker
def test_inputs_that_are_not_outputs_are_not_reported_as_deleted(runtime):
    m = ScriptedModel([[bash("echo out > result.txt")]])
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t", {"input.csv": "a,b\n"}, outputs=["*.txt"], model=m)
    assert run["files_changed"] == ["result.txt"]
    assert "input.csv" not in run["diff"] and "/dev/null" in run["diff"]


@needs_docker
def test_run_ids_are_per_tenant(runtime):
    import time
    from codenode.service import NodeService, RunNotFound
    svc = NodeService(model_factory=lambda: ScriptedModel([[bash("true")]]))
    a, b = TenantContext("a"), TenantContext("b")
    assert svc.start(b, "shared-id", {"runtime": runtime}, "b's task")["started"]      # B first
    assert svc.start(a, "shared-id", {"runtime": runtime}, "a's task")["started"]      # A not blocked
    ra, rb = svc.wait(a, "shared-id", 120), svc.wait(b, "shared-id", 120)
    assert ra["run_id"] != rb["run_id"]
    tasks = {t: [e["task"] for e in svc.events(ctx, "shared-id") if e["type"] == "run.started"][0]
             for t, ctx in (("a", a), ("b", b))}
    assert tasks == {"a": "a's task", "b": "b's task"}
    try:
        svc.events(TenantContext("c"), "shared-id")
        raise AssertionError("tenant c saw a run")
    except RunNotFound:
        pass


@needs_docker
def test_reexecution_in_a_fresh_sandbox_catches_code_that_does_not_produce_the_outputs(runtime):
    from pathlib import Path
    from acceptance import reexecute
    from reference_line3 import solve
    root = Path(__file__).resolve().parents[1]
    src = (root / "examples" / "line3" / "fixture" / "line3_export.csv").read_bytes()
    prog = (root / "tests" / "reference_line3.py").read_text() + (
        "\nimport sys\nfor k, v in solve(open(sys.argv[1], 'rb').read()).items():\n"
        "    open(k, 'wb').write(v) if k.endswith(('.json', '.csv', '.svg')) else None\n")
    files = {k: v for k, v in solve(src).items() if not k.endswith(".py")}
    files["pipeline.py"] = prog.encode()
    files["test_pipeline.py"] = b"def test_ok():\n    assert True\n"
    good = {r["id"]: r["passed"] for r in reexecute(files, src, runtime)}
    assert good == {"rerun_reproduces": True, "own_tests_pass": True}
    bad = {r["id"]: r["passed"] for r in reexecute(dict(files, **{"pipeline.py": b"print(1)\n"}), src, runtime)}
    assert bad["rerun_reproduces"] is False


@needs_docker
def test_exactly_one_terminal_event_when_startup_fails(runtime, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t")      # no model given, no key on the host
    fins = [e for e in run["events"] if e["type"] == "run.finished"]
    assert len(fins) == 1 and fins[0]["status"] == "failed" and "ANTHROPIC_API_KEY" in fins[0]["reason"]


@needs_docker
def test_exactly_one_terminal_event_when_writing_outputs_fails(runtime, tmp_path):
    blocker = tmp_path / "out"
    blocker.write_text("a file where the output directory should be")
    m = ScriptedModel([[bash("echo x > r.txt")]])
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t", outputs=["*.txt"], model=m, out_dir=blocker)
    fins = [e for e in run["events"] if e["type"] == "run.finished"]
    assert len(fins) == 1 and fins[0]["status"] == "failed" and "writing outputs failed" in fins[0]["reason"]


@needs_docker
def test_nonzero_exit_is_a_counted_tool_error(runtime):
    m = ScriptedModel([[bash("exit 7")]])
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t", model=m)
    res = [e for e in run["events"] if e["type"] == "tool.result"][0]
    assert res["is_error"] is True and "[exit code 7]" in res["untrusted_output"]
    assert run["ledger"]["tool_errors"] == 1 and run["status"] == "succeeded"


@needs_docker
def test_diff_applies_with_git(runtime, tmp_path):
    import subprocess
    m = ScriptedModel([[bash("printf 'no newline' > a.txt; printf 'x\\ny' > b.txt; echo ok > c.txt")]])
    run = run_node(TenantContext("t"), {"runtime": runtime}, "t", {"b.txt": "x\n"}, outputs=["*.txt"], model=m)
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "b.txt").write_text("x\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "p.diff").write_text(run["diff"])
    chk = subprocess.run(["git", "apply", "--check", "p.diff"], cwd=repo, capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr
    subprocess.run(["git", "apply", "p.diff"], cwd=repo, check=True)
    assert (repo / "a.txt").read_bytes() == b"no newline" and (repo / "b.txt").read_bytes() == b"x\ny"

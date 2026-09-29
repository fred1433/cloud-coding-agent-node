"""Tests that need neither Docker nor an API key."""
import json
from pathlib import Path

import jsonschema
import pytest

from codenode import config
from codenode.node import DEFAULT_POLICY, TenantContext, policy_violations, reservation_tokens, SYSTEM
from codenode.pricing import PriceTable
from codenode.text import clean
from codenode.tools import anthropic_tools

ROOT = Path(__file__).resolve().parents[1]


def recorded_runs():
    return sorted((ROOT / "runs").glob("line3-run*.jsonl"))


@pytest.mark.parametrize("path", recorded_runs(), ids=lambda p: p.name)
def test_recorded_costs_recompute_from_raw_usage(path):
    """Every dollar figure in a recorded run is recomputed from the raw usage block and
    the dated price table; the run total is the sum of the per-request costs."""
    events = [json.loads(line) for line in path.read_text().splitlines()]
    started = events[0]
    prices = PriceTable()
    assert started["prices_as_of"] == prices.as_of
    responses = [e for e in events if e["type"] == "model.response"]
    total = 0.0
    for r in responses:
        u = r["usage"]
        assert r["cost_usd"] == prices.cost(started["model_requested"], u)
        # the four counters are disjoint: input_tokens excludes cached tokens
        assert all(isinstance(u.get(k, 0), int) for k in
                   ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        total += r["cost_usd"]
    fin = events[-1]
    assert fin["type"] == "run.finished"
    assert abs(fin["cost_usd"] - total) < 1e-6
    assert fin["requests"] == len(responses)
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        assert fin["tokens"][k] == sum(r["usage"].get(k) or 0 for r in responses)


@pytest.mark.parametrize("path", recorded_runs(), ids=lambda p: p.name)
def test_reservation_bounded_every_recorded_request(path):
    """The pre-request reservation was never below what the API then reported."""
    events = [json.loads(line) for line in path.read_text().splitlines()]
    reqs = {e["step"]: e for e in events if e["type"] == "model.request"}
    for r in (e for e in events if e["type"] == "model.response"):
        u = r["usage"]
        actual_in = u["input_tokens"] + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        assert reqs[r["step"]]["reservation_input_tokens"] >= actual_in


def test_cost_formula_prices_each_counter():
    p = PriceTable()
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
         "cache_read_input_tokens": 1_000_000}
    r = p.rates("claude-sonnet-5")
    assert p.cost("claude-sonnet-5", u) == pytest.approx(r["input"] + r["output"] + r["cache_write_5m"] + r["cache_read"])
    split = dict(u, cache_creation={"ephemeral_5m_input_tokens": 400_000, "ephemeral_1h_input_tokens": 600_000})
    assert p.cost("claude-sonnet-5", split) == pytest.approx(
        r["input"] + r["output"] + 0.4 * r["cache_write_5m"] + 0.6 * r["cache_write_1h"] + r["cache_read"])


def test_reservation_is_above_a_measured_token_count():
    # 2855 is what the API's count_tokens endpoint returned for this exact request on 2026-09-29
    tools = anthropic_tools(["bash", "editor", "grep", "glob"])
    task = (ROOT / "examples" / "line3" / "task.md").read_text()
    assert reservation_tokens(SYSTEM, tools, [{"role": "user", "content": task}]) >= 2855


def test_config_defaults_and_rejections():
    c = config.load({})
    assert c["network"]["mode"] == "none" and c["limits"]["max_steps"] == 30
    with pytest.raises(jsonschema.ValidationError):
        config.load({"limits": {"max_steps": 0}})
    with pytest.raises(jsonschema.ValidationError):
        config.load({"docker_args": ["--privileged"]})      # no way to pass raw container options
    with pytest.raises(jsonschema.ValidationError):
        config.load({"tools": ["fetch_url"]})               # fetch without an allowlist


def test_tenant_policy_is_enforced_server_side():
    ctx = TenantContext("t", DEFAULT_POLICY)
    c = config.load({"limits": {"budget_usd": 20}, "network": {"mode": "egress_proxy", "allow": ["pypi.org"]}})
    v = policy_violations(c, ctx)
    assert any("budget_usd" in x for x in v) and any("network mode" in x for x in v)


def test_clean_strips_ansi_and_bad_bytes():
    raw = b"\x1b[31mred\x1b[0m ok \xff\xfe bell\x07 \x1b]0;title\x07end"
    out = clean(raw)
    assert "\x1b" not in out and "\x07" not in out and "red" in out and "end" in out and "�" in out
    assert "characters cut" in clean("x" * 50_000, 1000)


def test_anthropic_defined_tools_are_declared_without_schema():
    t = anthropic_tools(["bash", "editor", "grep"])
    assert {"type": "bash_20250124", "name": "bash"} in t
    assert {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"} in t
    assert any(x.get("name") == "grep" and "input_schema" in x for x in t)


def test_helpers_run_in_an_isolated_interpreter():
    from codenode.sandbox import HELPER_PY, Sandbox
    assert HELPER_PY[0] == "/usr/local/bin/python3" and "-I" in HELPER_PY and "-S" in HELPER_PY
    args = Sandbox("t", {"pids": 64, "memory_mb": 256, "cpus": 1, "workspace_mb": 16}).docker_run_args()
    assert args[-5:-2] == HELPER_PY and args[-2] == "/opt/codenode/init.py"

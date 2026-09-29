"""The attack suite as tests. Each case must hold, or say SKIPPED with its reason."""
import pytest
from conftest import needs_docker

from codenode.attacks import ATTACKS, run_all


@needs_docker
@pytest.mark.parametrize("aid", [a[0] for a in ATTACKS])
def test_attack(aid, runtime):
    r = run_all(runtime, [aid])[0]
    if r["outcome"] == "SKIPPED":
        pytest.skip(r["observed"])
    assert r["outcome"] == "held", r["observed"]

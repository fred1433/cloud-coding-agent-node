import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "line3"))
sys.path.insert(0, str(Path(__file__).parent))


def docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", "codenode-sandbox:dev"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(not docker_ok(), reason="Docker and the codenode-sandbox:dev image are required "
                                                          "(run `make image`)")


def pytest_addoption(parser):
    parser.addoption("--runtime", default="runc", help="container runtime for the sandbox (runc or runsc)")


@pytest.fixture
def runtime(request):
    return request.config.getoption("--runtime")

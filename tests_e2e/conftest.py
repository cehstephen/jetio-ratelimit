"""End-to-end test harness: launches a scenario app as a real subprocess
(real uvicorn, real TCP port, real HTTP over the wire) rather than the
in-process ASGI shortcuts unit tests use. This is deliberately closer to
how the package is actually deployed and used -- the point is to prove
these scenarios hold up as a real running server, not just that the
Python objects behave correctly when called directly.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

APPS_DIR = Path(__file__).parent / "apps"
REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(base_url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/docs", timeout=1.0)
            if resp.status_code == 200:
                return
        except httpx.TransportError as e:
            last_error = e
        time.sleep(0.2)
    raise RuntimeError(f"scenario app never became ready at {base_url}: {last_error}")


def run_scenario_app(tmp_path, script_name: str, extra_env: dict = None):
    """Starts apps/<script_name> as a subprocess bound to a free port,
    with its sqlite db isolated in tmp_path. Returns (process, base_url).
    Caller is responsible for terminating the process."""
    port = _free_port()
    script = APPS_DIR / script_name
    env = {
        "PYTHONIOENCODING": "utf-8",
        "JETIO_APP_PORT": str(port),
        # Run under `coverage run --parallel-mode` (below) rather than a
        # plain `python <script>`, so this subprocess's execution counts
        # toward the coverage report -- otherwise every line only these
        # scenario apps exercise (most of middleware.py, dependency.py's
        # actual check) shows up as untested even though it's the whole
        # point of this suite. COVERAGE_FILE/RCFILE are absolute paths
        # since cwd is tmp_path, not the repo -- coverage won't find
        # pyproject.toml's [tool.coverage.run] config or write its data
        # file next to the main run's otherwise.
        "COVERAGE_FILE": str(REPO_ROOT / ".coverage"),
        "COVERAGE_RCFILE": str(REPO_ROOT / "pyproject.toml"),
        **(extra_env or {}),
    }
    import os

    full_env = {**os.environ, **env}

    process = subprocess.Popen(
        [sys.executable, "-m", "coverage", "run", "--parallel-mode", str(script)],
        cwd=str(tmp_path),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
    except RuntimeError:
        process.terminate()
        out, _ = process.communicate(timeout=5)
        raise RuntimeError(f"scenario app failed to start; output:\n{out}")
    return process, base_url


def stop_scenario_app(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture
def saas_app(tmp_path):
    process, base_url = run_scenario_app(tmp_path, "saas_scenario_app.py")
    yield base_url
    stop_scenario_app(process)


@pytest.fixture
def public_api_app(tmp_path):
    process, base_url = run_scenario_app(tmp_path, "public_api_scenario_app.py")
    yield base_url
    stop_scenario_app(process)

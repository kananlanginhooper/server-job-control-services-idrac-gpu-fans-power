"""Test setup. Automated tests only ever use the mocks: the real .env (live hosts, live control) is never read."""
import os
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# must happen before anything imports svcenv: point it at an empty settings file
os.environ["SERVICES_ENV_FILE"] = os.path.join(ROOT, "tests", "empty.env")
for k in [k for k in os.environ if k.startswith(("SJC_", "HUB_", "GPU_", "RECOVERY_DB_"))]:
    del os.environ[k]
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "lib", "sjc_hw"), os.path.join(ROOT, "mocks")]


def mock_cmd(name):
    """Command line for a mock tool in mocks/bin, runnable on Windows and Linux."""
    py = sys.executable.replace("\\", "/")
    return f"{shlex.quote(py)} {shlex.quote(os.path.join(ROOT, 'mocks', 'bin', name).replace(chr(92), '/'))}"


# sjc_hw reads its settings at import time: give it mocks before any test imports it
os.environ["SJC_IPMITOOL"] = mock_cmd("ipmitool.py")
os.environ["SJC_NVIDIA_SMI"] = ""          # set per test where a GPU is wanted
os.environ["SJC_HOST"] = "test-host"

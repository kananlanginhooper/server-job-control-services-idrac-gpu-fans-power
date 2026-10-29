#!/usr/bin/env python3
"""Mock nvidia-smi: one GPU with canned readings; power-limit changes are logged instead of applied.

Use it wherever a service takes the nvidia-smi command, e.g.  SJC_NVIDIA_SMI="python mocks/bin/nvidia-smi.py"
(sjc_hw runs it through a list, so the interpreter prefix works). Writes go to MOCK_NVSMI_LOG if set.
MOCK_GPU_TEMP sets the temperature (to test Emergency handling).
"""

import os
import sys

TEMP = os.environ.get("MOCK_GPU_TEMP", "71")
# the field order sjc_hw.gpu.FIELDS asks for
ROW = f"0, Mock GPU 24GB, {TEMP}, 87, 20480, 24564, 65, 301.50, 400.00, 150.00, 450.00, 2610, 54, 3, 16"
APPS = "4242, ollama, 18000\n"


def main(a):
    if any(x.startswith("--query-gpu=") for x in a):
        print(ROW)
    elif any(x.startswith("--query-compute-apps=") for x in a):
        sys.stdout.write(APPS)
    elif "-pl" in a or "-pm" in a:
        p = os.environ.get("MOCK_NVSMI_LOG")
        if p:
            with open(p, "a") as f:
                f.write(" ".join(a) + "\n")
        print("Power limit set (mock).")
    elif a[:1] == ["-L"]:
        print("GPU 0: Mock GPU 24GB (UUID: GPU-00000000-0000-0000-0000-000000000000)")
    else:
        sys.stderr.write(f"mock nvidia-smi: unsupported {' '.join(a)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

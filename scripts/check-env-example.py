#!/usr/bin/env python3
"""Check that .env.example lists every setting the code reads (DEV-STANDARD section 2).

A key counts as listed when .env.example has it as `KEY=`, `# KEY=` or `# <HOST>_KEY=` (the per-server form).
Exit 1 and print the missing keys otherwise. Never reads the real .env.
    python scripts/check-env-example.py
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = ("services", "lib", "scripts")
READS = (
    r'\benv(?:_int)?\(\s*["\']([A-Z][A-Z0-9_]+)["\']',  # svcenv.env("KEY") / env_int("KEY")
    r'os\.environ(?:\.get)?\(?\[?\s*["\']([A-Z][A-Z0-9_]+)["\']',  # os.environ.get("KEY") / os.environ["KEY"]
    r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]+)["\']',
    r"\bsvc_env\s+([A-Z][A-Z0-9_]+)",  # svcenv.sh
)
# set by the system or by the runner, not by the .env
NOT_SETTINGS = {"HOME", "PATH", "USER", "SUDO_USER", "NAME", "SVC_HOST", "TASK_NAME", "TASK_INSTANCE", "INVOCATION_ID"}


def keys_read() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for top in SCAN:
        for d, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith((".py", ".sh", ".service", ".timer")) and "." in f:
                    continue
                path = os.path.join(d, f)
                if os.path.abspath(path) == os.path.abspath(__file__):
                    continue  # its own docstring shows example keys
                try:
                    text = open(path, encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                for pat in READS:
                    for k in re.findall(pat, text):
                        if k not in NOT_SETTINGS:
                            found.setdefault(k, set()).add(os.path.relpath(path, ROOT).replace(os.sep, "/"))
    return found


def keys_listed(path: str) -> set[str]:
    out = set()
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\s*#?\s*(?:<HOST>_)?([A-Z][A-Z0-9_]+)=", line)
        if m:
            out.add(m.group(1))
    return out


def missing(example: str | None = None) -> dict[str, set[str]]:
    listed = keys_listed(example or os.path.join(ROOT, ".env.example"))
    return {k: v for k, v in keys_read().items() if k not in listed}


if __name__ == "__main__":
    gone = missing()
    for k in sorted(gone):
        print(f"missing from .env.example: {k:28} read by {', '.join(sorted(gone[k]))}")
    sys.exit(1 if gone else 0)

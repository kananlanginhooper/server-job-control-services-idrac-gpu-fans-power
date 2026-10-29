#!/usr/bin/env python3
"""Print what a server will resolve from the shared .env, for every key the services use (secrets masked).
   usage: python scripts/show-env.py <HOSTNAME>   (e.g. the short hostname of the server you're about to cut over)"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import svcenv  # noqa: E402

if len(sys.argv) > 1:
    svcenv.HOST = re.sub(r"[^A-Z0-9]", "_", sys.argv[1].split(".")[0].upper())
keys = set()
for d, _, files in os.walk(ROOT):
    if any(x in d for x in (".git", "snapshot-", "private", "tests", "mocks")):
        continue
    for f in files:
        if f.endswith((".py", ".sh", ".service")) or f in ("taskctl",):
            src = open(os.path.join(d, f), encoding="utf-8", errors="replace").read()
            keys |= set(re.findall(r'env\("([A-Z_]+)"', src)) | set(re.findall(r"svc_env ([A-Z_]+)", src))
keys.discard("NAME")
print(f"host {svcenv.HOST}, settings file {svcenv.ENV_FILE}")
for k in sorted(keys):
    v = svcenv.env(k)
    src = "host" if svcenv.env(k) is not None and (os.environ.get(f"{svcenv.HOST}_{k}") or svcenv.FILE.get(f"{svcenv.HOST}_{k}")) else "shared"
    if v is not None and re.search(r"PASSWORD|SECRET|TOKEN", k):
        v = "(set)"
    print(f"  {k:24} {'-' if v is None else v}   [{src if v is not None else 'default in code'}]")

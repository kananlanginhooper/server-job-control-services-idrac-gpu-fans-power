# Refactor to the shared template (branch `refactor/template`)

Python side of the group's "Shared stack and code structure" (there's no UI here). It's a structure and tooling change only: **no behavior change**. Merging waits for the owner's review.

## What changed

| Step | What | Proof |
|---|---|---|
| 1 | `pyproject.toml`: ruff (lint + format) and pytest/coverage config. Code formatted with `ruff format`, safe lint fixes applied | `scripts/verify_refactor.py main` (below) |
| 2 | Type hints, with `from __future__ import annotations` so they're never evaluated at runtime. Done for every Python file. fan-control, dash-agent and taskrunner each got their own commit, AST-identical to the commit before it | same |
| 3 | More mock tests: 22 in all, coverage 62 % of the modules they load (`python -m pytest --cov`) | `python -m pytest` |
| 4 | One config module: already the case (`lib/svcenv.py`, plus `lib/svcenv.sh` for the shell scripts). `lib/` holds the shared code (`svcenv`, `sjc_hw`) | n/a |
| 5 | FastAPI: a proposal only ([`fastapi-proposal.md`](fastapi-proposal.md)) | n/a |

## How "no behavior change" is proven

```bash
python scripts/verify_refactor.py main --strict services/fan-control/fan-control.py services/power-guard/power-guard.py services/peak-power/peak-power.py services/sjc-agent/sjc_agent.py
```

- It compares every Python file's **AST** at `main` with the working tree.
- It ignores what can't change what runs: formatting, comments, docstrings, type annotations and `from __future__ import annotations`.
- **Result: the live controllers (fan-control, power-guard, peak-power, sjc-agent) and dash-agent are identical to `main`.** They also got no lint edits at all (per-file ignores in `pyproject.toml`).
- The other files differ only in:
  - import order (ruff's isort): the same names, with each file's `sys.path` line still before `from svcenv import env` (checked);
  - typing-only imports (`typing.Any`, `collections.abc.Iterator`, `subprocess` for a return type) in `lib/svcenv.py` and `lib/sjc_hw/idrac.py`;
  - one unused import removed (`PIL.ImageOps` in vision-server);
  - one unused loop name renamed (`key` → `_key` in taskrunner).

## Left for later (listed, not done tonight)

- **Callable parameters are typed `object`** (dash-agent's `gpu_fan`/`gpu_temp`/`gpu_mw`, sjc-agent's `loop(fn)`). A precise `Callable[...]` would need a `typing` import in a controller file, and that breaks the "identical AST" proof. Switch them when the controllers are next edited for real.
- **Unused names in fan-control** are left in place on purpose (no edits to the controller): an unused `socket` import, `prev_card` / `prev_warm` (line ~1087), and `work_why` / `working` (line ~1141). Remove them in a normal reviewed change if wanted.
- **Moving duplicated helpers into `lib/`:** the services each have small `log()`, atomic-JSON-write and dashboard-POST helpers with slightly different timeouts and error handling. Merging them would change the controllers' code, so it's left for after the cutover, behind its own tests.

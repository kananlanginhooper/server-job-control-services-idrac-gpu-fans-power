#!/usr/bin/env python3
"""Prove a refactor changed no code: compare each Python file's AST at a git ref with the working tree.

Ignored on purpose (they can't change what runs): formatting and comments (not in the AST), docstrings, type annotations,
and `from __future__ import annotations`. Anything else that differs is printed.
   usage: python scripts/verify_refactor.py [ref=main] [--strict <path> ...]
   --strict: files that must be IDENTICAL apart from the ignored parts (the live controllers)
"""

import ast
import subprocess
import sys


def normalize(src: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
            for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs + [node.args.vararg, node.args.kwarg]:
                if a is not None:
                    a.annotation = None
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]

    class Ann(ast.NodeTransformer):
        def visit_AnnAssign(self, n):  # x: int = 1  ->  x = 1 ;  bare "x: int" -> removed
            if n.value is None:
                return None
            return ast.copy_location(ast.Assign(targets=[n.target], value=n.value), n)

    tree = ast.fix_missing_locations(Ann().visit(tree))
    return ast.dump(tree, include_attributes=False)


def main(argv):
    ref = argv[0] if argv and not argv[0].startswith("--") else "main"
    strict = set(argv[argv.index("--strict") + 1 :]) if "--strict" in argv else set()
    files = subprocess.run(
        ["git", "ls-files", "*.py", "services/taskrunner/taskctl"], capture_output=True, text=True
    ).stdout.split()
    bad = 0
    for f in files:
        old = subprocess.run(["git", "show", f"{ref}:{f}"], capture_output=True, text=True, encoding="utf-8")
        if old.returncode:
            print(f"new      {f}")
            continue
        same = normalize(old.stdout) == normalize(open(f, encoding="utf-8").read())
        tag = "same" if same else ("CHANGED" if f in strict else "changed")
        bad += (not same) and f in strict
        print(f"{tag:8} {f}")
    print(f"\n{len(strict)} strict file(s); {bad} changed" + ("" if not bad else "  <-- NOT allowed"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plain-text output helpers: banners, fixed-width tables, value formatting, diagnostics."""
from __future__ import annotations

import sys
from typing import Any, Iterable, Sequence

WIDTH = 78


def banner(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f" {title}")
    print("=" * WIDTH)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, WIDTH - len(title) - 5))


def fmt(v: Any) -> str:
    """Ints as-is; float-parsable values with 2 dp (or as an int when integral); else str."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return str(v)
    if f.is_integer():
        return str(int(f))
    return f"{f:.2f}"


def print_table(header: Sequence[str], rows: Iterable[Any]) -> None:
    """Rows may be dicts (keyed by header) or sequences (in header order)."""
    header = list(header)
    cells = []
    for r in rows:
        if isinstance(r, dict):
            cells.append([fmt(r.get(h)) for h in header])
        else:
            cells.append([fmt(x) for x in r])
    widths = [len(h) for h in header]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(c.rjust(widths[i]) if _numeric(c) else c.ljust(widths[i]) for i, c in enumerate(row)))
    if not cells:
        print("(no rows)")


def _numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def info(msg: str) -> None:
    print(f"[INFO] {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)


def result_line(ok: bool, name: str, detail: str = "") -> str:
    tag = "PASS" if ok else "FAIL"
    text = f"  {tag}  {name}" + (f": {detail}" if detail else "")
    print(text, flush=True)
    return text

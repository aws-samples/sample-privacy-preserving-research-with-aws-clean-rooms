# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Local records under ``ROOT/.state`` (no secrets). IDs are always read live from the
stacks; these files are records, never the source of truth.

Files: outputs-a.json, outputs-b.json, memberships.json, query_log.jsonl,
last-teardown.json, log_groups.json.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlock_lib import ROOT

STATE_DIR = ROOT / ".state"


def path(name: str) -> Path:
    return STATE_DIR / name


def write_json(name: str, obj: Any) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = path(name)
    p.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")
    return p


def read_json(name: str, default: Any = None) -> Any:
    p = path(name)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def append_jsonl(name: str, obj: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path(name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def read_jsonl(name: str) -> list[dict]:
    p = path(name)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

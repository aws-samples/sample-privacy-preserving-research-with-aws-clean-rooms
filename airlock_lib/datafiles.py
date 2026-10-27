# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validate the Parquet layout under ``data.files_dir`` against the configured tables.

Layout: ``<files_dir>/<table.s3_prefix><file>.parquet``. Each table directory holds one or
more ``*.parquet`` files and nothing else (hidden files are ignored and never uploaded).
Each file's column names, order and types must match ``tables[*].columns``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from airlock_lib import ROOT
from airlock_lib.config import GENERATED_DIR, Config, ConfigError

GENERATE_HINT = "run .venv/bin/python -m datagen.generate"


def load_manifest(cfg: Config, root: Path = ROOT) -> dict | None:
    """The generator's A-side ground truth, or None (files mode, or not generated yet)."""
    if cfg.data.source != "generator":
        return None
    p = Path(root) / GENERATED_DIR / "local" / "manifest.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _glue_type(t: pa.DataType) -> str | None:
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    if pa.types.is_int32(t):
        return "int"
    if pa.types.is_int64(t):
        return "bigint"
    if pa.types.is_float64(t):
        return "double"
    if pa.types.is_date32(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamp"
    if pa.types.is_boolean(t):
        return "boolean"
    return None


def _hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def _err(path: Path, reason: str) -> ConfigError:
    return ConfigError(f"data.files_dir: {path}: {reason}")


def validate(cfg: Config, root: Path = ROOT) -> dict[str, list[Path]]:
    """Return ``{table_key: [parquet paths]}`` or raise ConfigError."""
    files_dir = (Path(root) / cfg.data.files_dir).resolve()
    hint = f"; {GENERATE_HINT}" if cfg.data.source == "generator" else ""
    if not files_dir.is_dir():
        raise _err(files_dir, f"directory not found{hint}")

    result: dict[str, list[Path]] = {}
    table_dirs = {}
    for t in cfg.tables:
        tdir = files_dir / t.s3_prefix.rstrip("/")
        table_dirs[tdir] = t
        if not tdir.is_dir():
            raise _err(tdir, f"table directory for tables.{t.key} not found{hint}")
        parquet = []
        for entry in sorted(tdir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                raise _err(entry, "unexpected sub-directory (put Parquet files directly in the table directory)")
            if entry.suffix.lower() != ".parquet":
                raise _err(entry, "only *.parquet files are allowed in a table directory")
            parquet.append(entry)
        if not parquet:
            raise _err(tdir, f"no *.parquet files for tables.{t.key}{hint}")
        expected = [(c.name, c.type) for c in t.columns]
        for f in parquet:
            try:
                schema = pq.read_schema(f)
            except Exception as exc:  # pyarrow raises several error types for bad files
                raise _err(f, f"not a readable Parquet file ({exc})") from exc
            names = list(schema.names)
            if names != [n for n, _ in expected]:
                raise _err(f, f"columns {names} do not match tables.{t.key}.columns "
                              f"{[n for n, _ in expected]} (names and order)")
            for field, (name, glue_type) in zip(schema, expected):
                actual = _glue_type(field.type)
                if actual != glue_type:
                    raise _err(f, f"column {name!r} has Parquet type {field.type}, "
                                  f"incompatible with Glue type {glue_type!r}")
        result[t.key] = parquet

    for f in sorted(files_dir.rglob("*")):
        rel = f.relative_to(files_dir)
        if f.is_dir() or _hidden(rel):
            continue
        if not any(d == f.parent for d in table_dirs):
            raise _err(f, "file is outside every table prefix (it would be uploaded but never queried)")
    return result

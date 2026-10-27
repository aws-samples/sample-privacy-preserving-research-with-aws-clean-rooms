# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Seeded synthetic data generator for Airlock (RR2, C1).

Usage: .venv/bin/python -m datagen.generate [--config PATH]

Every value comes from one ``random.Random(seed)`` instance; there is no other entropy
source and no external input, so the output is synthetic and byte-for-byte reproducible.
Only ``<root>/data/generated/`` is deleted and recreated.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from airlock_lib import ROOT
from airlock_lib.config import GENERATED_DIR, Config, ConfigError, load_config

# Arrow type per generated column, and the Glue type it must be declared as in the config.
PATIENT_SCHEMA = pa.schema([
    ("patient_id", pa.string()),
    ("age", pa.int32()),
    ("sex", pa.string()),
    ("region", pa.string()),
    ("primary_diagnosis", pa.string()),
    ("baseline_hba1c", pa.float64()),
    ("enrollment_date", pa.date32()),
])
OUTCOME_SCHEMA = pa.schema([
    ("patient_id", pa.string()),
    ("medication_class", pa.string()),
    ("procedure", pa.string()),
    ("followup_hba1c", pa.float64()),
    ("followup_date", pa.date32()),
    ("outcome_flag", pa.string()),
])
ARROW_TO_GLUE = {pa.string(): "string", pa.int32(): "int", pa.float64(): "double", pa.date32(): "date"}
GROUP_COLUMN = "medication_class"


class GenerationError(Exception):
    """A fatal self-check failed."""


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _pick(rng: random.Random, weights: dict) -> str:
    names = list(weights)
    return rng.choices(names, weights=[weights[n] for n in names], k=1)[0]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_schema(cfg: Config, key: str, schema: pa.Schema) -> None:
    try:
        table = cfg.table(key)
    except KeyError as exc:
        raise GenerationError(f"config has no table with key {key!r}; the generator writes "
                              "'patients' and 'outcomes'") from exc
    expected = [(c.name, c.type) for c in table.columns]
    actual = [(f.name, ARROW_TO_GLUE[f.type]) for f in schema]
    if expected != actual:
        raise GenerationError(f"tables.{key}.columns {expected} do not match the generated columns {actual}")


def build_rows(cfg: Config) -> tuple[dict, dict]:
    """Return column-oriented dicts for patients and outcomes."""
    g = cfg.data.generator
    rng = random.Random(g.seed)
    ids = [f"{g.id_prefix}{i:06d}" for i in range(1, g.patient_count + 1)]

    classes = [m for m in g.medication_classes for _ in range(m.count)]
    rng.shuffle(classes)

    pat = {f.name: [] for f in PATIENT_SCHEMA}
    out = {f.name: [] for f in OUTCOME_SCHEMA}
    for pid, mc in zip(ids, classes):
        age = int(_clamp(round(rng.gauss(g.age.mean, g.age.sd)), g.age.min, g.age.max))
        sex = _pick(rng, g.sex_weights)
        region = _pick(rng, g.region_weights)
        diagnosis = _pick(rng, g.diagnosis_weights)
        procedure = _pick(rng, g.procedure_weights)
        enrollment = g.enrollment_start + dt.timedelta(days=rng.randint(0, g.enrollment_days - 1))
        b = g.baseline_hba1c
        baseline = round(_clamp(rng.gauss(b.mean, b.sd), b.min, b.max), 1)
        followup = round(_clamp(baseline + rng.gauss(mc.mean_change, mc.sd_change),
                                g.followup_clip_min, g.followup_clip_max), 1)
        followup_date = enrollment + dt.timedelta(days=rng.randint(g.followup_days_min, g.followup_days_max))
        delta = round(followup - baseline, 1)
        if delta <= g.improved_max_delta:
            flag = "improved"
        elif delta >= g.worsened_min_delta:
            flag = "worsened"
        else:
            flag = "stable"

        for k, v in (("patient_id", pid), ("age", age), ("sex", sex), ("region", region),
                     ("primary_diagnosis", diagnosis), ("baseline_hba1c", baseline),
                     ("enrollment_date", enrollment)):
            pat[k].append(v)
        for k, v in (("patient_id", pid), ("medication_class", mc.name), ("procedure", procedure),
                     ("followup_hba1c", followup), ("followup_date", followup_date), ("outcome_flag", flag)):
            out[k].append(v)
    return pat, out


def _self_checks(cfg: Config, pat: dict, out: dict) -> None:
    g = cfg.data.generator
    _check_schema(cfg, "patients", PATIENT_SCHEMA)
    _check_schema(cfg, "outcomes", OUTCOME_SCHEMA)
    if any(a < 18 for a in pat["age"]):
        raise GenerationError("age < 18 found (adults only)")
    if len(set(pat["patient_id"])) != len(pat["patient_id"]):
        raise GenerationError("patient_id values are not unique")
    if not set(out["patient_id"]) <= set(pat["patient_id"]):
        raise GenerationError("outcomes contain a patient_id that is not in patients")
    if sum(m.count for m in g.medication_classes) != g.patient_count:
        raise GenerationError("medication class counts do not sum to patient_count")


def _class_stats(cfg: Config, pat: dict, out: dict) -> dict:
    baseline_by_id = dict(zip(pat["patient_id"], pat["baseline_hba1c"]))
    stats = {}
    for m in cfg.data.generator.medication_classes:
        rows = [i for i, c in enumerate(out["medication_class"]) if c == m.name]
        base = [baseline_by_id[out["patient_id"][i]] for i in rows]
        foll = [out["followup_hba1c"][i] for i in rows]
        flags = {"improved": 0, "stable": 0, "worsened": 0}
        for i in rows:
            flags[out["outcome_flag"][i]] += 1
        mean_b = sum(base) / len(base)
        mean_f = sum(foll) / len(foll)
        stats[m.name] = {"n": len(rows), "mean_baseline": mean_b, "mean_followup": mean_f,
                         "mean_change": mean_f - mean_b, "outcome_flag_counts": flags}
    return stats


def _write_csv(path: Path, cols: dict) -> None:
    names = list(cols)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(names)
        for row in zip(*(cols[n] for n in names)):
            w.writerow([v.isoformat() if isinstance(v, dt.date) else v for v in row])


def _story_warnings(cfg: Config, stats: dict) -> list[str]:
    n_min = cfg.privacy.min_distinct_patients
    warnings = []
    if not any(s["n"] < n_min for s in stats.values()):
        warnings.append(f"no class below threshold {n_min}; the suppression demo (blocked 03) "
                        "will show a visible group")
    for name, s in stats.items():
        if n_min <= s["n"] < 2 * n_min:
            warnings.append(f"class {name} ({s['n']}) is below 2x threshold; results near the suppression boundary")
    return warnings


def generate(cfg: Config, root: Path = ROOT) -> dict:
    """Generate the synthetic dataset under ``root`` and return the manifest."""
    if cfg.data.source != "generator":
        print(f"[ERROR] data.source=files: generator disabled; your files in {cfg.data.files_dir} "
              "are used as-is", file=sys.stderr)
        raise SystemExit(1)
    root = Path(root).resolve()
    generated = (root / GENERATED_DIR).resolve()
    files_dir = (root / cfg.data.files_dir).resolve()
    if generated.parts[-2:] != ("data", "generated") or root not in generated.parents:
        raise GenerationError(f"refusing to delete unexpected directory {generated}")
    if generated not in files_dir.parents:
        raise GenerationError(f"data.files_dir {files_dir} must be under {generated}")

    pat, out = build_rows(cfg)
    _self_checks(cfg, pat, out)

    if generated.exists():
        shutil.rmtree(generated)
    local = generated / "local"
    local.mkdir(parents=True)

    files = {}
    for key, schema, cols in (("patients", PATIENT_SCHEMA, pat), ("outcomes", OUTCOME_SCHEMA, out)):
        table_cfg = cfg.table(key)
        target_dir = files_dir / table_cfg.s3_prefix
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{key}.parquet"
        pq.write_table(pa.table(cols, schema=schema), target, compression="snappy")
        files[f"{table_cfg.s3_prefix}{key}.parquet"] = _sha256(target)
        _write_csv(local / f"{key}.csv", cols)

    stats = _class_stats(cfg, pat, out)
    manifest = {
        "synthetic": True,
        "generator": "datagen.generate",
        "seed": cfg.data.generator.seed,
        "row_counts": {"patients": len(pat["patient_id"]), "outcomes": len(out["patient_id"])},
        "group_column": GROUP_COLUMN,
        "classes": stats,
        "files": files,
    }
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m datagen.generate", description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        manifest = generate(cfg)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except GenerationError as exc:
        print(f"[ERROR] generator self-check failed: {exc}", file=sys.stderr)
        return 1

    n_min = cfg.privacy.min_distinct_patients
    print(f"Generated synthetic data (seed {manifest['seed']}): "
          f"patients={manifest['row_counts']['patients']} outcomes={manifest['row_counts']['outcomes']}")
    print(f"Per-class counts ({GROUP_COLUMN}; threshold {n_min} distinct {cfg.privacy.identity_column}):")
    for name, s in manifest["classes"].items():
        note = "  <- below threshold, suppressed in results" if s["n"] < n_min else ""
        print(f"  {name:<14} n={s['n']:>5}  mean_change={s['mean_change']:+.2f}{note}")
    for rel, digest in manifest["files"].items():
        print(f"  {cfg.data.files_dir}/{rel}  sha256={digest}")
    print(f"  preview + manifest: {GENERATED_DIR}/local/")
    for w in _story_warnings(cfg, manifest["classes"]):
        print(f"[WARN] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

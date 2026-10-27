# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from airlock_lib.config import load_config_dict
from datagen.generate import generate


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def gen(cfg, tmp_path):
    manifest = generate(cfg, root=tmp_path)
    s3 = tmp_path / cfg.data.files_dir
    return manifest, pq.read_table(s3 / "patients" / "patients.parquet"), pq.read_table(
        s3 / "outcomes" / "outcomes.parquet"), tmp_path


def test_deterministic(cfg, tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    m1, m2 = generate(cfg, root=r1), generate(cfg, root=r2)
    assert m1 == m2
    for rel in m1["files"]:
        assert _sha(r1 / cfg.data.files_dir / rel) == _sha(r2 / cfg.data.files_dir / rel)
    assert (r1 / "data/generated/local/manifest.json").read_bytes() == \
        (r2 / "data/generated/local/manifest.json").read_bytes()


def test_rows_and_class_counts(cfg, gen):
    manifest, pat, out, _ = gen
    assert pat.num_rows == 3000 and out.num_rows == 3000
    counts = {m.name: m.count for m in cfg.data.generator.medication_classes}
    assert {k: v["n"] for k, v in manifest["classes"].items()} == counts
    assert counts["Meglitinide"] == 12
    from collections import Counter
    assert dict(Counter(out.column("medication_class").to_pylist())) == counts


def test_invariants(cfg, gen):
    _, pat, out, _ = gen
    ids = pat.column("patient_id").to_pylist()
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("SYN-") for i in ids)
    assert set(out.column("patient_id").to_pylist()) <= set(ids)
    assert min(pat.column("age").to_pylist()) >= 18


def test_arrow_types_and_column_order(cfg, gen):
    _, pat, out, _ = gen
    assert pat.schema.names == list(cfg.table("patients").column_names)
    assert out.schema.names == list(cfg.table("outcomes").column_names)
    assert pat.schema.field("patient_id").type == pa.string()
    assert pat.schema.field("age").type == pa.int32()
    assert pat.schema.field("baseline_hba1c").type == pa.float64()
    assert pat.schema.field("enrollment_date").type == pa.date32()
    assert out.schema.field("followup_hba1c").type == pa.float64()
    assert out.schema.field("followup_date").type == pa.date32()
    assert out.schema.field("outcome_flag").type == pa.string()


def test_outcome_flag_consistent_with_delta(cfg, gen):
    _, pat, out, _ = gen
    g = cfg.data.generator
    base = dict(zip(pat.column("patient_id").to_pylist(), pat.column("baseline_hba1c").to_pylist()))
    for pid, fu, flag in zip(out.column("patient_id").to_pylist(), out.column("followup_hba1c").to_pylist(),
                             out.column("outcome_flag").to_pylist()):
        delta = round(fu - base[pid], 1)
        want = "improved" if delta <= g.improved_max_delta else ("worsened" if delta >= g.worsened_min_delta
                                                                 else "stable")
        assert flag == want


def test_manifest_has_no_timestamps(gen):
    _, _, _, root = gen
    text = (root / "data/generated/local/manifest.json").read_text()
    m = json.loads(text)
    assert m["group_column"] == "medication_class"
    assert not any(k in text.lower() for k in ("timestamp", "generated_at", "created"))


def test_files_mode_refuses(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["data"].update(source="files", files_dir="data/customer/s3")
    c2 = load_config_dict(raw)
    with pytest.raises(SystemExit) as ei:
        generate(c2, root=tmp_path)
    assert ei.value.code == 1


def test_only_generated_dir_is_replaced(cfg, tmp_path):
    keep = tmp_path / "data" / "customer" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("customer file")
    stale = tmp_path / "data" / "generated" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    generate(cfg, root=tmp_path)
    assert keep.read_text() == "customer file"
    assert not stale.exists()

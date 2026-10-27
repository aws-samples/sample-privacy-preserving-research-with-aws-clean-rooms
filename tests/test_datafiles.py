# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from airlock_lib.config import ConfigError, load_config_dict
from airlock_lib.datafiles import validate
from datagen.generate import generate


@pytest.fixture()
def root(cfg, tmp_path):
    generate(cfg, root=tmp_path)
    return tmp_path


def _s3(cfg, root):
    return root / cfg.data.files_dir


def test_generated_layout_passes(cfg, root):
    result = validate(cfg, root=root)
    assert set(result) == {"patients", "outcomes"}
    assert all(len(v) == 1 for v in result.values())


def test_missing_table_dir(cfg, root):
    import shutil
    shutil.rmtree(_s3(cfg, root) / "outcomes")
    with pytest.raises(ConfigError, match="tables.outcomes not found"):
        validate(cfg, root=root)


def test_extra_non_parquet_file(cfg, root):
    (_s3(cfg, root) / "patients" / "notes.csv").write_text("x")
    with pytest.raises(ConfigError, match=r"only \*\.parquet"):
        validate(cfg, root=root)


def test_hidden_file_ignored(cfg, root):
    (_s3(cfg, root) / "patients" / ".DS_Store").write_text("x")
    (_s3(cfg, root) / ".hidden").write_text("x")
    validate(cfg, root=root)


def test_wrong_column_order(cfg, root):
    p = _s3(cfg, root) / "outcomes" / "outcomes.parquet"
    t = pq.read_table(p)
    pq.write_table(t.select(list(reversed(t.column_names))), p)
    with pytest.raises(ConfigError, match="names and order"):
        validate(cfg, root=root)


def test_wrong_type(cfg, root):
    p = _s3(cfg, root) / "patients" / "patients.parquet"
    t = pq.read_table(p)
    i = t.column_names.index("age")
    t = t.set_column(i, "age", t.column("age").cast(pa.float64()))
    pq.write_table(t, p)
    with pytest.raises(ConfigError, match="'age' has Parquet type double"):
        validate(cfg, root=root)


def test_stray_file_outside_table_prefixes(cfg, root):
    (_s3(cfg, root) / "readme.txt").write_text("x")
    with pytest.raises(ConfigError, match="outside every table prefix"):
        validate(cfg, root=root)


def test_generator_mode_missing_files_hint(cfg, tmp_path):
    with pytest.raises(ConfigError, match=r"datagen\.generate"):
        validate(cfg, root=tmp_path)


def test_files_mode_missing_has_no_generator_hint(cfg, tmp_path):
    raw = copy.deepcopy(cfg.raw)
    raw["data"].update(source="files", files_dir="data/customer/s3")
    c2 = load_config_dict(raw)
    with pytest.raises(ConfigError) as ei:
        validate(c2, root=tmp_path)
    assert "datagen" not in str(ei.value)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import shutil

import pytest

from airlock_lib import ROOT
from airlock_lib.config import ConfigError, load_config, load_config_dict


def test_default_config_derived_values(cfg):
    assert cfg.data_bucket_name == "airlock-a-data-111122223333-us-west-2"
    assert cfg.results_bucket_name == "airlock-b-results-444455556666-us-west-2"
    assert cfg.payer_flags() == {"data_owner": False, "analyst": True}
    assert cfg.member("data_owner").profile == "airlock-a"
    assert cfg.member("analyst").profile == "airlock-b"
    assert cfg.table("outcomes").configured_table_name == "airlock-outcomes"
    assert cfg.privacy.min_distinct_patients == 30
    assert cfg.collaboration.creator_member_abilities == ()
    assert cfg.query.compute.worker_type == "CR.1X"
    assert cfg.approved_query_path.is_file()
    assert cfg.blocked_query_path("select_patient_id").is_file()


def _t(raw, key):
    return next(t for t in raw["tables"] if t["key"] == key)


MUTATIONS = [
    ("accounts.data_owner.account_id", lambda r: r["accounts"]["data_owner"].update(account_id="12345")),
    ("accounts.data_owner.account_id", lambda r: r["accounts"]["data_owner"].update(account_id=111122223333)),
    ("accounts.analyst.account_id", lambda r: r["accounts"]["analyst"].update(
        account_id=r["accounts"]["data_owner"]["account_id"])),
    ("accounts.analyst.profile", lambda r: r["accounts"]["analyst"].update(profile="")),
    ("region", lambda r: r.update(region="uswest2")),
    ("collaboration.description", lambda r: r["collaboration"].update(description="   ")),  # L4
    ("collaboration.description", lambda r: r["collaboration"].update(description="")),  # L4
    ("collaboration.description", lambda r: r["collaboration"].update(description="x" * 256)),
    ("tables.patients.description", lambda r: _t(r, "patients").update(description="x" * 256)),  # L4
    ("collaboration.name", lambda r: r["collaboration"].update(name="x" * 101)),
    ("storage.data_bucket_prefix", lambda r: r["storage"].update(data_bucket_prefix="Airlock_Data")),
    ("storage.result_format", lambda r: r["storage"].update(result_format="JSON")),
    ("storage.results_key_prefix", lambda r: r["storage"].update(results_key_prefix="bad prefix/")),
    ("glue.database_name", lambda r: r["glue"].update(database_name="Airlock-DB")),
    ("collaboration.analytics_engine", lambda r: r["collaboration"].update(analytics_engine="CLEAN_ROOMS_SQL")),
    ("collaboration.creator_member_abilities",
     lambda r: r["collaboration"].update(creator_member_abilities=["CAN_QUERY"])),
    ("collaboration.analyst_member_abilities",
     lambda r: r["collaboration"].update(analyst_member_abilities=["CAN_RECEIVE_RESULTS"])),
    ("collaboration.analyst_member_abilities", lambda r: r["collaboration"].update(analyst_member_abilities=[])),
    ("collaboration.query_compute_payer", lambda r: r["collaboration"].update(query_compute_payer="data_owner")),
    ("tables.patients.allowed_columns", lambda r: _t(r, "patients")["allowed_columns"].append("ssn")),
    ("tables.patients.analysis_rule.dimension_columns",
     lambda r: _t(r, "patients")["analysis_rule"]["dimension_columns"].append("age")),
    ("tables.outcomes.analysis_rule", lambda r: _t(r, "outcomes")["analysis_rule"]["dimension_columns"].append(
        "patient_id")),
    ("tables.outcomes.analysis_rule", lambda r: _t(r, "outcomes")["analysis_rule"]["aggregate_columns"].append(
        {"columns": ["medication_class"], "function": "COUNT"})),
    ("tables.outcomes.analysis_rule.aggregate_columns[0].function",
     lambda r: _t(r, "outcomes")["analysis_rule"]["aggregate_columns"][0].update(function="MEDIAN")),
    ("tables.outcomes.analysis_rule.scalar_functions",
     lambda r: _t(r, "outcomes")["analysis_rule"].update(scalar_functions=["NOT_A_FN"])),
    ("tables.outcomes.columns[1].type", lambda r: _t(r, "outcomes")["columns"][1].update(type="varchar")),
    ("tables.patients.s3_prefix", lambda r: _t(r, "patients").update(s3_prefix="patients")),
    ("tables.outcomes.s3_prefix", lambda r: _t(r, "outcomes").update(s3_prefix="patients/")),
    ("privacy.identity_column", lambda r: r["privacy"].update(identity_column="sex")),
    ("privacy.min_distinct_patients", lambda r: r["privacy"].update(min_distinct_patients=1)),
    ("privacy.min_distinct_patients", lambda r: r["privacy"].update(min_distinct_patients=100001)),
    ("query.compute.worker_type", lambda r: r["query"]["compute"].update(worker_type="CR.8X")),  # L12
    ("query.compute.worker_count", lambda r: r["query"]["compute"].update(worker_count=1)),
    ("query.compute.worker_count", lambda r: r["query"]["compute"].update(worker_count=129)),
    ("query.compute.worker_count", lambda r: r["query"]["compute"].update(worker_type="CR.4X", worker_count=33)),
    ("query.poll_interval_seconds", lambda r: r["query"].update(poll_interval_seconds=0)),
    ("query.timeout_seconds", lambda r: r["query"].update(timeout_seconds=30)),
    ("logging.warmup_seconds", lambda r: r["logging"].update(warmup_seconds=3601)),
    ("logging.log_group_wait_seconds", lambda r: r["logging"].update(log_group_wait_seconds=-1)),
    ("data.files_dir", lambda r: r["data"].update(source="files")),  # files mode under data/generated
    ("data.files_dir", lambda r: r["data"].update(files_dir="data/customer/s3")),  # generator mode outside
    ("data.files_dir", lambda r: r["data"].update(files_dir="/tmp/airlock-data")),
    ("data.files_dir", lambda r: r["data"].update(source="files", files_dir="../outside")),
    ("data.generator.medication_classes", lambda r: r["data"]["generator"]["medication_classes"][0].update(
        count=1151)),
    ("data.generator.sex_weights.M", lambda r: r["data"]["generator"]["sex_weights"].update(M=0)),
    ("data.generator.age.min", lambda r: r["data"]["generator"]["age"].update(min=17)),
    ("research.approved_query_file", lambda r: r["research"].update(approved_query_file="queries/missing.sql")),
    ("research.blocked_queries[0].expect", lambda r: r["research"]["blocked_queries"][0].update(expect="MAYBE")),
    ("research.derived_difference.name", lambda r: r["research"]["derived_difference"].update(name="mean change")),
    ("research.group_by_column", lambda r: r["research"].update(group_by_column="medication-class")),
    # L11: removed option is now an unknown key
    ("collaboration.allowed_result_receivers",
     lambda r: r["collaboration"].update(allowed_result_receivers=["analyst"])),
    ("privacy.typo", lambda r: r["privacy"].update(typo=1)),
    ("security", lambda r: r.pop("security")),
]


@pytest.mark.parametrize("path,mutate", MUTATIONS, ids=[f"{i}-{m[0]}" for i, m in enumerate(MUTATIONS)])
def test_invalid_config_rejected_with_dotted_path(raw, path, mutate):
    mutate(raw)
    with pytest.raises(ConfigError) as ei:
        load_config_dict(raw)
    assert str(ei.value).startswith(path), str(ei.value)


def test_worker_type_error_mentions_cr8x_exclusion(raw):
    raw["query"]["compute"]["worker_type"] = "CR.8X"
    with pytest.raises(ConfigError, match="CR.8X is deliberately excluded"):
        load_config_dict(raw)


def test_null_optionals_allowed(raw):
    raw["query"]["compute"] = None
    raw["research"]["derived_difference"] = None
    cfg = load_config_dict(raw)
    assert cfg.query.compute is None and cfg.research.derived_difference is None


def test_files_mode_outside_generated_is_valid(raw):
    raw["data"].update(source="files", files_dir="data/customer/s3", generator=None)
    cfg = load_config_dict(raw)
    assert cfg.data.source == "files"
    assert cfg.files_dir_path == ROOT / "data" / "customer" / "s3"


def test_paths_resolve_against_root_not_cwd(tmp_path, monkeypatch):
    """L9: a config copy outside ROOT, loaded from another cwd, still resolves under ROOT."""
    copy_path = tmp_path / "airlock.yaml"
    shutil.copy(ROOT / "config" / "airlock.yaml", copy_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(copy_path)
    assert cfg.approved_query_path == ROOT / "queries" / "approved" / "mean_hba1c_by_medication_class.sql"
    assert cfg.approved_query_path.is_file()
    for q in cfg.research.blocked_queries:
        p = cfg.blocked_query_path(q.id)
        assert ROOT in p.parents and p.is_file()
    assert cfg.files_dir_path == ROOT / "data" / "generated" / "s3"


def test_airlock_config_env_override(tmp_path, monkeypatch):
    import yaml

    raw = yaml.safe_load((ROOT / "config" / "airlock.yaml").read_text())
    raw["privacy"]["min_distinct_patients"] = 42
    p = tmp_path / "override.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.setenv("AIRLOCK_CONFIG", str(p))
    cfg = load_config()
    assert cfg.privacy.min_distinct_patients == 42
    assert cfg.path == p.resolve()

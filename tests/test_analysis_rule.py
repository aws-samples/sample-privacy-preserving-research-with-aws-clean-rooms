# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy

from airlock_lib.config import load_config_dict
from stacks.analysis_rule import aggregation_rule_dict, build_aggregation_rule

OUTCOMES_RULE = {
    "AggregateColumns": [
        {"ColumnNames": ["patient_id"], "Function": "COUNT_DISTINCT"},
        {"ColumnNames": ["patient_id"], "Function": "COUNT"},
        {"ColumnNames": ["followup_hba1c"], "Function": "AVG"},
    ],
    "JoinColumns": ["patient_id"],
    "DimensionColumns": ["medication_class", "outcome_flag"],
    "ScalarFunctions": [],
    "OutputConstraints": [{"ColumnName": "patient_id", "Minimum": 30, "Type": "COUNT_DISTINCT"}],
}


def test_outcomes_rule_matches_design(cfg):
    assert aggregation_rule_dict(cfg.table("outcomes"), cfg.privacy) == OUTCOMES_RULE


def test_patients_rule(cfg):
    rule = aggregation_rule_dict(cfg.table("patients"), cfg.privacy)
    assert rule["AggregateColumns"][2] == {"ColumnNames": ["baseline_hba1c"], "Function": "AVG"}
    assert rule["DimensionColumns"] == ["sex", "region", "primary_diagnosis"]
    assert rule["JoinColumns"] == ["patient_id"]
    assert rule["ScalarFunctions"] == []
    assert rule["OutputConstraints"] == OUTCOMES_RULE["OutputConstraints"]


def test_threshold_flows_into_output_constraint(cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["privacy"]["min_distinct_patients"] = 50
    c2 = load_config_dict(raw)
    for t in c2.tables:
        assert aggregation_rule_dict(t, c2.privacy)["OutputConstraints"][0]["Minimum"] == 50


def test_build_property_object(cfg):
    prop = build_aggregation_rule(cfg.table("outcomes"), cfg.privacy)
    assert prop.join_columns == ["patient_id"]
    assert prop.scalar_functions == []
    assert prop.output_constraints[0].minimum == 30

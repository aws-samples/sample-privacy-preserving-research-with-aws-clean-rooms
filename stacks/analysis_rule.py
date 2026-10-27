# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure builder for the Clean Rooms aggregation analysis rule (FR4, AC2).

The rule is derived only from the config: the table's analysis_rule block plus the
privacy block (identity column and minimum distinct count). Changing the threshold in
config/airlock.yaml changes the synthesized rule without any code change (AC7).
"""
from __future__ import annotations

from aws_cdk import aws_cleanrooms as cleanrooms

from airlock_lib.config import Privacy, Table


def aggregation_rule_dict(table: Table, privacy: Privacy) -> dict:
    """CloudFormation (PascalCase) shape of ``Policy.V1.Aggregation`` for one table."""
    rule = table.analysis_rule
    return {
        "AggregateColumns": [{"ColumnNames": list(a.columns), "Function": a.function}
                             for a in rule.aggregate_columns],
        "JoinColumns": list(rule.join_columns),
        "DimensionColumns": list(rule.dimension_columns),
        "ScalarFunctions": list(rule.scalar_functions),
        "OutputConstraints": [{
            "ColumnName": privacy.identity_column,
            "Minimum": privacy.min_distinct_patients,
            "Type": "COUNT_DISTINCT",
        }],
    }


def build_aggregation_rule(table: Table, privacy: Privacy) -> cleanrooms.CfnConfiguredTable.AnalysisRuleAggregationProperty:
    d = aggregation_rule_dict(table, privacy)
    ct = cleanrooms.CfnConfiguredTable
    return ct.AnalysisRuleAggregationProperty(
        aggregate_columns=[ct.AggregateColumnProperty(column_names=a["ColumnNames"], function=a["Function"])
                           for a in d["AggregateColumns"]],
        join_columns=d["JoinColumns"],
        dimension_columns=d["DimensionColumns"],
        scalar_functions=d["ScalarFunctions"],
        output_constraints=[ct.AggregationConstraintProperty(column_name=c["ColumnName"], minimum=c["Minimum"],
                                                             type=c["Type"])
                            for c in d["OutputConstraints"]],
    )

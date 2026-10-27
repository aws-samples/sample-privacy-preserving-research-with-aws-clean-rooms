# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest
from aws_cdk import App
from aws_cdk.assertions import Template

from stacks import build_app

RUNNER = "11111111-2222-4333-8444-555555555555"
PROJECT_TAG = {"Key": "Project", "Value": "Airlock"}
EXCLUDED_PREFIXES = ("Custom", "AWS679f53fac002430cb0da5b7982bd2287")
TAGGED_TYPES = {"AWS::CleanRooms::Collaboration", "AWS::CleanRooms::Membership", "AWS::CleanRooms::ConfiguredTable",
                "AWS::CleanRooms::ConfiguredTableAssociation", "AWS::S3::Bucket"}


@pytest.fixture(scope="module")
def built(cfg, generated_root_data, tmp_path_factory):
    app = App(outdir=str(tmp_path_factory.mktemp("cdk-p1")))
    owner, analyst = build_app(cfg, app, None)
    return app, owner, analyst, Template.from_stack(owner).to_json(), Template.from_stack(analyst).to_json()


@pytest.fixture(scope="module")
def built_p2(cfg, generated_root_data, tmp_path_factory):
    app = App(outdir=str(tmp_path_factory.mktemp("cdk-p2")))
    owner, _ = build_app(cfg, app, RUNNER)
    return owner, Template.from_stack(owner).to_json()


def _of_type(t, rtype):
    return {k: v for k, v in t["Resources"].items() if v["Type"] == rtype}


def _one(t, rtype):
    items = _of_type(t, rtype)
    assert len(items) == 1, items.keys()
    return next(iter(items.items()))


def test_collaboration(built, cfg):
    _, _, _, a, _ = built
    _, c = _one(a, "AWS::CleanRooms::Collaboration")
    p = c["Properties"]
    assert p["CreatorMemberAbilities"] == []
    assert p["CreatorPaymentConfiguration"] == {"QueryCompute": {"IsResponsible": False}}
    assert p["Members"] == [{
        "AccountId": cfg.accounts.analyst.account_id,
        "DisplayName": cfg.accounts.analyst.display_name,
        "MemberAbilities": ["CAN_QUERY", "CAN_RECEIVE_RESULTS"],
        "PaymentConfiguration": {"QueryCompute": {"IsResponsible": True}},
    }]
    assert p["QueryLogStatus"] == "ENABLED"
    assert p["AnalyticsEngine"] == "SPARK"
    assert p["CreatorDisplayName"] == cfg.accounts.data_owner.display_name


def test_configured_tables(built, cfg):
    _, _, _, a, _ = built
    tables = _of_type(a, "AWS::CleanRooms::ConfiguredTable")
    assert len(tables) == 2
    by_name = {v["Properties"]["Name"]: (k, v) for k, v in tables.items()}
    for t in cfg.tables:
        lid, res = by_name[t.configured_table_name]
        p = res["Properties"]
        agg = p["AnalysisRules"][0]["Policy"]["V1"]["Aggregation"]
        assert p["AnalysisRules"][0]["Type"] == "AGGREGATION"
        assert agg["ScalarFunctions"] == []
        assert agg["OutputConstraints"] == [{"ColumnName": "patient_id", "Minimum": 30, "Type": "COUNT_DISTINCT"}]
        assert p["AllowedColumns"] == list(t.allowed_columns)
        assert p["AnalysisMethod"] == "DIRECT_QUERY"
        assert f"GlueTable{t.construct_suffix}" in res.get("DependsOn", [])


def test_associations_and_memberships(built, cfg):
    _, _, _, a, b = built
    assocs = _of_type(a, "AWS::CleanRooms::ConfiguredTableAssociation")
    assert sorted(v["Properties"]["Name"] for v in assocs.values()) == sorted(t.association_name for t in cfg.tables)
    for v in assocs.values():
        assert "ConfiguredTableAssociationAnalysisRules" not in v["Properties"]  # L11
    _, am = _one(a, "AWS::CleanRooms::Membership")
    assert am["DeletionPolicy"] == "Retain"
    assert "PaymentConfiguration" not in am["Properties"]
    assert "DefaultResultConfiguration" not in am["Properties"]
    _, bm = _one(b, "AWS::CleanRooms::Membership")
    bp = bm["Properties"]
    assert bp["PaymentConfiguration"] == {"QueryCompute": {"IsResponsible": True}}
    assert bp["QueryLogStatus"] == "ENABLED"
    s3 = bp["DefaultResultConfiguration"]["OutputConfiguration"]["S3"]
    assert s3["ResultFormat"] == "CSV" and s3["KeyPrefix"] == "results/" and s3["SingleFileOutput"] is True
    assert "RoleArn" not in bp["DefaultResultConfiguration"]
    assert bp["CollaborationIdentifier"] == {"Ref": "CollaborationId"}


def test_b_parameter_and_outputs(built):
    _, _, _, a, b = built
    assert b["Parameters"]["CollaborationId"]["AllowedPattern"] == \
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    assert "Default" not in b["Parameters"]["CollaborationId"]
    assert {"AnalystMembershipId", "AnalystMembershipArn", "ResultsBucketName", "JoinedCollaborationId"} <= \
        set(b["Outputs"])
    assert {"CollaborationId", "CollaborationArn", "DataOwnerMembershipId", "DataOwnerMembershipArn",
            "DataBucketName", "GlueDatabaseName", "ConfiguredTableIds", "DataAccessRoleArn", "TrustPhase"} <= \
        set(a["Outputs"])


def _trust(owner, t):
    lid = owner.get_logical_id(owner.data_role.node.default_child)
    return t["Resources"][lid]["Properties"]["AssumeRolePolicyDocument"]["Statement"]


def test_trust_phase1(built, cfg):
    _, owner, _, a, _ = built
    st = {s["Sid"]: s for s in _trust(owner, a)}
    src = st["AllowIfSourceArnMatches"]["Condition"]["ForAnyValue:ArnLike"]["aws:SourceArn"]
    assert f"arn:aws:cleanrooms:{cfg.region}:{cfg.accounts.analyst.account_id}:membership/*" in src
    assert st["AllowIfExternalIdMatches"]["Condition"]["StringLike"]["sts:ExternalId"] == \
        f"arn:aws:*:{cfg.region}:*:dbuser:*/*"
    assert a["Outputs"]["TrustPhase"]["Value"] == "1"


def test_trust_phase2(built_p2, cfg):
    owner, a = built_p2
    st = {s["Sid"]: s for s in _trust(owner, a)}
    src = st["AllowIfSourceArnMatches"]["Condition"]["ForAnyValue:ArnEquals"]["aws:SourceArn"]
    assert f"arn:aws:cleanrooms:{cfg.region}:{cfg.accounts.analyst.account_id}:membership/{RUNNER}" in src
    assert st["AllowIfExternalIdMatches"]["Condition"]["StringLike"]["sts:ExternalId"] == \
        f"arn:aws:*:{cfg.region}:*:dbuser:*/{RUNNER}*"
    assert a["Outputs"]["TrustPhase"]["Value"] == "2"


def test_resource_tags(built):
    """M1: per-resource tags on Clean Rooms, buckets and the data-access role only."""
    _, owner, _, a, b = built
    role_lid = owner.get_logical_id(owner.data_role.node.default_child)
    checked = 0
    for t in (a, b):
        for lid, res in t["Resources"].items():
            if lid.startswith(EXCLUDED_PREFIXES):
                continue
            if res["Type"] in TAGGED_TYPES or lid == role_lid:
                assert PROJECT_TAG in res["Properties"].get("Tags", []), lid
                checked += 1
    # 1 collaboration + 2 memberships + 2 tables + 2 associations + 2 buckets + 1 role
    assert checked == 10


def test_stack_level_tags(built, cfg):
    app, _, _, _, _ = built
    assembly = app.synth()
    for name in (cfg.accounts.data_owner.stack_name, cfg.accounts.analyst.stack_name):
        assert assembly.get_stack_by_name(name).tags == cfg.project.tags


def test_b_membership_created_last(built):
    """L2: B's membership depends on the bucket, its policy and the auto-delete resource."""
    _, _, _, _, b = built
    _, bm = _one(b, "AWS::CleanRooms::Membership")
    auto = [k for k, v in b["Resources"].items() if v["Type"] == "Custom::S3AutoDeleteObjects"]
    assert auto and auto[0] in bm.get("DependsOn", [])
    bucket = next(k for k, v in b["Resources"].items() if v["Type"] == "AWS::S3::Bucket")
    assert bucket in bm["DependsOn"]


def test_data_bucket_hardening(built, cfg):
    _, _, _, a, _ = built
    _, bucket = _one(a, "AWS::S3::Bucket")
    p = bucket["Properties"]
    assert p["BucketName"] == cfg.data_bucket_name
    assert p["PublicAccessBlockConfiguration"] == {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                                   "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
    assert p["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    policy = next(v for v in a["Resources"].values() if v["Type"] == "AWS::S3::BucketPolicy")
    principals = [s.get("Principal") for s in policy["Properties"]["PolicyDocument"]["Statement"]]
    assert all(cfg.accounts.analyst.account_id not in str(pr) for pr in principals)

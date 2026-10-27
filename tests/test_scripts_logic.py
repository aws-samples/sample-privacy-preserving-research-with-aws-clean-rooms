# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from types import SimpleNamespace

import pytest

from airlock_lib.aws import _echo_api
from airlock_lib.cleanrooms import QueryOutcome
from airlock_lib.deployment import StackInfo
from scripts.ac7_config_diff import REPLACEMENT, SetError, apply_set, diff_templates
from scripts.demo_allowed import check_against_manifest
from scripts.demo_blocked import blocked_verdict, control_ok
from scripts.deploy import decide_action, retry_delay

COLLAB = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def A(status, collab=COLLAB):
    return StackInfo("AirlockDataOwner", status, {"CollaborationId": collab}, {}, "arn:a")


def B(status, collab=COLLAB):
    return StackInfo("AirlockAnalyst", status, {"AnalystMembershipId": "m"}, {"CollaborationId": collab}, "arn:b")


# --------------------------------------------------------------------------- stack-state guard
@pytest.mark.parametrize("a,b,kind,msg", [
    (None, None, "fresh", ""),
    (A("CREATE_COMPLETE"), None, "deploy_b", ""),
    (A("UPDATE_COMPLETE"), None, "deploy_b", ""),
    (A("UPDATE_ROLLBACK_COMPLETE"), None, "deploy_b", ""),
    (A("UPDATE_COMPLETE"), B("CREATE_COMPLETE"), "update_all", ""),
    (A("UPDATE_COMPLETE"), B("UPDATE_COMPLETE", collab="other"), "exit", "different collaboration"),
    (None, B("CREATE_COMPLETE"), "exit", "B stack exists without A"),
    (A("DELETE_FAILED"), B("CREATE_COMPLETE"), "exit", "B stack exists without A"),
    (A("ROLLBACK_FAILED"), B("CREATE_COMPLETE"), "exit", "B stack exists without A"),
    (A("UPDATE_IN_PROGRESS"), None, "exit", "in progress"),
    (A("CREATE_COMPLETE"), B("DELETE_IN_PROGRESS"), "exit", "in progress"),
])
def test_decide_action_table(a, b, kind, msg):
    act = decide_action(a, b)
    assert act.kind == kind
    assert msg in act.message
    assert not act.catch_all


@pytest.mark.parametrize("a,b", [
    (A("ROLLBACK_FAILED"), None),
    (A("UPDATE_COMPLETE"), B("UPDATE_ROLLBACK_FAILED")),
    (A("UPDATE_ROLLBACK_FAILED"), None),
    (A("DELETE_FAILED"), None),
    (A("IMPORT_COMPLETE"), None),
])
def test_decide_action_catch_all(a, b):
    act = decide_action(a, b)
    assert act.kind == "exit" and act.catch_all
    assert "run teardown" in act.message and "then deploy" in act.message


def test_retry_delay():
    assert retry_delay(["A conflicting conditional operation is currently in progress (OperationAborted)"]) == 60
    assert retry_delay(["User is not authorized to perform sts:AssumeRole on resource"]) == 30
    assert retry_delay(["Unable to assume the role"]) == 30
    assert retry_delay(["Bucket name already exists"]) is None
    assert retry_delay([]) is None


# --------------------------------------------------------------------------- blocked verdicts
def O(status, rows=(), code=None, msg=None):
    return QueryOutcome(label="x", status=status, rows=list(rows), error_code=code, error_message=msg)


@pytest.mark.parametrize("expect,outcome,ok", [
    ("REJECTED", O("REJECTED", code="ValidationException", msg="Column(s) [patient_id] is not allowed"), True),
    ("REJECTED", O("SUCCESS", rows=[{"a": 1}]), False),
    ("REJECTED", O("SUCCESS"), False),
    ("NO_ROWS", O("SUCCESS"), True),
    ("NO_ROWS", O("REJECTED"), True),
    ("NO_ROWS", O("SUCCESS", rows=[{"a": 1}]), False),
    ("REJECTED", O("FAILED", code="X", msg="internal"), False),
    ("NO_ROWS", O("ERROR", code="AccessDenied"), False),
    ("NO_ROWS", O("TIMEOUT"), False),
    ("REJECTED", O("CANCELLED"), False),
])
def test_blocked_verdict(expect, outcome, ok):
    got, text = blocked_verdict(expect, outcome)
    assert got is ok
    assert text.startswith("PASS" if ok else "FAIL")


def test_control_ok(cfg):
    assert control_ok(cfg, O("SUCCESS", rows=[{"patient_count": "488"}]))
    assert not control_ok(cfg, O("SUCCESS", rows=[{"patient_count": "12"}]))
    assert not control_ok(cfg, O("SUCCESS"))
    assert not control_ok(cfg, O("REJECTED"))
    assert not control_ok(cfg, None)


def test_rare_class_check_fails_without_group_column(cfg, monkeypatch, capsys):
    """Live finding: Spark returns an un-aliased column as column__0; the absence check must not pass vacuously."""
    import scripts.demo_blocked as db
    from airlock_lib.deployment import Deployed

    monkeypatch.setattr(db.cleanrooms, "run_protected_query", lambda *a, **k: O("REJECTED", code="V", msg="m"))
    monkeypatch.setattr(db.datafiles, "load_manifest", lambda c: MANIFEST)
    control = O("SUCCESS", rows=[{"column__0": "GLP-1", "patient_count": "700"}])
    control.header = ["column__0", "patient_count"]
    a = StackInfo("A", "CREATE_COMPLETE", {"CollaborationId": "c"}, {}, "ida")
    b = StackInfo("B", "CREATE_COMPLETE", {"AnalystMembershipId": "m"}, {}, "idb")
    rc = db.run(cfg, {"analyst": None}, Deployed(cfg, a, b), control=control, run_id="r")
    out = capsys.readouterr().out
    assert rc == 1
    assert "has no 'medication_class' column" in out
    assert "Meglitinide: 12" not in out


# --------------------------------------------------------------------------- AC3 check
MANIFEST = {"group_column": "medication_class", "classes": {
    "GLP-1": {"n": 700, "mean_change": -1.37}, "SGLT2": {"n": 650, "mean_change": -0.79},
    "Meglitinide": {"n": 12, "mean_change": -0.42}}}
HEADER = ["medication_class", "patient_count", "mean_baseline_hba1c", "mean_followup_hba1c"]


def _row(name, n, base, fu):
    return {"medication_class": name, "patient_count": str(n), "mean_baseline_hba1c": str(base),
            "mean_followup_hba1c": str(fu)}


def test_check_against_manifest_pass(cfg):
    rows = [_row("GLP-1", 700, 8.45, 7.08), _row("SGLT2", 650, 8.40, 7.61)]
    assert check_against_manifest(HEADER, rows, MANIFEST, cfg) == []


def test_check_against_manifest_mismatches(cfg):
    rows = [_row("GLP-1", 699, 8.45, 7.08), _row("SGLT2", 650, 8.40, 7.50), _row("Meglitinide", 12, 8, 7.6)]
    errs = check_against_manifest(HEADER, rows, MANIFEST, cfg)
    assert any("groups returned" in e for e in errs)
    assert any("GLP-1: patient_count 699" in e for e in errs)
    assert any("SGLT2: mean_change" in e for e in errs)


# --------------------------------------------------------------------------- AC7 helpers
def test_apply_set_table_key_addressing(raw):
    out = apply_set(raw, "tables.outcomes.analysis_rule.dimension_columns", "[medication_class]")
    t = next(t for t in out["tables"] if t["key"] == "outcomes")
    assert t["analysis_rule"]["dimension_columns"] == ["medication_class"]
    t0 = next(t for t in raw["tables"] if t["key"] == "outcomes")
    assert t0["analysis_rule"]["dimension_columns"] == ["medication_class", "outcome_flag"]  # input untouched
    assert apply_set(raw, "privacy.min_distinct_patients", "50")["privacy"]["min_distinct_patients"] == 50
    q = "Does mean follow-up HbA1c differ by medication class?"
    assert apply_set(raw, "research.question", q)["research"]["question"] == q


@pytest.mark.parametrize("path", ["privacy.nope", "tables.nosuch.description", "nosuch", "tables.outcomes.x.y"])
def test_apply_set_unknown_path(raw, path):
    with pytest.raises(SetError):
        apply_set(raw, path, "1")


def _tmpl(members_name="State", minimum=30):
    return {"S": {"Resources": {
        "Collaboration": {"Type": "AWS::CleanRooms::Collaboration",
                          "Properties": {"Name": "c", "Members": [{"DisplayName": members_name}]}},
        "CT": {"Type": "AWS::CleanRooms::ConfiguredTable",
               "Properties": {"Name": "airlock-outcomes", "AnalysisRules": [
                   {"Policy": {"V1": {"Aggregation": {"OutputConstraints": [{"Minimum": minimum}],
                                                      "DimensionColumns": ["a", "b"]}}}}]}},
    }}}


def test_diff_templates_flags_create_only():
    changes = diff_templates(_tmpl(), _tmpl(members_name="State U"))
    assert len(changes) == 1
    c = changes[0]
    assert c.path == "Members.0.DisplayName" and c.replacement
    assert REPLACEMENT in c.render()


def test_diff_templates_configured_table_not_replacement():
    changes = diff_templates(_tmpl(), _tmpl(minimum=50))
    assert len(changes) == 1
    c = changes[0]
    assert c.path == "AnalysisRules.0.Policy.V1.Aggregation.OutputConstraints.0.Minimum"
    assert (c.old, c.new) == (30, 50) and not c.replacement
    assert c.render().endswith("Minimum: 30 -> 50")
    assert "(airlock-outcomes)" in c.render()


def _model(service, op):
    return SimpleNamespace(name=op, service_model=SimpleNamespace(service_name=service))


def test_api_echo_skips_credential_calls_and_masks_secrets(capsys):
    _echo_api({"clientSecret": "s", "refreshToken": "t"}, _model("sso-oidc", "CreateToken"))
    _echo_api({"collaborationIdentifier": "c"}, _model("cleanrooms", "ListMembers"))
    _echo_api({"membershipIdentifier": "m", "sessionToken": "t"}, _model("cleanrooms", "DeleteMembership"))
    err = capsys.readouterr().err
    assert err.strip().splitlines() == ["[api] cleanrooms.delete_membership(membershipIdentifier=m, sessionToken=***)"]


def test_diff_templates_scalar_list_reported_whole():
    new = _tmpl()
    new["S"]["Resources"]["CT"]["Properties"]["AnalysisRules"][0]["Policy"]["V1"]["Aggregation"][
        "DimensionColumns"] = ["a"]
    (c,) = diff_templates(_tmpl(), new)
    assert c.path.endswith("DimensionColumns") and c.old == ["a", "b"] and c.new == ["a"]

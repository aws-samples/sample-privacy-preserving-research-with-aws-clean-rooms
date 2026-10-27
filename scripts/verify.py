# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live post-deploy checks (read-only): AC1 members, AC2 analysis rules, Project tags.

Usage: .venv/bin/python -m scripts.verify [--ac1] [--ac2] [--tags] [--config PATH]
(no flag = all). AC1/AC2 failures exit 1; tag failures print [WARN] and never change the
exit code.
"""
from __future__ import annotations

import argparse
from collections import Counter

import botocore.exceptions as bce

from airlock_lib import aws, deployment
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, result_line, warn

Check = tuple  # (ok: bool, name: str, detail: str)


def _paginate(client, op: str, key: str, **kw) -> list:
    items = []
    for page in client.get_paginator(op).paginate(**kw):
        items.extend(page.get(key, []))
    return items


def check_ac1(cfg: Config, sessions: dict, d: deployment.Deployed) -> list[Check]:
    a_cr = sessions["data_owner"].client("cleanrooms")
    b_cr = sessions["analyst"].client("cleanrooms")
    collab = d.collaboration_id
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    members = _paginate(a_cr, "list_members", "memberSummaries", collaborationIdentifier=collab)
    by_acct = {m["accountId"]: m for m in members}
    checks = [(len(members) == 2, "AC1 collaboration has 2 members",
               ", ".join(f"{m['accountId']} {m.get('displayName')} {m.get('status')} {m.get('abilities')}"
                         for m in members))]
    ma, mb = by_acct.get(A.account_id), by_acct.get(B.account_id)
    checks.append((bool(ma) and ma.get("status") == "ACTIVE" and list(ma.get("abilities", [])) == [],
                   f"AC1 {A.display_name} ({A.account_id}) ACTIVE with abilities []",
                   f"{ma.get('status') if ma else 'missing'} {ma.get('abilities') if ma else ''}"))
    checks.append((bool(mb) and mb.get("status") == "ACTIVE"
                   and set(mb.get("abilities", [])) == set(cfg.collaboration.analyst_member_abilities),
                   f"AC1 {B.display_name} ({B.account_id}) ACTIVE with abilities "
                   f"{list(cfg.collaboration.analyst_member_abilities)}",
                   f"{mb.get('status') if mb else 'missing'} {mb.get('abilities') if mb else ''}"))
    for label, cr, mid in (("A", a_cr, d.a_membership_id), ("B", b_cr, d.b_membership_id)):
        mems = [m for m in _paginate(cr, "list_memberships", "membershipSummaries", status="ACTIVE")
                if m.get("collaborationId") == collab]
        ok = any(m["id"] == mid for m in mems) and len(mems) == 1
        checks.append((ok, f"AC1 Account {label} has exactly one ACTIVE membership for the collaboration",
                       ", ".join(m["id"] for m in mems) or "none"))
    return checks


def _agg_cfg(cfg: Config, table) -> dict:
    r = table.analysis_rule
    return {
        "outputConstraints": [{"columnName": cfg.privacy.identity_column, "minimum": cfg.privacy.min_distinct_patients,
                               "type": "COUNT_DISTINCT"}],
        "joinColumns": sorted(r.join_columns),
        "dimensionColumns": sorted(r.dimension_columns),
        "aggregateColumns": Counter((tuple(a.columns), a.function) for a in r.aggregate_columns),
    }


def check_ac2(cfg: Config, sessions: dict, d: deployment.Deployed) -> list[Check]:
    a_cr = sessions["data_owner"].client("cleanrooms")
    ids = d.configured_table_ids
    checks = []
    for t in cfg.tables:
        ct_id = ids.get(t.key)
        if not ct_id:
            checks.append((False, f"AC2 {t.configured_table_name}", "no configured table ID in stack outputs"))
            continue
        rule = a_cr.get_configured_table_analysis_rule(configuredTableIdentifier=ct_id,
                                                       analysisRuleType="AGGREGATION")["analysisRule"]
        agg = rule["policy"]["v1"]["aggregation"]
        want = _agg_cfg(cfg, t)
        got = {
            "outputConstraints": agg.get("outputConstraints"),
            "joinColumns": sorted(agg.get("joinColumns", [])),
            "dimensionColumns": sorted(agg.get("dimensionColumns", [])),
            "aggregateColumns": Counter((tuple(a["columnNames"]), a["function"])
                                        for a in agg.get("aggregateColumns", [])),
        }
        for k in want:
            checks.append((got[k] == want[k], f"AC2 {t.configured_table_name} {k}",
                           f"{dict(got[k]) if isinstance(got[k], Counter) else got[k]}"))
    return checks


def _has_tags(tags: dict, want: dict) -> bool:
    return all(tags.get(k) == v for k, v in want.items())


def check_tags(cfg: Config, sessions: dict, d: deployment.Deployed) -> list[Check]:
    """L5: service-native tag APIs; resourcegroupstaggingapi is informational only."""
    want = cfg.project.tags
    sa, sb = sessions["data_owner"], sessions["analyst"]
    a_cr, b_cr = sa.client("cleanrooms"), sb.client("cleanrooms")
    checks = []

    def cr_tags(cr, arn: str, name: str):
        try:
            tags = cr.list_tags_for_resource(resourceArn=arn).get("tags", {})
            checks.append((_has_tags(tags, want), f"tags {name}", str(tags)))
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            checks.append((False, f"tags {name}", aws.error_message(exc)))

    cr_tags(a_cr, d.collaboration_arn, "collaboration")
    cr_tags(a_cr, d.a_membership_arn, f"{cfg.accounts.data_owner.display_name} membership")
    for t in cfg.tables:
        ct_id = d.configured_table_ids.get(t.key)
        if ct_id:
            arn = a_cr.get_configured_table(configuredTableIdentifier=ct_id)["configuredTable"]["arn"]
            cr_tags(a_cr, arn, f"configured table {t.configured_table_name}")
    for assoc in _paginate(a_cr, "list_configured_table_associations", "configuredTableAssociationSummaries",
                           membershipIdentifier=d.a_membership_id):
        cr_tags(a_cr, assoc["arn"], f"association {assoc['name']}")
    cr_tags(b_cr, d.b_membership_arn, f"{cfg.accounts.analyst.display_name} membership")

    for sess, bucket in ((sa, d.data_bucket), (sb, d.results_bucket)):
        try:
            ts = sess.client("s3").get_bucket_tagging(Bucket=bucket).get("TagSet", [])
            tags = {t["Key"]: t["Value"] for t in ts}
            checks.append((_has_tags(tags, want), f"tags bucket {bucket}", str(tags)))
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            checks.append((False, f"tags bucket {bucket}", aws.error_message(exc)))

    role_name = d.data_role_arn.rsplit("/", 1)[-1]
    try:
        ts = sa.client("iam").list_role_tags(RoleName=role_name).get("Tags", [])
        tags = {t["Key"]: t["Value"] for t in ts}
        checks.append((_has_tags(tags, want), f"tags role {role_name}", str(tags)))
    except bce.ClientError as exc:
        aws.reraise_if_sso(exc)
        checks.append((False, f"tags role {role_name}", aws.error_message(exc)))

    # Informational only (L5): the tagging API's coverage of Clean Rooms is unverified.
    for label, sess in (("A", sa), ("B", sb)):
        try:
            arns = [r["ResourceARN"] for r in _paginate(
                sess.client("resourcegroupstaggingapi"), "get_resources", "ResourceTagMappingList",
                TagFilters=[{"Key": k, "Values": [v]} for k, v in want.items()])]
            print(f"  INFO  resourcegroupstaggingapi Account {label}: {len(arns)} tagged resource(s) indexed")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            print(f"  INFO  resourcegroupstaggingapi Account {label}: {aws.error_message(exc)}")
    print("  INFO  documented exceptions: Glue database/tables (no CFN Tags) and Clean Rooms-created log groups")
    return checks


def run(cfg: Config, sessions: dict, d: deployment.Deployed, ac1=True, ac2=True, tags=True) -> int:
    """Print results. Returns 1 when an AC1/AC2 check fails; tag failures only warn."""
    rc = 0
    if ac1:
        banner("AC1: collaboration members")
        for ok, name, detail in check_ac1(cfg, sessions, d):
            result_line(ok, name, detail)
            rc |= 0 if ok else 1
    if ac2:
        banner("AC2: aggregation analysis rules")
        for ok, name, detail in check_ac2(cfg, sessions, d):
            result_line(ok, name, detail)
            rc |= 0 if ok else 1
    if tags:
        banner(f"Tags: {cfg.project.tags}")
        bad = 0
        for ok, name, detail in check_tags(cfg, sessions, d):
            result_line(ok, name, detail)
            bad += 0 if ok else 1
        if bad:
            warn(f"{bad} tag check(s) failed (informational; does not change the exit code)")
    return rc


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.verify", description="Live AC1/AC2/tag checks (read-only)")
    ap.add_argument("--ac1", action="store_true", help="collaboration members and memberships")
    ap.add_argument("--ac2", action="store_true", help="aggregation analysis rules on both configured tables")
    ap.add_argument("--tags", action="store_true", help="Project tags via service-native APIs")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)
    sel = (args.ac1, args.ac2, args.tags)
    if not any(sel):
        sel = (True, True, True)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        d = deployment.live(cfg, sessions)
        return run(cfg, sessions, d, *sel)

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tear down Airlock: B first (B leaves), then A, then A's retained membership record,
then a residual report (AC6).

Usage: .venv/bin/python -m scripts.teardown [--config PATH]

Uses ``aws cloudformation delete-stack`` (no synth, never touches data files). Only the
Airlock stacks and the resources they own are deleted. CDKToolkit and the log groups that
Clean Rooms creates are never deleted; their delete commands are printed instead.
"""
from __future__ import annotations

import argparse
import time

import botocore.exceptions as bce

from airlock_lib import aws, deployment, state
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, info, result_line, warn


def _paginate(client, op: str, key: str, **kw) -> list:
    items = []
    for page in client.get_paginator(op).paginate(**kw):
        items.extend(page.get(key, []))
    return items


def destroy(cfg: Config, member: str, sess, st: deployment.StackInfo) -> None:
    acct = cfg.member(member)
    banner(f"Destroy {st.name} ({acct.display_name}, {acct.account_id})")
    aws.run_cli(["aws", "cloudformation", "delete-stack", "--stack-name", st.name,
                 "--profile", acct.profile, "--region", cfg.region], acct.profile)
    try:
        sess.client("cloudformation").get_waiter("stack_delete_complete").wait(
            StackName=st.stack_id, WaiterConfig={"Delay": 15, "MaxAttempts": 60})
    except Exception as exc:  # noqa: BLE001 - WaiterError
        aws.reraise_if_sso(exc, acct.profile)
        events = deployment.failed_events(sess, st.stack_id, 20)
        deployment.print_failed_events(events, st.name)
        logical = sorted({e["LogicalResourceId"] for e in events
                          if e.get("ResourceStatus") == "DELETE_FAILED" and e.get("LogicalResourceId") != st.name})
        print("\nManual recovery (review, then run it yourself):")
        print(f"  aws cloudformation delete-stack --stack-name {st.name} --retain-resources "
              f"{' '.join(logical) or '<LogicalId>'} --profile {acct.profile} --region {cfg.region}")
        raise aws.AirlockError(f"{st.name} did not delete cleanly ({exc})") from exc
    info(f"{st.name} deleted")


def residual_report(cfg: Config, sessions: dict, saved: dict) -> bool:
    banner("Residual report (AC6)")
    sa, sb = sessions["data_owner"], sessions["analyst"]
    a_cr, b_cr = sa.client("cleanrooms"), sb.client("cleanrooms")
    collab = saved.get("collaborationId")
    ok_all = True

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        result_line(ok, name, detail)

    if collab:
        try:
            a_cr.get_collaboration(collaborationIdentifier=collab)
            check(False, f"collaboration {collab} removed", "still exists")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            code = aws.error_code(exc)
            check(code in ("ResourceNotFoundException", "AccessDeniedException"),
                  f"collaboration {collab} removed", code)
        for label, cr in (("A", a_cr), ("B", b_cr)):
            active = [m["id"] for m in _paginate(cr, "list_memberships", "membershipSummaries")
                      if m.get("collaborationId") == collab and m.get("status") == "ACTIVE"]
            check(not active, f"Account {label}: no ACTIVE membership for {collab}", ", ".join(active) or "none")
    else:
        info("no saved collaboration ID; collaboration/membership checks skipped")

    names = {t.configured_table_name for t in cfg.tables}
    saved_ids = set((saved.get("configuredTableIds") or {}).values())
    left = [f"{t['name']} ({t['id']})" for t in _paginate(a_cr, "list_configured_tables", "configuredTableSummaries")
            if t.get("name") in names or t.get("id") in saved_ids]
    check(not left, "Account A: no Airlock configured tables", ", ".join(left) or "none")

    for member, sess in (("data_owner", sa), ("analyst", sb)):
        name = cfg.member(member).stack_name
        st = deployment.describe_stack(sess, name)
        check(st is None, f"stack {name} removed", st.status if st else "does not exist")

    for sess, bucket in ((sa, saved.get("dataBucket") or cfg.data_bucket_name),
                         (sb, saved.get("resultsBucket") or cfg.results_bucket_name)):
        try:
            sess.client("s3").head_bucket(Bucket=bucket)
            check(False, f"bucket {bucket} removed", "still exists")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            code = aws.error_code(exc)
            check(code in ("404", "NoSuchBucket", "403"), f"bucket {bucket} removed",
                  "not found" if code in ("404", "NoSuchBucket") else f"{code} (not accessible from this account)")
    return ok_all


def retained_log_groups(cfg: Config, sessions: dict, collab: str | None) -> None:
    """AC6 accepted gap: list (never delete) the Clean Rooms-created analysis log groups."""
    if not collab:
        return
    print("\nRetained (documented residual):")
    found = False
    for member in ("data_owner", "analyst"):
        acct = cfg.member(member)
        try:
            groups = _paginate(sessions[member].client("logs"), "describe_log_groups", "logGroups",
                               logGroupNamePattern=collab)
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc, acct.profile)
            warn(f"could not list log groups in {acct.account_id}: {aws.error_message(exc)}")
            continue
        for g in groups:
            found = True
            print(f"  aws logs delete-log-group --log-group-name {g['logGroupName']} "
                  f"--profile {acct.profile} --region {cfg.region}")
    if not found:
        print("  (no Clean Rooms analysis log groups found for this collaboration)")
    print("  Also retained: the CDKToolkit bootstrap stacks and the CloudWatch Logs resource policy "
          "that Clean Rooms created.")


def run(cfg: Config) -> int:
    banner("Preflight")
    sessions = aws.preflight(cfg)
    sa, sb = sessions["data_owner"], sessions["analyst"]
    d = deployment.live(cfg, sessions)
    if d.a is None and d.b is None:
        saved = state.read_json("last-teardown.json", {}) or {}
        info("both stacks are absent; using the previous last-teardown.json (if any) for residual checks only")
    else:
        saved = {
            "collaborationId": d.a.outputs.get("CollaborationId") if d.a else (
                d.b.parameters.get("CollaborationId") if d.b else None),
            "dataOwnerMembershipId": d.a.outputs.get("DataOwnerMembershipId") if d.a else None,
            "analystMembershipId": d.b.outputs.get("AnalystMembershipId") if d.b else None,
            "configuredTableIds": d.configured_table_ids if (d.a and "ConfiguredTableIds" in d.a.outputs) else {},
            "dataBucket": d.data_bucket,
            "resultsBucket": d.results_bucket,
            "stacks": {"data_owner": cfg.accounts.data_owner.stack_name,
                       "analyst": cfg.accounts.analyst.stack_name},
        }
        state.write_json("last-teardown.json", saved)
        info(f"saved IDs to {state.path('last-teardown.json')}")

    # B first: deleting B's membership is B leaving the collaboration.
    if d.b is not None:
        destroy(cfg, "analyst", sb, d.b)
        old = saved.get("analystMembershipId")
        if old:
            b_cr = sb.client("cleanrooms")
            for _ in range(12):
                active = {m["id"] for m in _paginate(b_cr, "list_memberships", "membershipSummaries",
                                                     status="ACTIVE")}
                if old not in active:
                    break
                time.sleep(5)
            else:
                warn(f"B membership {old} still ACTIVE after 60 s")
    else:
        info(f"{cfg.accounts.analyst.stack_name} not found; skipped")

    if d.a is not None:
        destroy(cfg, "data_owner", sa, d.a)
    else:
        info(f"{cfg.accounts.data_owner.stack_name} not found; skipped")

    mid = saved.get("dataOwnerMembershipId")
    if mid:
        a_cr = sa.client("cleanrooms")
        try:
            m = a_cr.get_membership(membershipIdentifier=mid)["membership"]
            info(f"retained membership {mid} status {m.get('status')}; deleting the record")
            try:
                a_cr.delete_membership(membershipIdentifier=mid)
            except bce.ClientError as exc:
                aws.reraise_if_sso(exc)
                warn(f"tombstone membership {mid} status {m.get('status')} left in place "
                     f"({aws.error_code(exc)}: {aws.error_message(exc)})")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            info(f"retained membership {mid}: {aws.error_code(exc)} (already gone)")

    ok = residual_report(cfg, sessions, saved)
    retained_log_groups(cfg, sessions, saved.get("collaborationId"))
    return 0 if ok else 1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.teardown",
                                 description="Destroy B, then A, then report residuals (AC6).")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)
    aws.run_main(lambda: run(load_config(args.config)), lambda: load_config(args.config))


if __name__ == "__main__":
    main()

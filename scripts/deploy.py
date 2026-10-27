# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deploy Airlock to both accounts: A (phase 1) -> B joins (phase 2) -> A pins trust (phase 3).

Usage: .venv/bin/python -m scripts.deploy [--config PATH]

Every subprocess is echoed as ``[cli] ...`` and every mutating API call as ``[api] ...``,
so the output doubles as the CLI-equivalent record (RR5). Exit codes: 0 ok, 1 failure,
2 SSO expired. Re-running on a healthy environment is a no-op apart from CDK's
"no changes" output.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass

import botocore.exceptions as bce

from airlock_lib import aws, datafiles, deployment, state
from airlock_lib.config import Config, load_config
from airlock_lib.deployment import StackInfo
from airlock_lib.output import banner, info, warn

HEALTHY = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
# L10: S3 bucket-name reuse contention right after teardown is retried after 60 s.
RETRY_REASON = re.compile(r"(?i)assume|role|not authori[sz]ed|permission|OperationAborted|"
                          r"conflicting conditional operation")
BUCKET_REASON = re.compile(r"(?i)OperationAborted|conflicting conditional operation")
TEARDOWN_HINT = "run teardown (it prints the --retain-resources recovery command), then deploy"


@dataclass(frozen=True)
class Action:
    kind: str  # fresh | deploy_b | update_all | exit
    message: str = ""
    catch_all: bool = False


def decide_action(a: StackInfo | None, b: StackInfo | None) -> Action:
    """Stack-state guard (design §4.1 step 4 + L1 catch-all). ROLLBACK_COMPLETE stacks are
    deleted by the caller before this runs."""
    sa, sb = (a.status if a else None), (b.status if b else None)
    if (sa and sa.endswith("_IN_PROGRESS")) or (sb and sb.endswith("_IN_PROGRESS")):
        return Action("exit", "stack operation in progress; retry later")
    if a is None and b is None:
        return Action("fresh")
    if sa in HEALTHY and b is None:
        return Action("deploy_b")
    if sa in HEALTHY and sb in HEALTHY:
        if b.parameters.get("CollaborationId") == a.outputs.get("CollaborationId"):
            return Action("update_all")
        return Action("exit", "B is joined to a different collaboration; run teardown first")
    if b is not None and (a is None or sa.startswith("DELETE_") or sa.startswith("ROLLBACK_")):
        return Action("exit", "B stack exists without A; run teardown first")
    return Action("exit", f"unexpected stack states (A={sa or 'absent'}, B={sb or 'absent'}); {TEARDOWN_HINT}",
                  catch_all=True)


def retry_delay(reasons: list[str]) -> int | None:
    """Seconds to wait before the single phase-1 retry, or None when the failure isn't retryable."""
    if any(BUCKET_REASON.search(r or "") for r in reasons):
        return 60
    if any(RETRY_REASON.search(r or "") for r in reasons):
        return 30
    return None


# --------------------------------------------------------------------------- helpers
def _cdk_env(cfg: Config) -> dict:
    return {"AIRLOCK_CONFIG": str(cfg.path)} if cfg.path else {}


def ensure_bootstrap(cfg: Config, member: str, sess, cdk: str) -> None:
    acct = cfg.member(member)
    tk = deployment.describe_stack(sess, "CDKToolkit")
    ours = tk is not None and all(tk.tags.get(k) == v for k, v in cfg.project.tags.items())
    if tk is None or ours:
        cmd = [cdk, "bootstrap", f"aws://{acct.account_id}/{cfg.region}", "--profile", acct.profile]
        for k, v in cfg.project.tags.items():
            cmd += ["--tags", f"{k}={v}"]
        aws.run_cli(cmd, acct.profile, env_extra=_cdk_env(cfg))
    else:
        info(f"using existing CDKToolkit in {acct.account_id} (BootstrapVersion="
             f"{tk.outputs.get('BootstrapVersion', '?')}); not modified")


def delete_stack(cfg: Config, member: str, sess, info_: StackInfo) -> None:
    acct = cfg.member(member)
    aws.run_cli(["aws", "cloudformation", "delete-stack", "--stack-name", info_.name,
                 "--profile", acct.profile, "--region", cfg.region], acct.profile)
    try:
        sess.client("cloudformation").get_waiter("stack_delete_complete").wait(
            StackName=info_.stack_id, WaiterConfig={"Delay": 15, "MaxAttempts": 60})
    except Exception as exc:  # noqa: BLE001 - WaiterError
        aws.reraise_if_sso(exc, acct.profile)
        deployment.print_failed_events(deployment.failed_events(sess, info_.stack_id, 10), info_.name)
        raise aws.AirlockError(f"deleting {info_.name} did not complete: {exc}; {TEARDOWN_HINT}") from exc


def delete_tombstones(sess, membership_ids: list[str]) -> None:
    cr = sess.client("cleanrooms")
    for mid in membership_ids:
        try:
            cr.delete_membership(membershipIdentifier=mid)
            info(f"deleted retained membership {mid}")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc)
            warn(f"tombstone membership {mid}: {aws.error_code(exc)}: {aws.error_message(exc)}")


def delete_rollback_stacks(cfg: Config, sessions: dict) -> None:
    """Delete ROLLBACK_COMPLETE stacks (B first, then A) and A's tombstone membership."""
    b = deployment.describe_stack(sessions["analyst"], cfg.accounts.analyst.stack_name)
    if b and b.status == "ROLLBACK_COMPLETE":
        warn(f"{b.name} is ROLLBACK_COMPLETE; deleting it before deploying")
        delete_stack(cfg, "analyst", sessions["analyst"], b)
    a = deployment.describe_stack(sessions["data_owner"], cfg.accounts.data_owner.stack_name)
    if a and a.status == "ROLLBACK_COMPLETE":
        warn(f"{a.name} is ROLLBACK_COMPLETE; deleting it before deploying")
        tombstones = deployment.created_memberships(sessions["data_owner"], a.stack_id)
        delete_stack(cfg, "data_owner", sessions["data_owner"], a)
        delete_tombstones(sessions["data_owner"], tombstones)


def cdk_deploy_a(cfg: Config, cdk: str, runner_id: str | None) -> int:
    A = cfg.accounts.data_owner
    cmd = [cdk, "deploy", A.stack_name, "--profile", A.profile, "--region", cfg.region,
           "--require-approval", "never", "--outputs-file", str(state.path("outputs-a.json"))]
    if runner_id:
        cmd += ["-c", f"queryRunnerMembershipId={runner_id}"]
    rc, _ = aws.run_cli(cmd, A.profile, env_extra=_cdk_env(cfg), check=False)
    return rc


def cdk_deploy_b(cfg: Config, cdk: str, collaboration_id: str) -> int:
    B = cfg.accounts.analyst
    cmd = [cdk, "deploy", B.stack_name, "--profile", B.profile, "--region", cfg.region,
           "--require-approval", "never", "--parameters", f"{B.stack_name}:CollaborationId={collaboration_id}",
           "--outputs-file", str(state.path("outputs-b.json"))]
    rc, _ = aws.run_cli(cmd, B.profile, env_extra=_cdk_env(cfg), check=False)
    return rc


def phase1(cfg: Config, sessions: dict, cdk: str, fresh: bool, runner_id: str | None) -> StackInfo:
    banner(f"Phase 1: {cfg.accounts.data_owner.display_name} stack ({cfg.accounts.data_owner.stack_name})")
    sa = sessions["data_owner"]
    name = cfg.accounts.data_owner.stack_name
    for attempt in (1, 2):
        rc = cdk_deploy_a(cfg, cdk, runner_id)
        a = deployment.describe_stack(sa, name)
        if rc == 0 and a is not None and a.status in HEALTHY:
            return a
        events = deployment.failed_events(sa, a.stack_id if a else name, 20)
        deployment.print_failed_events(events, name)
        delay = retry_delay([e.get("ResourceStatusReason") or "" for e in events])
        if attempt == 1 and fresh and a is not None and a.status == "ROLLBACK_COMPLETE" and delay:
            warn(f"phase 1 failed on a fresh create with a retryable reason; cleaning up and retrying once "
                 f"in {delay} s")
            tombstones = deployment.created_memberships(sa, a.stack_id)
            delete_stack(cfg, "data_owner", sa, a)
            delete_tombstones(sa, tombstones)
            time.sleep(delay)
            continue
        raise aws.AirlockError(f"phase 1 ({name}) failed (status {a.status if a else 'absent'})")
    raise aws.AirlockError("phase 1 failed after retry")


def _paginate(client, op: str, key: str, **kw) -> list:
    items = []
    for page in client.get_paginator(op).paginate(**kw):
        items.extend(page.get(key, []))
    return items


def wait_for_invitation(cfg: Config, sessions: dict, collab_id: str) -> None:
    b_cr = sessions["analyst"].client("cleanrooms")
    active = {c["id"] for c in _paginate(b_cr, "list_collaborations", "collaborationList", memberStatus="ACTIVE")}
    if collab_id in active:
        info(f"{cfg.accounts.analyst.display_name} is already an active member of {collab_id}")
        return
    print(f"[cli] aws cleanrooms list-collaborations --member-status INVITED "
          f"--profile {cfg.accounts.analyst.profile} --region {cfg.region}", file=sys.stderr)
    for _ in range(13):  # every 5 s for up to 60 s
        invited = {c["id"] for c in _paginate(b_cr, "list_collaborations", "collaborationList",
                                              memberStatus="INVITED")}
        if collab_id in invited:
            info(f"invitation to {collab_id} is visible to {cfg.accounts.analyst.display_name}")
            return
        time.sleep(5)
    a_cr = sessions["data_owner"].client("cleanrooms")
    members = _paginate(a_cr, "list_members", "memberSummaries", collaborationIdentifier=collab_id)
    me = next((m for m in members if m["accountId"] == cfg.accounts.analyst.account_id), None)
    if me and me.get("status") in ("LEFT", "REMOVED"):
        raise aws.AirlockError(f"{cfg.accounts.analyst.display_name} left this collaboration and cannot rejoin "
                               "it; run teardown, then deploy")
    raise aws.AirlockError(f"invitation to collaboration {collab_id} not visible in Account B after 60 s; "
                           f"members: {[(m['accountId'], m.get('status')) for m in members]}")


def post_checks(cfg: Config, sessions: dict) -> int:
    from scripts import verify

    d = deployment.live(cfg, sessions)
    a_m = sessions["data_owner"].client("cleanrooms").get_membership(
        membershipIdentifier=d.a_membership_id)["membership"]
    b_m = sessions["analyst"].client("cleanrooms").get_membership(
        membershipIdentifier=d.b_membership_id)["membership"]
    state.write_json("memberships.json", {
        "data_owner": {"id": a_m["id"], "arn": a_m["arn"], "createTime": a_m["createTime"].isoformat()},
        "analyst": {"id": b_m["id"], "arn": b_m["arn"], "createTime": b_m["createTime"].isoformat()},
    })
    rc = verify.run(cfg, sessions, d, ac1=True, ac2=True, tags=True)
    banner("Deployed")
    print(f"  Collaboration           {d.collaboration_id}  ({cfg.collaboration.name})")
    print(f"  {cfg.accounts.data_owner.display_name:<23} membership {d.a_membership_id}")
    print(f"  {cfg.accounts.analyst.display_name:<23} membership {d.b_membership_id}")
    print(f"  Data-role trust phase   {d.trust_phase}")
    ready = max(a_m["createTime"], b_m["createTime"]).timestamp() + cfg.logging.warmup_seconds
    ready_s = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ready))
    print(f"  Analysis-log delivery is being set up; demo queries wait until {ready_s} (UTC)")
    return rc


def run(cfg: Config) -> int:
    banner(f"Preflight ({cfg.region})")
    sessions = aws.preflight(cfg)
    cdk = aws.cdk_bin()
    info(f"cdk: {cdk}")
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)

    banner("Data")
    if cfg.data.source == "generator":
        aws.run_cli([sys.executable, "-m", "datagen.generate"] + (["--config", str(cfg.path)] if cfg.path else []),
                    None)
    datafiles.validate(cfg)
    info(f"data files validated under {cfg.data.files_dir}")

    banner("Bootstrap")
    for member in ("data_owner", "analyst"):
        ensure_bootstrap(cfg, member, sessions[member], cdk)

    banner("Stack-state guard")
    delete_rollback_stacks(cfg, sessions)
    a = deployment.describe_stack(sessions["data_owner"], cfg.accounts.data_owner.stack_name)
    b = deployment.describe_stack(sessions["analyst"], cfg.accounts.analyst.stack_name)
    action = decide_action(a, b)
    print(f"  A={a.status if a else 'absent'}  B={b.status if b else 'absent'}  -> {action.kind}")
    if action.kind == "exit":
        if action.catch_all:
            for sess, st, name in ((sessions["data_owner"], a, cfg.accounts.data_owner.stack_name),
                                   (sessions["analyst"], b, cfg.accounts.analyst.stack_name)):
                if st is not None:
                    deployment.print_failed_events(deployment.failed_events(sess, st.stack_id, 10), name)
        raise aws.AirlockError(action.message)

    runner = b.outputs.get("AnalystMembershipId") if (b is not None and b.status in HEALTHY) else None
    a = phase1(cfg, sessions, cdk, fresh=(action.kind == "fresh"), runner_id=runner)
    collab_id = a.outputs["CollaborationId"]
    info(f"collaboration {collab_id}")

    wait_for_invitation(cfg, sessions, collab_id)

    banner(f"Phase 2: {cfg.accounts.analyst.display_name} joins ({cfg.accounts.analyst.stack_name})")
    rc = cdk_deploy_b(cfg, cdk, collab_id)
    b = deployment.describe_stack(sessions["analyst"], cfg.accounts.analyst.stack_name)
    if rc != 0 or b is None or b.status not in HEALTHY:
        deployment.print_failed_events(
            deployment.failed_events(sessions["analyst"], b.stack_id if b else cfg.accounts.analyst.stack_name, 20),
            cfg.accounts.analyst.stack_name)
        raise aws.AirlockError(f"phase 2 ({cfg.accounts.analyst.stack_name}) failed "
                               f"(status {b.status if b else 'absent'})")
    b_mid = b.outputs["AnalystMembershipId"]

    if cfg.security.pin_data_role_trust_to_query_runner:
        banner("Phase 3: pin the data-role trust to the query runner's membership")
        rc = cdk_deploy_a(cfg, cdk, b_mid)
        a = deployment.describe_stack(sessions["data_owner"], cfg.accounts.data_owner.stack_name)
        if rc != 0 or a is None or a.status not in HEALTHY:
            raise aws.AirlockError("phase 3 failed; environment works with phase-1 trust; re-run deploy")
        info("waiting 15 s for IAM propagation")
        time.sleep(15)

    banner("Post-deploy checks")
    return post_checks(cfg, sessions)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.deploy",
                                 description="Deploy both Airlock stacks (A -> B -> A) and verify AC1/AC2.")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)
    aws.run_main(lambda: run(load_config(args.config)), lambda: load_config(args.config))


if __name__ == "__main__":
    main()

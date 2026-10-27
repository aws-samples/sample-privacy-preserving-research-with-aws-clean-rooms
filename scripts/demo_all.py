# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The full demo runbook (requirements §9), one run ID end to end.

Usage: .venv/bin/python -m scripts.demo_all [--no-pause] [--skip-warmup] [--config PATH]

1. The two institutions and the collaboration.   2. A's configured tables + analysis rules.
3. B runs the approved query.                    4. B tries blocked queries.
5. Isolation, query logs, who-can-see-what.      6. README: adapting to a customer's data.
Exit 1 if the allowed, blocked or isolation step fails; a logging PENDING is not a failure.
"""
from __future__ import annotations

import argparse
import json
import re

import botocore.exceptions as bce

from airlock_lib import ROOT, aws, cleanrooms, deployment
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, print_table
from scripts import demo_allowed, demo_blocked, demo_isolation, demo_logging

README_SECTION = "Adapting Airlock to your own data and governance"


def pause(enabled: bool) -> None:
    if enabled:
        try:
            input("\nPress Enter to continue...")
        except EOFError:
            pass


def step1(cfg: Config, sessions: dict, d: deployment.Deployed) -> None:
    banner("Step 1: two institutions, one collaboration")
    for member in ("data_owner", "analyst"):
        acct = cfg.member(member)
        ident = sessions[member].client("sts").get_caller_identity()
        print(f"  {acct.display_name:<36} account {ident['Account']}  profile {acct.profile}")
    a_cr = sessions["data_owner"].client("cleanrooms")
    c = a_cr.get_collaboration(collaborationIdentifier=d.collaboration_id)["collaboration"]
    print(f"\n  Collaboration {c['name']} ({c['id']})")
    print(f"  creator {c.get('creatorDisplayName')} ({c.get('creatorAccountId')}), query logging "
          f"{c.get('queryLogStatus')}, engine {c.get('analyticsEngine')}")
    members = []
    for page in a_cr.get_paginator("list_members").paginate(collaborationIdentifier=d.collaboration_id):
        members.extend(page.get("memberSummaries", []))
    print()
    print_table(["displayName", "accountId", "abilities", "status"],
                [{"displayName": m.get("displayName"), "accountId": m.get("accountId"),
                  "abilities": ", ".join(m.get("abilities", [])) or "(none: contributes data)",
                  "status": m.get("status")} for m in members])


def step2(cfg: Config, sessions: dict, d: deployment.Deployed) -> None:
    banner(f"Step 2: {cfg.accounts.data_owner.display_name}'s configured tables + analysis rules")
    a_cr = sessions["data_owner"].client("cleanrooms")
    b_cr = sessions["analyst"].client("cleanrooms")
    ids = d.configured_table_ids
    for t in cfg.tables:
        print(f"\n  {t.configured_table_name} (association {t.association_name!r}, Glue {t.glue_table_name})")
        rule = a_cr.get_configured_table_analysis_rule(configuredTableIdentifier=ids[t.key],
                                                       analysisRuleType="AGGREGATION")["analysisRule"]
        print("  A's rule (author view):")
        for line in json.dumps(rule["policy"], indent=2).splitlines():
            print(f"    {line}")
        try:
            schema = b_cr.get_schema(collaborationIdentifier=d.collaboration_id, name=t.association_name)["schema"]
            print(f"  B's view (schema only): {[c['name'] for c in schema.get('columns', [])]}")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc, cfg.accounts.analyst.profile)
            print(f"  B's view: {aws.error_code(exc)}: {aws.error_message(exc)}")


def step6() -> None:
    banner("Step 6: adapting Airlock to a customer's data and governance")
    readme = ROOT / "README.md"
    print(f"  {readme}  ->  section \"{README_SECTION}\"")
    text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = README_SECTION in line
            continue
        if in_section and re.match(r"^#{3,4} ", line):
            print(f"    {line.lstrip('#').strip()}")


def run(cfg: Config, sessions: dict, d: deployment.Deployed, pause_enabled: bool, skip_warmup: bool) -> int:
    run_id = cleanrooms.new_run_id()
    print(f"Demo run {run_id}")
    step1(cfg, sessions, d)
    pause(pause_enabled)
    step2(cfg, sessions, d)
    pause(pause_enabled)

    cleanrooms.ensure_log_warmup(cfg, sessions, d, skip=skip_warmup)
    rc_allowed, approved = demo_allowed.run(cfg, sessions, d, run_id, check=True, warmup=False)
    pause(pause_enabled)
    rc_blocked = demo_blocked.run(cfg, sessions, d, control=approved, run_id=run_id)
    pause(pause_enabled)

    rc_iso = demo_isolation.run(cfg, sessions, d)
    try:
        rc_log = demo_logging.run(cfg, sessions, d, run_id=run_id, wait_minutes=0)
    except aws.AirlockError as exc:
        print(f"  logging: {exc}")
        rc_log = 1
    if rc_log == demo_logging.PENDING:
        print("  Analysis-log entries can take a few minutes to arrive. Re-run: "
              f".venv/bin/python -m scripts.demo_logging --run-id {run_id} --wait-minutes 10")
    pause(pause_enabled)
    step6()

    banner("Summary")
    for name, rc in (("allowed (FR6/AC3)", rc_allowed), ("blocked (FR7/AC4)", rc_blocked),
                     ("isolation (FR9)", rc_iso), ("logging (FR10/AC5)", rc_log)):
        state_ = "PASS" if rc == 0 else ("PENDING" if rc == demo_logging.PENDING else "FAIL")
        print(f"  {state_:<8} {name}")
    return 1 if (rc_allowed or rc_blocked or rc_iso) else 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.demo_all", description=__doc__.splitlines()[0])
    ap.add_argument("--no-pause", action="store_true", help="do not wait for Enter between steps")
    ap.add_argument("--skip-warmup", action="store_true", help="skip the analysis-log warm-up gate (logs may be lost)")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        d = deployment.live(cfg, sessions)
        return run(cfg, sessions, d, pause_enabled=not args.no_pause, skip_warmup=args.skip_warmup)

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()

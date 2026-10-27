# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo step 4 (FR7, AC4): queries the analysis rule blocks or limits.

Usage: .venv/bin/python -m scripts.demo_blocked [--skip-warmup] [--config PATH]

The approved query runs first as a control (same run ID): only when it succeeds with rows
do REJECTED / no-rows outcomes mean "blocked by the rule". Rejection messages are printed
verbatim, with no pattern matching.
"""
from __future__ import annotations

import argparse

from airlock_lib import aws, cleanrooms, datafiles, deployment
from airlock_lib.cleanrooms import QueryOutcome
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, print_table

ALWAYS_FAIL = {"ERROR", "FAILED", "TIMEOUT", "CANCELLED"}
CONTROL_FAIL = ("FAIL: environment not validated (approved control query did not succeed with rows) "
                "— fix and run demo_allowed first")


def blocked_verdict(expect: str, outcome: QueryOutcome) -> tuple[bool, str]:
    if outcome.status in ALWAYS_FAIL:
        return False, f"FAIL: {outcome.status}: {outcome.error_code}: {outcome.error_message}"
    if expect == "REJECTED":
        if outcome.status == "REJECTED":
            return True, "PASS: rejected by the analysis rule"
        return False, f"FAIL: query {outcome.status} with {len(outcome.rows)} row(s); expected a rejection"
    if expect == "NO_ROWS":
        if outcome.status == "REJECTED":
            return True, "PASS: rejected by the analysis rule"
        if outcome.status == "SUCCESS" and not outcome.rows:
            return True, "PASS: suppressed (0 rows returned)"
        return False, f"FAIL: {len(outcome.rows)} row(s) returned; expected the group to be suppressed"
    return False, f"FAIL: unknown expectation {expect!r}"


def control_ok(cfg: Config, control: QueryOutcome | None) -> bool:
    if control is None or control.status != "SUCCESS" or not control.rows:
        return False
    c = cfg.research.count_column
    for r in control.rows:
        try:
            if int(float(r.get(c))) >= cfg.privacy.min_distinct_patients:
                return True
        except (TypeError, ValueError):
            continue
    return False


def run(cfg: Config, sessions: dict, d: deployment.Deployed, control: QueryOutcome | None = None,
        run_id: str | None = None, skip_warmup: bool = False) -> int:
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    run_id = run_id or cleanrooms.new_run_id()
    banner(f"Blocked / limited: {B.display_name} tries to go around {A.display_name}'s rule")
    if control is None:
        cleanrooms.ensure_log_warmup(cfg, sessions, d, skip=skip_warmup)
        print("  Control: the approved query must succeed first (proves the environment works).")
        control = cleanrooms.run_protected_query(
            sessions["analyst"], d.b_membership_id, cfg.approved_query_path, "control", cfg, run_id,
            d.results_bucket, collaboration_id=d.collaboration_id)
        print(f"  control {control.protected_query_id}: {control.status}, {len(control.rows)} row(s)")
    else:
        print(f"  Control: approved query {control.protected_query_id} from this run: "
              f"{control.status}, {len(control.rows)} row(s)")

    if not control_ok(cfg, control):
        for q in cfg.research.blocked_queries:
            print(f"  {q.title}: {CONTROL_FAIL}")
        if control is not None and (control.error_code or control.error_message):
            print(f"  control error: {control.error_code}: {control.error_message}")
        return 1

    failures = 0
    for i, q in enumerate(cfg.research.blocked_queries, start=1):
        print(f"\n[{chr(96 + i)}] {q.title}   (expect: {q.expect})")
        outcome = cleanrooms.run_protected_query(
            sessions["analyst"], d.b_membership_id, cfg.blocked_query_path(q.id), f"blocked:{q.id}", cfg, run_id,
            d.results_bucket, collaboration_id=d.collaboration_id)
        print("  SQL sent:")
        for line in outcome.sent_text.splitlines():
            print(f"    {line}")
        if outcome.status == "REJECTED":
            print(f"  REJECTED: {outcome.error_code}: {outcome.error_message}")
        elif outcome.status == "SUCCESS":
            print(f"  SUCCESS: {len(outcome.rows)} rows  (protected query {outcome.protected_query_id})")
            if outcome.rows:
                print_table(outcome.header, outcome.rows)
        else:
            print(f"  {outcome.status}: {outcome.error_code}: {outcome.error_message}")
        ok, text = blocked_verdict(q.expect, outcome)
        print(f"  {text}")
        failures += 0 if ok else 1

    print()
    manifest = datafiles.load_manifest(cfg)
    if manifest is None or manifest.get("group_column") != cfg.research.group_by_column:
        print("  rare-class check skipped (no A-side manifest for this grouping)")
    else:
        rare = {n: s for n, s in manifest.get("classes", {}).items() if s["n"] < cfg.privacy.min_distinct_patients}
        if not rare:
            print("  [INFO] no class below threshold in this dataset")
        visible = {str(r.get(cfg.research.group_by_column)) for r in control.rows}
        if cfg.research.group_by_column not in control.header:
            # Without the group column the absence check below would pass vacuously.
            print(f"  FAIL  control result header {control.header} has no {cfg.research.group_by_column!r} "
                  "column; alias it in the approved SQL")
            failures += 1
            rare = {}
        for name, s in rare.items():
            absent = name not in visible
            print(f"  {'PASS' if absent else 'FAIL'}  {name}: {s['n']} patients in {A.display_name}'s source "
                  f"(A-side manifest) → {0 if absent else 'visible'} rows visible to {B.display_name}")
            failures += 0 if absent else 1
    print(f"\n  {'All blocked/limited checks PASS' if not failures else f'{failures} check(s) FAILED'}")
    return 0 if not failures else 1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.demo_blocked", description=__doc__.splitlines()[0])
    ap.add_argument("--skip-warmup", action="store_true", help="skip the analysis-log warm-up gate (logs may be lost)")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        d = deployment.live(cfg, sessions)
        return run(cfg, sessions, d, skip_warmup=args.skip_warmup)

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()

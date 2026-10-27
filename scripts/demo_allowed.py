# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo step 3 (FR6, AC3): Institution B runs the approved query and gets aggregates.

Usage: .venv/bin/python -m scripts.demo_allowed [--check] [--skip-warmup] [--config PATH]

FR5: the approved query is a vetted SQL file checked into queries/approved/ and run by
this script; the aggregation analysis rule enforces what any query may return.
"""
from __future__ import annotations

import argparse

from airlock_lib import aws, cleanrooms, datafiles, deployment
from airlock_lib.cleanrooms import QueryOutcome
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, error, print_table, warn


def derived_rows(cfg: Config, header: list[str], rows: list[dict]) -> tuple[list[str], list[dict]]:
    """Append the optional client-side difference column (aggregation SQL can't do arithmetic)."""
    dd = cfg.research.derived_difference
    if dd is None:
        return header, rows
    missing = [c for c in (dd.minuend, dd.subtrahend) if c not in header]
    if missing:
        warn(f"derived_difference skipped: missing {', '.join(missing)}")
        return header, rows
    col = f"{dd.name} (computed client-side)"
    out = []
    for r in rows:
        r2 = dict(r)
        try:
            r2[col] = round(float(r[dd.minuend]) - float(r[dd.subtrahend]), 2)
        except (TypeError, ValueError):
            r2[col] = None
        out.append(r2)
    return header + [col], out


def check_against_manifest(header: list[str], rows: list[dict], manifest: dict, cfg: Config) -> list[str]:
    """AC3 check against the data owner's ground truth. Empty list = pass."""
    errs = []
    g, c, n_min = cfg.research.group_by_column, cfg.research.count_column, cfg.privacy.min_distinct_patients
    classes = manifest.get("classes", {})
    expected = {name for name, s in classes.items() if s["n"] >= n_min}
    got = {str(r.get(g)) for r in rows}
    if got != expected:
        errs.append(f"groups returned {sorted(got)} != expected (n >= {n_min}) {sorted(expected)}")
    dd = cfg.research.derived_difference
    for r in rows:
        name = str(r.get(g))
        if name not in classes:
            continue
        try:
            count = int(float(r.get(c)))
        except (TypeError, ValueError):
            errs.append(f"{name}: {c} {r.get(c)!r} is not a number")
            continue
        if count != classes[name]["n"]:
            errs.append(f"{name}: {c} {count} != manifest n {classes[name]['n']}")
        if dd is not None and dd.minuend in header and dd.subtrahend in header:
            value = float(r[dd.minuend]) - float(r[dd.subtrahend])
            if abs(value - classes[name]["mean_change"]) > 0.01:
                errs.append(f"{name}: {dd.name} {value:.4f} differs from manifest "
                            f"{classes[name]['mean_change']:.4f} by more than 0.01")
    return errs


def print_outcome(cfg: Config, sql_path, outcome: QueryOutcome) -> None:
    print(f"  SQL file            {sql_path}")
    print(f"  file sha256         {outcome.file_sha256}")
    print(f"  sent sha256         {outcome.sent_sha256}")
    print("  SQL sent (full-line comments stripped):")
    for line in outcome.sent_text.splitlines():
        print(f"    {line}")
    print(f"  protected query ID  {outcome.protected_query_id}")
    print(f"  status              {outcome.status}  ({outcome.duration_s} s)")
    if outcome.error_code or outcome.error_message:
        print(f"  error               {outcome.error_code}: {outcome.error_message}")
    if outcome.result_location:
        print(f"  result location     {outcome.result_location}")
        print(f"  result keys         {outcome.result_keys}")


def run(cfg: Config, sessions: dict, d: deployment.Deployed, run_id: str, check: bool = False,
        skip_warmup: bool = False, warmup: bool = True) -> tuple[int, QueryOutcome]:
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    if warmup:
        cleanrooms.ensure_log_warmup(cfg, sessions, d, skip=skip_warmup)
    banner(f"Allowed: {B.display_name} runs the approved query")
    print(f"  Research question: {cfg.research.question}")
    sql_path = cfg.approved_query_path
    outcome = cleanrooms.run_protected_query(
        sessions["analyst"], d.b_membership_id, sql_path, "approved", cfg, run_id, d.results_bucket,
        collaboration_id=d.collaboration_id)
    print_outcome(cfg, sql_path, outcome)

    rc = 0
    if outcome.status != "SUCCESS":
        error(f"approved query did not succeed: {outcome.status}")
        return 1, outcome
    if not outcome.rows:
        error(f"no result rows parsed under {outcome.result_location}: {outcome.result_keys}")
        return 1, outcome
    for col in (cfg.research.group_by_column, cfg.research.count_column):
        if col not in outcome.header:
            error(f"result header {outcome.header} has no {col!r} column (Clean Rooms names un-aliased "
                  f"columns column__N: alias it in the SQL, e.g. AS {col}; see README troubleshooting)")
            rc = 1
    print()
    header, rows = derived_rows(cfg, outcome.header, outcome.rows)
    print_table(header, rows)
    print(f"\n  Groups with fewer than {cfg.privacy.min_distinct_patients} distinct "
          f"{cfg.privacy.identity_column} values are suppressed by {A.display_name}'s rule.")

    if check:
        manifest = datafiles.load_manifest(cfg)
        if manifest is None or manifest.get("group_column") != cfg.research.group_by_column:
            print("  check skipped (no A-side manifest for this grouping)")
        else:
            errs = check_against_manifest(outcome.header, outcome.rows, manifest, cfg)
            if errs:
                for e in errs:
                    print(f"  FAIL  {e}")
                rc = 1
            else:
                print(f"  PASS  AC3: groups, {cfg.research.count_column} and "
                      f"{cfg.research.derived_difference.name if cfg.research.derived_difference else 'counts'} "
                      "match the data owner's manifest (A-side ground truth)")
    return rc, outcome


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.demo_allowed", description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="AC3: compare against the generator's A-side manifest")
    ap.add_argument("--skip-warmup", action="store_true", help="skip the analysis-log warm-up gate (logs may be lost)")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        d = deployment.live(cfg, sessions)
        rc, _ = run(cfg, sessions, d, cleanrooms.new_run_id(), check=args.check, skip_warmup=args.skip_warmup)
        return rc

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()

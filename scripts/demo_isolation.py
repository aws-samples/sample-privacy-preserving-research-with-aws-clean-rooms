# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo step 5a (FR9, FR11): Institution B cannot read Institution A's rows; it sees only
the allowed schema, the rules, and aggregate results. Ends with the who-can-see-what matrix.

Usage: .venv/bin/python -m scripts.demo_isolation [--config PATH]

Every call below uses B's own SSO profile.
"""
from __future__ import annotations

import argparse
import json

import botocore.exceptions as bce

from airlock_lib import aws, cleanrooms, deployment, state
from airlock_lib.config import Config, load_config
from airlock_lib.output import banner, print_table, result_line


def matrix_rows(cfg: Config) -> list[tuple[str, str, str]]:
    n = cfg.privacy.min_distinct_patients
    idc = cfg.privacy.identity_column
    hidden = sorted({c for t in cfg.tables for c in t.column_names if c not in t.allowed_columns})
    can_query = "CAN_QUERY" in cfg.collaboration.analyst_member_abilities
    return [
        ("Raw rows in S3 / Glue tables", "Yes (owns them)", "No - AccessDenied (demo_isolation)"),
        (f"Columns not in the allowed list ({', '.join(hidden) or 'none'})", "Yes", "Not visible, even as schema"),
        (f"{idc} values", "Yes", "Never; join key only, can't be selected"),
        ("Schema of allowed columns + analysis rule", "Yes (author)", "Read-only via the collaboration"),
        ("Run queries", "No (no CAN_QUERY)", "Yes, only aggregate SQL the rule allows" if can_query else "No"),
        ("Query results", "No", f"Yes, aggregates only, in {cfg.results_bucket_name}"),
        (f"Groups below the threshold ({n} distinct {idc})", "Yes (own data)", "Suppressed (row absent)"),
        ("Clean Rooms analysis logs", "Yes, for queries touching its tables (query text, runner, receiver)",
         "Yes, for queries it ran/received"),
        ("CloudTrail StartProtectedQuery events", "No (recorded in the caller's account)", "Yes"),
        ("Query compute charges", "No", "Yes (payer)"),
    ]


def print_matrix(cfg: Config) -> None:
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    banner("Who can see what (FR11)")
    print_table(["Item", f"{A.display_name} (data owner)", f"{B.display_name} (analyst)"], matrix_rows(cfg))


def _expect_denied(fn, ok_codes: set[str], profile: str) -> tuple[bool, str]:
    try:
        fn()
        return False, "call SUCCEEDED (expected access denied)"
    except bce.ClientError as exc:
        aws.reraise_if_sso(exc, profile)
        code = aws.error_code(exc)
        return code in ok_codes, f"{code}: {aws.error_message(exc)}"


def _first_parquet_key(cfg: Config) -> str:
    t = cfg.tables[0]
    tdir = cfg.files_dir_path / t.s3_prefix
    files = sorted(p.name for p in tdir.glob("*.parquet")) if tdir.is_dir() else []
    return f"{t.s3_prefix}{files[0] if files else t.key + '.parquet'}"


def run(cfg: Config, sessions: dict, d: deployment.Deployed) -> int:
    A, B = cfg.accounts.data_owner, cfg.accounts.analyst
    sb = sessions["analyst"]
    s3, glue, cr = sb.client("s3"), sb.client("glue"), sb.client("cleanrooms")
    bucket, db = d.data_bucket, d.glue_database
    failures = 0
    banner(f"Isolation (FR9): {B.display_name} ({B.account_id}, profile {B.profile}) vs "
           f"{A.display_name}'s data")

    ok, detail = _expect_denied(lambda: s3.list_objects_v2(Bucket=bucket), {"AccessDenied"}, B.profile)
    result_line(ok, f"B lists s3://{bucket}", detail)
    failures += 0 if ok else 1

    key = _first_parquet_key(cfg)
    ok, detail = _expect_denied(lambda: s3.get_object(Bucket=bucket, Key=key), {"AccessDenied"}, B.profile)
    result_line(ok, f"B reads s3://{bucket}/{key}", detail)
    failures += 0 if ok else 1

    t0 = cfg.tables[0]
    ok, detail = _expect_denied(
        lambda: glue.get_table(CatalogId=A.account_id, DatabaseName=db, Name=t0.glue_table_name),
        {"AccessDeniedException", "EntityNotFoundException"}, B.profile)
    result_line(ok, f"B reads Glue table {A.account_id}:{db}.{t0.glue_table_name}",
                f"not accessible ({detail})" if ok else detail)
    failures += 0 if ok else 1

    print()
    for t in cfg.tables:
        try:
            schema = cr.get_schema(collaborationIdentifier=d.collaboration_id, name=t.association_name)["schema"]
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc, B.profile)
            result_line(False, f"B views schema {t.association_name}", aws.error_message(exc))
            failures += 1
            continue
        visible = [c["name"] for c in schema.get("columns", [])]
        hidden = [c for c in t.column_names if c not in t.allowed_columns]
        leaked = [c for c in hidden if c in visible]
        result_line(not leaked, f"B's schema view of {t.association_name}: {visible}",
                    f"hidden columns absent: {hidden}" if not leaked else f"LEAKED: {leaked}")
        failures += 1 if leaked else 0
        try:
            rule = cr.get_schema_analysis_rule(collaborationIdentifier=d.collaboration_id, name=t.association_name,
                                               type="AGGREGATION")["analysisRule"]
            agg = rule["policy"]["v1"]["aggregation"]
            print(f"        rule (read-only for B): outputConstraints={json.dumps(agg.get('outputConstraints'))} "
                  f"joinColumns={agg.get('joinColumns')} dimensionColumns={agg.get('dimensionColumns')}")
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc, B.profile)
            print(f"        rule: {aws.error_code(exc)}: {aws.error_message(exc)}")

    print()
    prefix = cfg.storage.results_key_prefix
    try:
        objs = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=d.results_bucket, Prefix=prefix):
            objs.extend(page.get("Contents", []))
        objs.sort(key=lambda o: o["LastModified"], reverse=True)
        result_line(True, f"B lists its own results bucket s3://{d.results_bucket}/{prefix}",
                    f"{len(objs)} object(s); latest: {[o['Key'] for o in objs[:3]]}")
    except bce.ClientError as exc:
        aws.reraise_if_sso(exc, B.profile)
        result_line(False, f"B lists s3://{d.results_bucket}/{prefix}", aws.error_message(exc))
        failures += 1

    latest = None
    for line in reversed(state.read_jsonl(cleanrooms.QUERY_LOG)):
        if (line.get("label") in ("approved", "control") and line.get("status") == "SUCCESS"
                and line.get("collaboration_id") == d.collaboration_id and line.get("protected_query_id")):
            latest = line
            break
    if latest is None:
        print("  SKIP  no approved/control result yet for this collaboration; run demo_allowed first")
    else:
        try:
            pq_desc = cr.get_protected_query(membershipIdentifier=d.b_membership_id,
                                             protectedQueryIdentifier=latest["protected_query_id"])["protectedQuery"]
            _, _, header, rows = cleanrooms.fetch_results(s3, pq_desc, d.results_bucket, prefix,
                                                          latest["protected_query_id"], cfg.storage.result_format)
            leaked = cfg.privacy.identity_column in header
            result_line(not leaked, f"latest approved result {latest['protected_query_id']} holds aggregates only",
                        f"columns {header}")
            failures += 1 if leaked else 0
            print_table(header, rows[:3])
        except bce.ClientError as exc:
            aws.reraise_if_sso(exc, B.profile)
            print(f"  SKIP  could not re-read the latest result: {aws.error_message(exc)}")

    print_matrix(cfg)
    return 0 if not failures else 1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.demo_isolation", description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        cfg = load_config(args.config)
        sessions = aws.preflight(cfg)
        return run(cfg, sessions, deployment.live(cfg, sessions))

    aws.run_main(body, lambda: load_config(args.config))


if __name__ == "__main__":
    main()

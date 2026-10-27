---
inclusion: always
---

# Tech stack and commands

## Stack

- **AWS CDK v2 in Python 3.12**, two stacks: `AirlockDataOwner` (Account A) and `AirlockAnalyst` (Account B). Scripts use boto3; stacks use CDK L1 `aws_cleanrooms` constructs.
- **Exact pins** in `requirements.txt`: `aws-cdk-lib==2.273.0`, `aws-cdk.cloud-assembly-schema==54.26.0`, `constructs==10.8.1`, `boto3==1.43.117`, `botocore==1.43.117`, `pyarrow==26.0.0`, `PyYAML==6.0.3`. `requirements-dev.txt` adds `pytest==9.1.1`. Pin any new dependency exactly (`==`), prefer well-known packages, and don't bump `aws-cdk-lib` past 2.273.0 without upgrading the CDK CLI (CLI 2.1126.0 supports cloud-assembly schema 54; aws-cdk-lib 2.274.0 needs 55).
- **Always use the project venv:** `.venv/bin/python`, `.venv/bin/pip`. Create it with `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`. `cdk.json` runs `.venv/bin/python app.py`.
- **CDK CLI 2.1126.0:** `cdk` on `PATH`, or `export AIRLOCK_CDK_BIN=/path/to/cdk`; the scripts read it.

## Commands (run from the repository root)

```bash
.venv/bin/python -m pytest -q                 # unit tests, no AWS calls
cdk synth --quiet                             # or "$AIRLOCK_CDK_BIN" synth --quiet; both stacks into cdk.out/
.venv/bin/python -m datagen.generate          # regenerate seeded data (deterministic)
.venv/bin/python -m scripts.ac7_config_diff --set privacy.min_distinct_patients=50   # config-change preview, no deploy

# These call AWS (see aws-safety.md before running any of them):
.venv/bin/python -m scripts.verify            # read-only AC1/AC2/tag checks (--ac1 --ac2 --tags)
.venv/bin/python -m scripts.deploy            # mutating: A -> B -> A
.venv/bin/python -m scripts.teardown          # destructive: B, then A
.venv/bin/python -m scripts.demo_all          # full runbook (--no-pause, --skip-warmup)
.venv/bin/python -m scripts.demo_allowed      # approved query (--check compares to A's manifest)
.venv/bin/python -m scripts.demo_blocked      # control + blocked/limited queries
.venv/bin/python -m scripts.demo_isolation    # B-side AccessDenied checks + matrix
.venv/bin/python -m scripts.demo_logging      # A's analysis log + B's CloudTrail (--wait-minutes N)
```

- Every entry point accepts `--config PATH` (else `$AIRLOCK_CONFIG`, else `config/airlock.yaml`).
- Synth context: `-c queryRunnerMembershipId=<uuid>` (deploy phase 3 only).
- Exit codes: `0` ok, `1` failure or config error, `2` SSO session expired, `3` logs pending (`demo_logging`).
- After any code change, run `pytest -q` and `cdk synth` before calling it done.

## Hard Clean Rooms constraints (learned live)

- **Spark only.** `analytics_engine` must be `SPARK`; the Clean Rooms SQL engine was retired Dec 17, 2025.
- **Aggregation-rule SQL:** every SELECT needs at least one aggregate; aggregate functions are limited to `COUNT`, `COUNT_DISTINCT`, `AVG`, `SUM`, `SUM_DISTINCT` on the columns the rule lists (Airlock uses `COUNT`, `COUNT DISTINCT`, `AVG`). **No median.** **No arithmetic between aggregates**: compute differences client-side (`research.derived_difference`).
- **Join columns cannot be projected.** `patient_id` can join and be counted, never appear in the output ("prohibited in the PROJECT clause"). Dimension columns are the only GROUP BY/filter columns.
- **Alias every selected column**, including plain dimensions (`o.medication_class AS medication_class`); Spark names un-aliased output columns `column__0`, `column__1`, …
- **Rejections are asynchronous:** `StartProtectedQuery` returns `SUBMITTED`, then about 7 s later the query is `FAILED` with code `QUERY_FAILED`. Don't expect a synchronous `ValidationException`.
- **Concurrency:** outside `us-east-1` the default is 2 concurrent SQL queries per account. Run queries strictly one at a time and never run two demo scripts at once.
- **Timing:** successful queries take about 2–4 minutes (the first on a new collaboration is slowest); analysis-log entries arrive about 5 minutes later; queries in the first ~5 minutes after membership creation may not be logged (the scripts gate on it).
- Worker types `CR.1X` and `CR.4X` only (`CR.8X` deliberately excluded).
- Spark analysis-log entries have no `logStage` field: submission entries (one per table) have no `status`; the termination entry does.

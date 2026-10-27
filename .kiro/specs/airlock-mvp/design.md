# Airlock MVP: Design

Status: implemented and verified live in `us-west-2`: AC1–AC7 pass. Account IDs in this document are AWS documentation example IDs (A `111122223333`, B `444455556666`), and the institution names are fictional. Requirements: [`requirements.md`](requirements.md) (including its "Implementation decisions" section). This document describes the system as built; where the original design assumption turned out wrong, the [Live findings](#11-live-findings) section records what the service actually does and how the code handles it.

## 1. Overview

Airlock is two CDK v2 (Python) stacks plus Python scripts, driven by one YAML file (`config/airlock.yaml`).

- **Account A**, 111122223333, Mercy Research Hospital (profile `airlock-a`), data owner and collaboration creator:
  - seeded synthetic Parquet data in S3 and the Glue catalog;
  - two Clean Rooms configured tables with **aggregation analysis rules** (minimum 30 distinct `patient_id` per output row);
  - the collaboration, A's own membership, the data-access role and the table associations.
- **Account B**, 444455556666, State University Health Analytics (profile `airlock-b`), analyst:
  - joins by creating its membership (that is the invitation acceptance);
  - owns the results bucket;
  - is the only query runner and result receiver, and pays for query compute.
- **Region:** `us-west-2` for everything.
- **Deploy:** a Python orchestrator runs A → B → A. The second A pass pins the data-role trust policy to B's membership ID.
- **Demo:** boto3 scripts wait for the analysis-log warm-up, then run the approved query (the control) and the blocked queries strictly one at a time, then show isolation and logs.

Key decisions:

- **Analytics engine `SPARK`.** Clean Rooms SQL engine support ended Dec 17, 2025; Spark supports aggregation rules.
- **FR5 via a vetted SQL file**, not an analysis template: analysis templates only work with the custom analysis rule, and the requirements call for the aggregation rule.
- **No result-writer role in B.** A member that both queries and receives results may skip it.
- **A's membership is a separate `CfnMembership` with `DeletionPolicy: Retain`.** The creator can only leave by deleting the collaboration; teardown removes the leftover record.
- **Teardown uses `aws cloudformation delete-stack`, not `cdk destroy`.** No synth, so teardown never touches data files or config.

## 2. Layout, config, dependencies

### 2.1 Directory layout (relative to the repository root)

```
airlock/
  README.md                  # RR4: prerequisites, deploy, runbook, controls, teardown, adapting, troubleshooting
  requirements.txt           # runtime pins (exact)
  requirements-dev.txt       # -r requirements.txt + pytest
  cdk.json                   # {"app": ".venv/bin/python app.py", "output": "cdk.out"}
  pytest.ini                 # testpaths = tests
  app.py                     # CDK entry: load + validate config and data files, context check, build_app
  config/airlock.yaml        # the single config file (override: --config PATH or $AIRLOCK_CONFIG)
  airlock_lib/
    __init__.py              # ROOT = airlock/ (all relative config paths resolve against it)
    config.py                # YAML -> frozen dataclasses, validation, derived names
    datafiles.py             # Parquet layout/schema validation, A-side manifest loader
    aws.py                   # per-profile sessions, SSO-expiry handling, [cli]/[api] echo, preflight
    deployment.py            # live describe-stacks outputs for both stacks (authoritative IDs)
    cleanrooms.py            # prepare_sql, run_protected_query, fetch_results, ensure_log_warmup, log helpers
    state.py                 # .state/*.json(l) records
    output.py                # banners, fixed-width tables, value formatting, [INFO]/[WARN]/[ERROR]
  stacks/
    __init__.py              # build_app(cfg, app, query_runner_membership_id): both stacks + Tags.of(app)
    analysis_rule.py         # pure fn: table cfg + privacy cfg -> aggregation rule
    data_owner_stack.py      # AirlockDataOwner (Account A)
    analyst_stack.py         # AirlockAnalyst (Account B)
  datagen/generate.py        # python -m datagen.generate (refuses to run when data.source=files)
  data/
    SCHEMA.md                # schema contract (RR2), incl. "bring your own files"
    generated/               # runtime only, git-ignored; owned and recreated by the generator
      s3/patients/patients.parquet, s3/outcomes/outcomes.parquet   # uploaded to A's bucket
      local/patients.csv, local/outcomes.csv                         # human preview, never uploaded
      local/manifest.json                                            # A-side ground truth (per-class n, means, sha256)
  queries/approved/mean_hba1c_by_medication_class.sql
  queries/blocked/01_select_patient_id.sql, 02_row_level_select_star.sql, 03_rare_class_filter.sql
  scripts/                   # deploy, teardown, verify, demo_all, demo_allowed, demo_blocked,
                             # demo_isolation, demo_logging, ac7_config_diff
  tests/                     # conftest.py + test_config, test_analysis_rule, test_datagen, test_datafiles,
                             # test_stacks, test_cleanrooms, test_scripts_logic (no AWS calls)
  docs/sample-output.md      # captured output of a real demo run
  .state/                    # runtime only, git-ignored, no secrets: outputs-a.json, outputs-b.json,
                             # memberships.json, query_log.jsonl, log_groups.json, last-teardown.json
```

Commands run from the repository root as `.venv/bin/python -m scripts.<name>` (or `-m datagen.generate`, `-m pytest -q`). The CDK binary is `$AIRLOCK_CDK_BIN`, else `cdk` on `PATH`; otherwise the scripts exit 1 with a hint.

**IDs are always read live.** Scripts read collaboration, membership, configured-table and bucket identifiers from `describe-stacks` outputs in both accounts (`airlock_lib/deployment.py`). B's joined collaboration is read from B's stack **parameter** `CollaborationId`. The `.state/` files are records, never the source of truth, so every script stays correct after a teardown and redeploy.

### 2.2 Config (`config/airlock.yaml`)

The file in the repo is the full, commented schema. Sections:

| Section | Contents |
|---|---|
| `project` | `name`, `tags` (`{Project: Airlock}`) |
| `region` | one region for everything |
| `accounts.data_owner`, `accounts.analyst` | `account_id` (quoted 12 digits), SSO `profile`, `display_name`, `stack_name` |
| `collaboration` | `name`, `description`, `analytics_engine: SPARK`, `query_log_status: ENABLED`, `creator_member_abilities: []`, `analyst_member_abilities: [CAN_QUERY, CAN_RECEIVE_RESULTS]`, `query_compute_payer: analyst` |
| `storage` | bucket prefixes (bucket = `<prefix>-<account_id>-<region>`), `results_key_prefix: results/`, `result_format: CSV`, `single_file_output: true` |
| `glue` | `database_name: airlock_research` |
| `privacy` | `identity_column: patient_id`, `min_distinct_patients: 30` |
| `security` | `pin_data_role_trust_to_query_runner: true` |
| `logging` | `warmup_seconds: 300`, `log_group_wait_seconds: 600` |
| `tables[*]` | `key`, Glue name, S3 prefix, configured-table and association names, description, columns (Glue schema, Parquet order), `allowed_columns`, `analysis_rule` (join, dimension, aggregate columns, scalar functions) |
| `research` | question, approved SQL file, `group_by_column`, `count_column`, optional `derived_difference`, `blocked_queries` (id, title, file, expect `REJECTED`/`NO_ROWS`) |
| `query` | `compute: {worker_type: CR.1X, worker_count: 4}` (or null), poll interval, timeout |
| `data` | `source: generator` or `files`, `files_dir`, generator parameters (seed 20261022, 3000 patients, exact class counts) |

**Validation (`airlock_lib/config.py`).** Every failure raises `ConfigError("<dotted.path>: <reason>")`; `app.py` and the scripts print it and exit 1 before any AWS call. Unknown keys are rejected, so typos (and the removed `collaboration.allowed_result_receivers`) fail loudly.

- Accounts and region: `account_id` matches `^\d{12}$` and the two differ; `region` matches `^[a-z]{2}-[a-z]+-\d$`; stack names are CloudFormation-safe.
- Names and descriptions: display names and `collaboration.name` 1–100 chars; `collaboration.description` 1–255 chars and not whitespace-only; table descriptions 0–255 chars.
- Buckets and paths: bucket prefixes `[a-z0-9-]`, derived bucket names valid; `results_key_prefix` matches `^[\w!.=*/-]{0,512}$`; `result_format` in `{CSV, PARQUET}`; Glue names lower-case; `s3_prefix` matches `^[a-z0-9_-]+/$` and is unique.
- Collaboration (MVP limits): `analytics_engine` must be `SPARK`; `creator_member_abilities` must be `[]`; `analyst_member_abilities` is a non-empty subset of `{CAN_QUERY, CAN_RECEIVE_RESULTS}` that includes `CAN_QUERY`; `query_compute_payer` must be `analyst`.
- Tables and rule: column names `^[a-z0-9_]+$`; Glue types from `{string, int, bigint, double, date, timestamp, boolean}`; `allowed_columns` ⊆ `columns`; every rule column ⊆ `allowed_columns`; join ∩ dimension = ∅ and aggregate ∩ dimension = ∅; functions from `{COUNT, COUNT_DISTINCT, AVG, SUM, SUM_DISTINCT}`; `identity_column` in every table's `allowed_columns`; `min_distinct_patients` in 2..100000; unique table keys, configured-table names and association names.
- Research: SQL files exist; `group_by_column`, `count_column` and `derived_difference` fields are identifiers.
- Logging: ints in 0..3600.
- Query compute: `worker_type` in `{CR.1X, CR.4X}` (`CR.8X` deliberately excluded, and the error says so); `worker_count` 2..128 for CR.1X, 2..32 for CR.4X (the 512-vCPU concurrent SQL quota); poll 1..60 s; timeout 60..7200 s.
- Data source: `files_dir` is relative and resolves inside the repository root. With `source: generator` it must be under `data/generated/` and `data.generator` is required (class counts sum to `patient_count`, positive weights, `age.min ≥ 18`). With `source: files` it must **not** be under `data/generated/`, so a stray generator run can never wipe your own files.

**Path rule (AC7, RR3).** All relative config paths (`data.files_dir`, `research.approved_query_file`, `research.blocked_queries[*].file`) resolve against `airlock_lib.ROOT` (the repository root directory), never the current directory or the config file's directory.

Derived values: `data_bucket_name`, `results_bucket_name`, `member(name)`, `table(key)`, `files_dir_path`, `approved_query_path`, `blocked_query_path(id)`, `payer_flags()` (`{data_owner: False, analyst: True}`).

**Data-file validation (`airlock_lib/datafiles.py`)** runs at synth (`app.py`) and in deploy. For each table, `<files_dir>/<s3_prefix>` must contain ≥1 `*.parquet` file and no other non-hidden files; each Parquet schema must match `tables[*].columns` in names, order and compatible types (string→string, int32→int, int64→bigint, double→double, date32→date, timestamp→timestamp, bool→boolean). Stray files outside every table prefix are an error. In generator mode, missing files produce `run .venv/bin/python -m datagen.generate`.

### 2.3 Pinned dependencies

`requirements.txt` (exact pins): `aws-cdk-lib==2.273.0`, `aws-cdk.cloud-assembly-schema==54.26.0`, `constructs==10.8.1`, `boto3==1.43.117`, `botocore==1.43.117`, `pyarrow==26.0.0`, `PyYAML==6.0.3`. `requirements-dev.txt`: `-r requirements.txt` plus `pytest==9.1.1`. Python 3.12.

aws-cdk-lib is held at 2.273.0 because the CDK CLI 2.1126.0 supports cloud-assembly schema major 54, and aws-cdk-lib 2.274.0 needs schema 55. Fallback if `cdk synth` reports `Cloud assembly schema version mismatch`: `aws-cdk-lib==2.260.0`, or upgrade the CLI.

## 3. Synthetic data generator (RR2, C1)

`python -m datagen.generate [--config PATH]`. With `data.source: files` it exits 1 ("generator disabled; your files … are used as-is").

- One `random.Random(seed)` instance, no other entropy; pyarrow writes snappy Parquet.
- IDs `SYN-000001` … `SYN-003000`. A shuffled list with the exact class counts assigns one medication class per patient.
- Per patient: `age = clamp(round(gauss(58,12)), 18, 89)`; sex, region, diagnosis and procedure by weighted choice; `enrollment_date = 2025-01-01 + randint(0,364)`; `baseline = round(clamp(gauss(8.4,1.1), 6.5, 13.5), 1)`; `followup = round(clamp(baseline + gauss(mean_change, sd_change), 5.0, 14.0), 1)`; `followup_date = enrollment + randint(150,210)`; `outcome_flag` from `delta = round(followup − baseline, 1)`: improved ≤ −0.5, worsened ≥ 0.5, else stable.
- One row per patient per table, so the inner join is 1:1 and `AVG(followup) − AVG(baseline)` equals the group's mean change.
- Arrow types: `patient_id` string, `age` int32, HbA1c float64, dates date32, others string (Glue `string/int/double/date`; the spec's "float" is stored as double).
- It deletes and recreates only `data/generated/`, writes the Parquet files, CSV previews and a deterministic `local/manifest.json` (seed, row counts, `group_column`, per-class `{n, mean_baseline, mean_followup, mean_change, outcome_flag_counts}`, Parquet sha256). No timestamps, so the asset hash is stable across runs.
- Fatal self-checks: columns, order and types match the config; `age ≥ 18`; unique IDs; every outcome ID exists in patients; counts sum to `patient_count`. Story checks are `[WARN]` only (no class below the threshold; a visible class below 2× the threshold).
- C1: every value comes from the seeded RNG; no names, addresses, birth dates, MRNs or free text; IDs carry `SYN-`.

Default data (threshold 30):

| medication_class | n | mean baseline | mean follow-up | mean change | visible to B |
|---|---|---|---|---|---|
| Metformin | 1150 | 8.502 | 7.394 | −1.107 | yes |
| GLP-1 | 700 | 8.450 | 7.081 | −1.369 | yes |
| SGLT2 | 650 | 8.436 | 7.644 | −0.791 | yes |
| DPP-4 | 488 | 8.502 | 7.962 | −0.539 | yes |
| Meglitinide | 12 | 7.925 | 7.500 | −0.425 | no (12 < 30) |

## 4. Stacks

### 4.1 App and tagging

`app.py` loads and validates the config, runs `datafiles.validate`, validates the optional context `-c queryRunnerMembershipId=<uuid>` (exit 1 if not a UUID), then calls `stacks.build_app(cfg, app, runner)` and synthesizes. `build_app` is the single code path for synth and tests:

- `AirlockDataOwner` with `env=Environment(account=A, region)` and `AirlockAnalyst` with `env=Environment(account=B, region)`; stack names from config.
- `Tags.of(app).add(k, v)` for each `project.tags` entry: resource tags on taggable resources plus CloudFormation **stack tags**, which CloudFormation propagates (this covers the CDK custom-resource provider Lambdas and roles).
- Documented exceptions: `AWS::Glue::Database`/`AWS::Glue::Table` (no `Tags` property) and the log groups Clean Rooms creates.
- `test_stacks.py` asserts per-resource `Project=Airlock` on every `AWS::CleanRooms::*` resource, both buckets and the data-access role (skipping logical IDs that start with `Custom` or `AWS679f53fac002430cb0da5b7982bd2287`, i.e. not every IAM role), and asserts stack-level tags via `assembly.get_stack_by_name(name).tags`.

Construct IDs are stable (`DataBucket`, `DataDeployment`, `DataDeploymentLogs`, `GlueDatabase`, `GlueTable<Key>`, `Collaboration`, `DataOwnerMembership`, `DataAccessRole`, `ConfiguredTable<Key>`, `Association<Key>`, `ResultsBucket`, `AnalystMembership`) so logical IDs, and therefore `ac7_config_diff` output, stay comparable.

### 4.2 Account A stack `AirlockDataOwner` (`stacks/data_owner_stack.py`)

**S3 data bucket.** `<data_bucket_prefix>-<A>-<region>`; `BLOCK_ALL`, SSE-S3, `enforce_ssl`, unversioned, `BUCKET_OWNER_ENFORCED`, `DESTROY` + `auto_delete_objects`; no bucket policy grants to B. A `BucketDeployment` uploads `Source.asset(<files_dir>, exclude=[".*", "**/.*"])` with `prune=True`, `retain_on_delete=False`, `memory_limit=256` and a one-week log group. Objects land at `<s3_prefix><file>`, e.g. `patients/patients.parquet`.

**Glue.** `CfnDatabase` plus one external Parquet `CfnTable` per config table (Hive Parquet input/output formats and SerDe, location `s3://<bucket>/<s3_prefix>`), each depending on the database. Lake Formation is in IAM-only mode in both accounts, so no LF grants are needed.

**Collaboration.** `CfnCollaboration`:

```
name                          = collaboration.name
description                   = collaboration.description
creator_display_name          = accounts.data_owner.display_name
creator_member_abilities      = []                                  # kept as [] in the template
creator_payment_configuration = {query_compute: {is_responsible: False}}
members = [{account_id: B, display_name: accounts.analyst.display_name,
            member_abilities: [CAN_QUERY, CAN_RECEIVE_RESULTS],
            payment_configuration: {query_compute: {is_responsible: True}}}]
query_log_status = ENABLED
analytics_engine = SPARK
# not set: JobLogStatus, DataEncryptionMetadata (no C3R), AutoApprovedChangeTypes,
#          AllowedResultRegions, IsMetricsEnabled, CreatorMLMemberAbilities
```

Create-only properties (from the live CloudFormation schema): `CreatorDisplayName`, `CreatorMemberAbilities`, `CreatorMLMemberAbilities`, `DataEncryptionMetadata`, `JobLogStatus`, `QueryLogStatus`, `Members`, `CreatorPaymentConfiguration`, `AutoApprovedChangeTypes`, `AllowedResultRegions`. Changing any of them replaces the collaboration. The config keys that map to them are `accounts.*.account_id`, `accounts.*.display_name`, `collaboration.query_log_status`, `collaboration.creator_member_abilities`, `collaboration.analyst_member_abilities` and `collaboration.query_compute_payer`; they require teardown, then deploy.

**A's membership.** `CfnMembership(collaboration_identifier=<collab>, query_log_status=ENABLED)`, no result or payment configuration (A neither receives results nor pays), `RemovalPolicy.RETAIN`.

**Data-access role** (used by both associations). `iam.Role` assumed by `cleanrooms.amazonaws.com` with inline policy `AirlockDataRead` (exists before Clean Rooms validates the role), following the AWS "read data from Amazon S3" service-role policy:

- `glue:GetDatabase, GetDatabases, GetTable, GetTables, GetPartition, GetPartitions, BatchGetPartition` on the catalog, the database and each table ARN;
- `glue:GetSchema, GetSchemaVersion` on `*`;
- `s3:GetBucketLocation, s3:ListBucket` on the bucket and `s3:GetObject` on `<bucket>/<prefix>*` per table, all with `StringEquals s3:ResourceAccount = <A>`.

The trust policy is replaced with `add_property_override("AssumeRolePolicyDocument", …)` in the AWS confused-deputy form (two statements, `AllowIfSourceArnMatches` and `AllowIfExternalIdMatches`):

- **Phase 1** (no `queryRunnerMembershipId` context): `ForAnyValue:ArnLike aws:SourceArn` = [A's membership ARN, `arn:aws:cleanrooms:<region>:<B>:membership/*`]; `StringLike sts:ExternalId` = `arn:aws:*:<region>:*:dbuser:*/*`.
- **Phase 2** (context set and `pin_data_role_trust_to_query_runner: true`): `ForAnyValue:ArnEquals aws:SourceArn` = [A's membership ARN, `arn:aws:cleanrooms:<region>:<B>:membership/<B-id>`]; `StringLike sts:ExternalId` = `arn:aws:*:<region>:*:dbuser:*/<B-id>*`.

**Configured tables.** One `CfnConfiguredTable` per table: `analysis_method = DIRECT_QUERY`, `allowed_columns` from config, Glue table reference, and one `AGGREGATION` analysis rule from `stacks/analysis_rule.py`. Each depends explicitly on its Glue table (the names are plain strings). For `outcomes`:

```json
{
  "AggregateColumns": [
    {"ColumnNames": ["patient_id"],     "Function": "COUNT_DISTINCT"},
    {"ColumnNames": ["patient_id"],     "Function": "COUNT"},
    {"ColumnNames": ["followup_hba1c"], "Function": "AVG"}
  ],
  "JoinColumns": ["patient_id"],
  "DimensionColumns": ["medication_class", "outcome_flag"],
  "ScalarFunctions": [],
  "OutputConstraints": [{"ColumnName": "patient_id", "Minimum": 30, "Type": "COUNT_DISTINCT"}]
}
```

`patients` is the same with `baseline_hba1c` as the AVG column and dimensions `[sex, region, primary_diagnosis]`. `ScalarFunctions: []` is required by the CFN schema and kept by the CDK L1 (asserted in `test_stacks.py`). Omitted on purpose: `JoinRequired` (`QUERY_RUNNER` would need a table owned by B), `AllowedJoinOperators` (default), `AdditionalAnalyses`.

**Associations.** One `CfnConfiguredTableAssociation` per table: `name = association_name` (the table name used in SQL), the configured table, A's membership and the data-access role. No association analysis rule; the README documents the optional `allowedResultReceivers` association rule as a CLI command.

**Outputs (A):** `CollaborationId`, `CollaborationArn`, `DataOwnerMembershipId`, `DataOwnerMembershipArn`, `DataBucketName`, `GlueDatabaseName`, `ConfiguredTableIds` (JSON map key → ID), `DataAccessRoleArn`, `TrustPhase` (`1` or `2`). No cross-stack exports.

### 4.3 Account B stack `AirlockAnalyst` (`stacks/analyst_stack.py`)

- **`CfnParameter CollaborationId`:** String, UUID `allowed_pattern`, no default. Synth works without it; a deploy without it fails before any resource is created.
- **Results bucket:** `<results_bucket_prefix>-<B>-<region>`, hardened like A's bucket.
- **B's membership** (creating it is accepting the invitation): `query_log_status = ENABLED`; payment `{query_compute: {is_responsible: True}}`; `default_result_configuration` S3 output to the results bucket with `key_prefix: results/`, `result_format: CSV`, `single_file_output: true`, and no `role_arn`. `membership.node.add_dependency(results_bucket)` makes it depend on the bucket, its policy and the auto-delete custom resource, so a bucket-side failure cannot roll back (and permanently "leave") the membership. Default removal policy, so stack deletion calls DeleteMembership (B leaves).
- **Outputs (B):** `AnalystMembershipId`, `AnalystMembershipArn`, `ResultsBucketName`, `JoinedCollaborationId` (named so because `CollaborationId` is already the parameter's construct ID).

## 5. Deploy and teardown

### 5.1 AWS access (`airlock_lib/aws.py`)

- `session(profile, region)` returns a `boto3.Session(profile_name=…, region_name=…)`. Subprocesses get `--profile` and `--region` explicitly; `AWS_PROFILE` is never exported.
- `preflight(cfg)` runs `sts get-caller-identity` per profile and exits 1 if an account differs from the config.
- **SSO expiry → exit 2** with `SSO session expired for profile <p>. Run: aws sso login --profile <p>`: botocore `SSOTokenLoadError`, `UnauthorizedSSOTokenError`, `TokenRetrievalError`, `NoCredentialsError`, `SSOError`; ClientError codes `ExpiredToken`, `ExpiredTokenException`, `InvalidClientTokenId`, `UnrecognizedClientException`; or subprocess output containing `Token has expired`, `SSO session`, `Error loading SSO Token`, `ExpiredToken`.
- **Command echo (RR5).** Every subprocess is echoed as `[cli] <command>`. Every mutating boto3 call (`Create|Delete|Start|Update|Put*`) is echoed as `[api] <service>.<op>(<args>)`, but only for the services Airlock calls (`cleanrooms, s3, cloudformation, glue, iam, logs, cloudtrail`), and any parameter whose name contains `secret`, `token`, `password` or `credential` prints as `***`. This keeps botocore's internal `sso-oidc`/`sts` credential calls out of the output.
- Exit codes everywhere: `0` ok, `1` failure or config error, `2` SSO expired, `3` logs pending (`demo_logging` only).

### 5.2 `scripts/deploy.py`

1. **Preflight**, then resolve the CDK binary.
2. **Data:** with `data.source: generator`, run `python -m datagen.generate` (deterministic). Then `datafiles.validate` in both modes.
3. **Bootstrap**, per account: if `CDKToolkit` is absent or carries the project tags, run `cdk bootstrap aws://<acct>/<region> --profile <p> --tags Project=Airlock` (idempotent). A pre-existing untagged `CDKToolkit` is used as-is and never modified.
4. **Stack-state guard.** Delete any `ROLLBACK_COMPLETE` stack first (B, then A; for A, read the stack events for `AWS::CleanRooms::Membership` resources that reached `CREATE_COMPLETE` and delete those retained records afterwards, `[WARN]` on failure). Then `decide_action`:

   | A stack | B stack | Action |
   |---|---|---|
   | any `*_IN_PROGRESS` | any | exit 1: retry later |
   | any | any `*_IN_PROGRESS` | exit 1: retry later |
   | absent | absent | fresh deploy |
   | healthy (`CREATE_COMPLETE`, `UPDATE_COMPLETE`, `UPDATE_ROLLBACK_COMPLETE`) | absent | update A, then deploy B |
   | healthy | healthy, `CollaborationId` parameter = A's output | update all phases (idempotent) |
   | healthy | healthy, joined to a different collaboration | exit 1: run teardown first |
   | absent, `DELETE_*` or `ROLLBACK_*` | exists | exit 1: B exists without A; run teardown first |
   | any other combination | | exit 1, printing both statuses, the 10 newest `*_FAILED` events per stack and the teardown hint |

5. **Phase 1 (A):** `cdk deploy AirlockDataOwner --profile <pA> --region <r> --require-approval never --outputs-file .state/outputs-a.json` (plus `-c queryRunnerMembershipId=<B id>` when B is already healthy). **One automatic retry** only for a fresh create that ends `ROLLBACK_COMPLETE` with a `*_FAILED` reason matching `(?i)assume|role|not authori[sz]ed|permission|OperationAborted|conflicting conditional operation`: print the events, delete the stack, clean the retained membership, sleep 60 s for the S3 bucket-name contention reasons (`OperationAborted`, `conflicting conditional operation`) or 30 s otherwise, retry once. Any other failure prints the 20 newest `*_FAILED` events and exits 1.
6. **Invitation visible to B:** skip if B's `list_collaborations(memberStatus=ACTIVE)` already contains it; else poll `memberStatus=INVITED` every 5 s for 60 s. On timeout, A's `list_members` decides the message: B `LEFT`/`REMOVED` → "left this collaboration and cannot rejoin it; run teardown, then deploy"; otherwise the member list.
7. **Phase 2 (B):** `cdk deploy AirlockAnalyst --profile <pB> --region <r> --require-approval never --parameters AirlockAnalyst:CollaborationId=<id> --outputs-file .state/outputs-b.json`. On failure, print events and exit 1.
8. **Phase 3 (A):** with `pin_data_role_trust_to_query_runner`, redeploy A with `-c queryRunnerMembershipId=<B membership>`; only the role trust policy changes. Sleep 15 s for IAM propagation. On failure: exit 1 ("environment works with phase-1 trust; re-run deploy").
9. **Post-checks:** write `.state/memberships.json` (IDs, ARNs, `createTime`), run `verify` AC1, AC2 and tags in-process, print the IDs and the UTC time after which demo queries will run (`max(createTime) + warmup_seconds`). An AC1 or AC2 failure exits 1; a tag mismatch only prints `[WARN]`.

Re-running deploy on a healthy environment changes nothing (CDK reports "no changes").

### 5.3 `scripts/teardown.py`

1. **Preflight** (no synth, no generator). Read both stacks' outputs; save `{collaborationId, dataOwnerMembershipId, analystMembershipId, configuredTableIds, dataBucket, resultsBucket, stacks}` to `.state/last-teardown.json`. If both stacks are absent, reuse the previous file for the residual checks only.
2. **Destroy B first:** `aws cloudformation delete-stack --stack-name AirlockAnalyst --profile <pB> --region <r>` plus a boto3 waiter (15 s × 60). This deletes B's membership (B leaves) and empties the results bucket. Then wait up to 60 s until B's old membership is no longer `ACTIVE`.
3. **Destroy A:** same for `AirlockDataOwner` (associations, configured tables, collaboration, role, Glue objects, data bucket). A's membership is retained by CloudFormation.
4. **A's retained membership:** `get_membership` by the saved ID; if it exists (status `COLLABORATION_DELETED`), `delete_membership`. A `ClientError` there is a `[WARN]`, not a failure.
5. **Residual report (AC6)** by saved IDs, PASS/FAIL per check, exit 1 on any FAIL:
   - A: `get_collaboration(<old id>)` raises `ResourceNotFoundException` or `AccessDeniedException`;
   - each account: no `ACTIVE` membership for the old collaboration;
   - A: no configured table with an Airlock name or a saved ID;
   - both stacks: do not exist;
   - both buckets: `head_bucket` → 404/NoSuchBucket (403 is reported as "not accessible from this account").
6. **Retained (documented residual):** list, never delete, the log groups whose name contains the old collaboration ID in both accounts and print one `aws logs delete-log-group --log-group-name <name> --profile <p> --region <r>` per group. `CDKToolkit` is never touched.

If a stack ends `DELETE_FAILED`, teardown prints the failed events and the `aws cloudformation delete-stack --stack-name <s> --retain-resources <LogicalId…> --profile <p> --region <r>` command for the user to review and run, and exits 1. Teardown never deletes resources the stacks don't own.

### 5.4 CLI equivalents (RR5)

The README's "No-console runbook" maps every console step to its IaC resource and an `aws cleanrooms …` command with `--profile` and `--region`, including the cross-account invitation (A's `create-collaboration` with `--members`) and acceptance (B's `create-membership`). Because the scripts echo every subprocess and mutating API call, their output is also a CLI record of what ran.

## 6. Demo scripts

### 6.1 Shared helpers (`airlock_lib/cleanrooms.py`)

- **`prepare_sql(path) -> (sent_text, file_sha256, sent_sha256)`:** drop full-line `--` comments, strip leading/trailing blank lines and one trailing `;`. Inline `--` after code is left alone. Both hashes are printed and logged.
- **`run_protected_query(session, membership_id, sql_path, label, cfg, run_id, results_bucket, collaboration_id=None) -> QueryOutcome`:**
  1. `start_protected_query(type=SQL, membershipIdentifier, sqlParameters={queryString}, computeConfiguration={worker: {type, number}})` (no `computeConfiguration` when `query.compute` is null; no `resultConfiguration`, so the membership default applies).
  2. Start errors: `ValidationException` → `REJECTED` (kept as a safety net; live rejections are asynchronous, see §11); `ServiceQuotaExceededException`/`ThrottlingException` → sleep 20 s, retry up to 3 times; SSO errors → exit 2; anything else → `ERROR`.
  3. Poll `get_protected_query` every `poll_interval_seconds` until `SUCCESS | FAILED | CANCELLED | TIMED_OUT`. Local timeout → `update_protected_query(targetStatus=CANCELLED)` and `TIMEOUT`.
  4. `FAILED` → `classify_failure`: `REJECTED` when `(?i)analysis.?rule|validat|not allowed|not supported|disallow|invalid|column` matches `"<code> <message>"`, else `FAILED`. The verbatim `error.code` and `error.message` are always kept and printed.
  5. `SUCCESS` → `fetch_results`.
  6. Append one JSON line to `.state/query_log.jsonl`: `{ts_utc, run_id, label, collaboration_id, membership_id, file_sha256, sent_sha256, protected_query_id, status, error_code, error_message, result_location, result_keys, row_count}`. Labels: `approved`, `control`, `blocked:<id>`.
- **`fetch_results`:** if `protectedQuery.result.output.s3.location` is present, list every object under it (paginated); otherwise `[WARN]` the output union keys and list `<results_bucket>/<results_key_prefix>` filtered by the query ID. Drop keys ending in `/`, `_SUCCESS`, `.crc`, `$folder$` and zero-byte objects; if any kept key has the configured format's extension (`.csv`/`.parquet`), keep only those. Parse CSV with `utf-8-sig` `DictReader`, Parquet with `pyarrow`. Unparsable objects are `[WARN]`ed and skipped.
- **`ensure_log_warmup(cfg, sessions, deployed, skip=False)`:** called by `demo_allowed`, `demo_blocked` (standalone) and `demo_all` before any query. Reads both memberships' `createTime` live; while `now < max(createTime) + warmup_seconds`, prints a countdown every 15 s. Then polls A's `describe_log_groups(logGroupNamePattern=<collaboration-id>)` every 15 s for up to `log_group_wait_seconds`; on timeout it warns and continues (never fatal). `--skip-warmup` bypasses both with a `[WARN]`. On a long-lived environment it returns at once.
- **Log helpers:** `decode_log_event` (`json.loads` the message, then the nested JSON-string fields `resultReceivers`, `queryRunners`, `memberDisplayNames`, `memberSchemaMapping`); `log_stage(ev)` (uses `logStage` if present, else `TERMINATION` when the entry has `status`, otherwise `SUBMISSION`); `find_ac5_entry(events, query_id, analyst_account_id)` (a `SUBMISSION` entry with matching `eventId` whose `resultReceivers` contains the analyst account); `select_ac5_target(query_log, since, run_id=None)` (the latest run with a `SUCCESS` `approved`/`control` line).
- Queries always run **strictly sequentially**.

### 6.2 SQL (exact; full-line comments are stripped before sending)

`queries/approved/mean_hba1c_by_medication_class.sql`:

```sql
-- Airlock approved query (vetted by Institution A). Research question: config research.question
SELECT
  o.medication_class           AS medication_class,
  COUNT(DISTINCT p.patient_id) AS patient_count,
  AVG(p.baseline_hba1c)        AS mean_baseline_hba1c,
  AVG(o.followup_hba1c)        AS mean_followup_hba1c
FROM patients p
INNER JOIN outcomes o
  ON p.patient_id = o.patient_id
GROUP BY o.medication_class
ORDER BY o.medication_class
```

Every selected column is aliased, including the plain dimension column (see §11). The mean change is computed client-side via `research.derived_difference`.

`queries/blocked/01_select_patient_id.sql` (expect `REJECTED`):

```sql
-- Blocked: tries to return the identifier (join-only column)
SELECT patient_id, AVG(followup_hba1c) AS mean_followup_hba1c
FROM outcomes
GROUP BY patient_id
```

`queries/blocked/02_row_level_select_star.sql` (expect `REJECTED`):

```sql
-- Blocked: row-level read
SELECT * FROM patients
```

`queries/blocked/03_rare_class_filter.sql` (expect `NO_ROWS`):

```sql
-- Limited: the rare class has fewer distinct patients than the threshold -> suppressed
SELECT o.medication_class           AS medication_class,
       COUNT(DISTINCT o.patient_id) AS patient_count,
       AVG(o.followup_hba1c)        AS mean_followup_hba1c
FROM outcomes o
WHERE o.medication_class = 'Meglitinide'
GROUP BY o.medication_class
```

### 6.3 Scripts

Every printed threshold, account ID, display name, bucket, prefix and format is rendered from the config; the scripts contain no literals for these.

- **`demo_allowed [--check] [--skip-warmup]` (FR6, FR8, AC3).** Warm-up gate; prints the question, SQL path, both hashes, the sent SQL, query ID, status, duration, result location and keys; then a table of the returned columns plus `mean_change (computed client-side)` (2 dp) and the footer "Groups with fewer than 30 distinct patient_id values are suppressed by Mercy Research Hospital's rule." Exits 1 when the status is not `SUCCESS`, no rows were parsed, or `group_by_column`/`count_column` is missing from the header (the message names the `column__N` cause and the aliasing fix). `--check` (generator data with a manifest whose `group_column` matches) asserts that the returned groups equal the manifest classes with `n ≥ threshold`, that counts match, and that the derived difference is within 0.01 of the manifest.
- **`demo_blocked [--skip-warmup]` (FR7, AC4).** Control first: `demo_all` passes its approved outcome; standalone it runs the approved query as `control`. The control must be `SUCCESS` with ≥1 row whose count is ≥ the threshold; otherwise every blocked check prints `FAIL: environment not validated …` and the script exits 1. Then each `blocked_queries` entry runs in order, printing the sent SQL and `REJECTED: <code>: <verbatim message>` or `SUCCESS: <n> rows`. Verdicts: `REJECTED` expects status `REJECTED`; `NO_ROWS` passes on `SUCCESS` with 0 rows or `REJECTED`; `ERROR`/`FAILED`/`TIMEOUT`/`CANCELLED` always fail. Rare-class line (manifest required): for each class with `n < threshold`, assert it is absent from the control rows and print `Meglitinide: 12 patients in Mercy Research Hospital's source (A-side manifest) → 0 rows visible to State University Health Analytics`. If the control header lacks the group column, that check FAILs instead of passing vacuously.
- **`demo_isolation` (FR9, FR11).** With B's session: `s3.list_objects_v2` and `s3.get_object` on A's bucket → `AccessDenied`; `glue.get_table` in A's catalog → `AccessDeniedException` (or `EntityNotFoundException`); `get_schema` per association → hidden columns absent; `get_schema_analysis_rule` → the constraints B can see; B's own results bucket listing and the latest approved/control result re-parsed (must not contain `patient_id`). Ends with the who-can-see-what matrix (§7).
- **`demo_logging [--run-id R | --since-minutes N] [--wait-minutes M]` (FR10, AC5).** Target: `select_ac5_target` (no target → exit 1 "run demo_allowed first"). Window starts 30 min before the run's earliest logged query. (a) A's analysis log: find groups by `logGroupNamePattern=<collaboration-id>`, read with the `filter_log_events` paginator, decode, print time, stage, `eventId`, `status`, table (`schemaName`), runners, receivers, display names and the first 120 chars of `queryText`, marking this run's queries. (c) B's CloudTrail Event history, `lookup_events` paginator on `EventName=StartProtectedQuery`: time, caller ARN, `protectedQuery.id`/`status`, `sqlParameters` (`***`, redacted by AWS). **PASS** = `find_ac5_entry` finds A's `SUBMISSION` entry for the target whose `resultReceivers` includes B's account. CloudTrail is printed but never gates the result (`[WARN]` if missing). `--wait-minutes` polls every 30 s; still missing → `PENDING`, exit 3. Discovered group names go to `.state/log_groups.json`.
- **`demo_all [--no-pause] [--skip-warmup]`.** One `run_id`: (1) caller identities, `get_collaboration`, `list_members`; (2) A's rule per table next to B's `get_schema` view; warm-up gate; (3) `demo_allowed` with `--check` on; (4) `demo_blocked` with the approved outcome as control; (5) `demo_isolation`, then `demo_logging` without waiting (PENDING prints the re-run hint and is not a failure); (6) the README "Adapting Airlock…" headings. Pauses between steps unless `--no-pause`. Exits 1 if allowed, blocked or isolation fail.
- **`verify [--ac1] [--ac2] [--tags]`** (read-only; no flag = all). AC1: two members, A `ACTIVE` with abilities `[]`, B `ACTIVE` with the configured abilities, exactly one `ACTIVE` membership for the collaboration in each account. AC2: per configured table, `outputConstraints`, `joinColumns`, `dimensionColumns` and `aggregateColumns` equal the config. Tags: `cleanrooms list_tags_for_resource` (collaboration, both memberships, configured tables, associations), `s3 get_bucket_tagging` (both buckets), `iam list_role_tags` (data role); `resourcegroupstaggingapi` is informational. Tag failures warn only.
- **`ac7_config_diff --set dotted.path=value [--set …]` (AC7).** Values parsed as YAML; `tables.<key>.…` and `research.blocked_queries.<id>.…` address list items by key/id. Hashes every `*.py` (excluding `.venv/`, `cdk.out/`) and `config/airlock.yaml`, writes the modified config to a temp file and validates it, synthesizes original and modified (`AIRLOCK_CONFIG=<tmp> cdk synth -o <tmp>`), prints every changed template property by stack, logical ID and path, flags changes to create-only properties (`CREATE_ONLY` map for Collaboration, Membership and ConfiguredTableAssociation) as `REPLACEMENT (create-only) — run teardown first`, lists `--set` keys that change no template as runtime-only, re-hashes to prove nothing changed, and deletes its temp files. Never deploys and never regenerates data.

Output conventions: plain fixed-width tables and section banners, no colour; diagnostics on stderr prefixed `[INFO]`, `[WARN]`, `[ERROR]`.

## 7. Who can see what (FR11)

| Item | Institution A (Mercy, data owner) | Institution B (State Univ., analyst) |
|---|---|---|
| Raw rows in S3 / Glue tables | Yes (owns them) | No: AccessDenied (`demo_isolation`) |
| Columns not in the allowed list (age, enrollment_date, procedure, followup_date) | Yes | Not visible, even as schema |
| `patient_id` values | Yes | Never; join key only, can't be selected |
| Schema of allowed columns + analysis rule | Yes (author) | Read-only via the collaboration |
| Run queries | No (no CAN_QUERY) | Yes, only aggregate SQL the rule allows |
| Query results | No | Yes, aggregates only, in B's results bucket |
| Groups below the threshold (30 distinct patients) | Yes (own data) | Suppressed (row absent) |
| Clean Rooms analysis logs | Yes, for queries touching A's tables (query text, runner, receiver) | Yes, for queries B ran/received |
| CloudTrail StartProtectedQuery events | No (recorded in the caller's account) | Yes |
| Query compute charges | No | Yes (payer) |

`demo_isolation` renders this matrix from the config.

## 8. Testing

Unit tests (`.venv/bin/python -m pytest -q`, 153 tests) make no AWS calls; S3 is a hand-written stub.

- `test_config.py`: default config and derived names; one `ConfigError` per validation rule (dotted path checked); unknown keys; ROOT-relative path resolution from another cwd; `$AIRLOCK_CONFIG`.
- `test_analysis_rule.py`: the exact rule shape for both tables; threshold flows into `OutputConstraints`.
- `test_datagen.py`: determinism (identical sha256 and manifest), counts, ranges, ID uniqueness, Arrow types, `outcome_flag` consistency, `files` mode refusal.
- `test_datafiles.py`: layout, extra/hidden/stray files, column order and types, generator-mode hint.
- `test_stacks.py`: `Template.from_stack` on `build_app` (phase 1 and phase 2): `CreatorMemberAbilities: []`, payment flags, `ENABLED`, `SPARK`, `ScalarFunctions: []`, output constraints, `DependsOn` (Glue tables; B membership on the bucket and auto-delete resource), no association analysis rules, A membership `Retain`, B parameter pattern and outputs, both trust phases, per-resource and stack-level tags.
- `test_cleanrooms.py`: `prepare_sql`, `classify_failure`, result-key filtering and extension preference, CSV (BOM) and Parquet parsing, `fetch_results` with and without `output.s3`, nested log decoding, `find_ac5_entry` (including the live entry shape without `logStage`), `select_ac5_target`.
- `test_scripts_logic.py`: every stack-guard row and the catch-all, retry regexes and delays, blocked verdicts, the rare-class vacuous-pass guard, manifest check, `apply_set`, `diff_templates` with create-only flagging, and the `[api]` echo skipping credential calls and masking secret-named parameters.

`cdk synth` (both stacks, plus a phase-2 synth with a fixed test UUID) must succeed; a non-UUID `queryRunnerMembershipId` exits 1.

## 9. Verification (live, as executed)

| AC | Command | Observed |
|---|---|---|
| Deploy | `.venv/bin/python -m scripts.deploy` | Bootstrap (both accounts, tagged), phase 1 A, invitation visible, phase 2 B, phase 3 A (`TrustPhase 2`); AC1, AC2 and 10 tag checks PASS; re-run is a no-op. |
| AC1 | `scripts.verify --ac1`; `aws cleanrooms list-members …` | Both members `ACTIVE`: A abilities `[]` (not payer), B `[CAN_QUERY, CAN_RECEIVE_RESULTS]` (payer); one ACTIVE membership per account. |
| AC2 | `scripts.verify --ac2`; `aws cleanrooms get-configured-table-analysis-rule …` | Both tables `AGGREGATION`, `outputConstraints [{patient_id, 30, COUNT_DISTINCT}]`, `joinColumns [patient_id]`, dimensions and aggregates as configured. |
| AC3, FR6, FR8 | `scripts.demo_allowed --check` | `SUCCESS`; DPP-4 488, GLP-1 700, Metformin 1150, SGLT2 650; no Meglitinide; `PASS AC3`; CSV with header in B's bucket. |
| AC4, FR7 | `scripts.demo_blocked` | Control `SUCCESS`; (a) and (b) `FAILED`/`QUERY_FAILED` with the rule's verbatim message → `REJECTED`; (c) `SUCCESS` with 0 rows (header-only CSV); rare-class line PASS. |
| FR9 | `scripts.demo_isolation` | AccessDenied on A's S3 list/get and Glue `GetTable` from B; hidden columns absent from B's schema view; results hold aggregates only. |
| AC5, FR10 | `scripts.demo_logging --since-minutes 60 --wait-minutes 15` | A-side `SUBMISSION` entry for the approved query with `resultReceivers` = B's account ID → PASS; B's CloudTrail shows `StartProtectedQuery` for every query (including rejected ones); A's CloudTrail shows none. |
| End to end | `scripts.demo_all --no-pause` | allowed, blocked, isolation, logging all PASS, exit 0. |
| AC6 | `scripts.teardown`, then `scripts.deploy` and `scripts.demo_allowed --check` | 8/8 residual checks PASS; independent CLI checks empty in both accounts (collaborations, memberships, configured tables, stacks, buckets, Glue DB, roles); `CDKToolkit` and analysis log groups retained as documented; redeploy and approved query PASS with identical numbers. |
| AC7 | `scripts.ac7_config_diff --set privacy.min_distinct_patients=50 --set "research.question=…" --set "tables.outcomes.analysis_rule.dimension_columns=[medication_class]"` | `Minimum: 30 -> 50` on both configured tables; `DimensionColumns` change on outcomes; `research.question` runtime-only; no REPLACEMENT; py and config hashes unchanged; `cdk diff` against the deployed stacks: no differences. |

Not verified live: the `data.source: files` path (unit tests only), a deployed AC7 change (synth-level only), the concurrent-query quota, and a least-privilege run (both SSO profiles used administrator permission sets; the README's "Demo hardening only" section recommends a scoped deployer role).

## 10. Clean Rooms facts the design relies on

| Topic | Fact | Status |
|---|---|---|
| Payment | `PaymentConfiguration.QueryCompute.IsResponsible` on the creator and each member; the member who can query is the default payer; a non-querying member supplies it only when configured as payer. Creator `false`, B `true` in the collaboration; B's membership `true`; A's membership omits it. | Verified live (`queryComputePayerAccountId` = B) |
| Engine | `AnalyticsEngine: CLEAN_ROOMS_SQL \| SPARK`; Clean Rooms SQL support ended Dec 17, 2025; Spark supports aggregation rules. | Verified live |
| Aggregation grammar | `aggregation_function(column) [AS alias]` plus grouping columns; only columns as aggregate arguments; no arithmetic between aggregates; functions `SUM, SUM_DISTINCT, COUNT, COUNT_DISTINCT, AVG` (no median); a join column may be an aggregate column but not a dimension; output constraints apply even when the query doesn't reference the column, and across tables the stricter wins. | Docs; rejections verified live |
| Creator membership | Not auto-created by the collaboration; the creator can only leave by deleting the collaboration. Separate `CfnMembership` with `Retain`; the `COLLABORATION_DELETED` record can be deleted afterwards. | Verified live |
| Analysis logs | Clean Rooms creates the log groups itself after each membership is created with logging enabled. Members who query or receive results get logs for every analysis; table owners get logs for analyses that reference their tables. | Verified live (§11) |
| Membership log config | `QueryLogStatus` is required and updatable on the membership; create-only on the collaboration. `JobLogStatus` is PySpark-only, omitted. | Verified |
| Result configuration | S3 output needs `Bucket` and `ResultFormat`; `KeyPrefix`, `SingleFileOutput`, `RoleArn` optional; a member that both queries and receives results may skip the result role. Results bucket must differ from data-source buckets. | Verified live (no role) |
| Data-role trust | Confused-deputy guidance: `aws:SourceArn` with each membership ARN plus an `sts:ExternalId` statement with the query runner's membership ID, so the trust is two-phase. | Verified live (queries succeed after phase 3) |
| CFN shapes | ConfiguredTable needs `AllowedColumns` (1–100), `AnalysisMethod`, `Name`, `TableReference.Glue`; ≤1 analysis rule; aggregation needs all five lists including `ScalarFunctions`; `Minimum` 2–100000. Association needs table, membership, name, role. ConfiguredTable has no create-only properties; ConfiguredTableAssociation's are `ConfiguredTableIdentifier, Name, MembershipIdentifier`; Membership's is `CollaborationIdentifier`. | Verified (CFN schema) |
| Empty lists | `CreatorMemberAbilities: []` and `ScalarFunctions: []` are kept by the CDK L1 and accepted by the service. | Verified live |
| Worker compute | SQL queries: `CR.1X \| CR.4X`, 2..400 workers in the API; the 512-vCPU concurrent SQL quota caps one query at 128 CR.1X or 32 CR.4X. Default CR.1X × 4. | Docs |
| Concurrency | Outside `us-east-1` the default quota is 2 concurrent SQL queries per account, so queries run one at a time. | Docs (not load-tested) |
| Warm-up | AWS: queries run in the first minutes after logging is enabled may not send logs. Gate: 300 s after the newest membership `createTime` plus A's log group existing. | Docs; gate worked live |

## 11. Live findings

What the live runs showed, and how the code and docs handle it.

- **Un-aliased result columns are named `column__N`.** The first approved run succeeded with the right numbers, but the CSV header was `column__0,patient_count,…` because Spark names any output column without an explicit alias `column__0`, `column__1`, …, even a plain `o.medication_class`. Fix: alias every selected column in the SQL (`o.medication_class AS medication_class`) in the approved and rare-class queries; `demo_allowed`'s error names this cause; `demo_blocked`'s rare-class check fails instead of passing vacuously when the group column is missing; the README troubleshooting and "Your question" sections say to alias every column.
- **Result object layout.** Results land under `s3://<results-bucket>/results/collaboration=<collaboration-id>/<query-id>/` as `part-00000-<uuid>-c000.csv` plus a zero-byte `_SUCCESS` marker. The CSV has a header row. A suppressed group yields a header-only CSV and status `SUCCESS` with 0 rows. Rejected queries write nothing.
- **Spark analysis-log entries have no `logStage` field.** A **submission** entry is written per configured table touched and carries `schemaName`, `configuredTableId`, `directQueryAnalysisRuleType` and `directQueryAnalysisRulePolicy`; the **termination** entry carries the final `status`. Every entry has `eventId` (the protected query ID), `queryText`, `collaborationId`, `queryRunners`, `resultReceivers` (a bare account ID string such as `"444455556666"`), `memberSchemaMapping` and `memberDisplayNames`. `log_stage()` distinguishes the two by the presence of `status`; `find_ac5_entry` uses it. For rejected queries only submission entries were seen in the observed window.
- **Rejections are asynchronous.** `StartProtectedQuery` returns `SUBMITTED`; about 7 s later the query is `FAILED` with code `QUERY_FAILED` and a message such as `validation error at line 1, column 1: columns prohibited by aggregation analysis rule` (`note: columns [outcomes.patient_id] are prohibited in the PROJECT clause …`) or `… no aggregate functions in SELECT expression`. There is no synchronous `ValidationException`, so CloudTrail records rejected queries as normal `SUBMITTED` calls. `classify_failure` maps these to `REJECTED`; the text is printed verbatim.
- **Log groups.** Clean Rooms names the group `/aws/cleanrooms/<collaboration-id>` in each account; scripts discover it with `describe-log-groups --log-group-name-pattern <collaboration-id>`. No CloudWatch Logs resource policy was observed (`describe-resource-policies` returned `[]` in both accounts), although the README's Teardown section and teardown's "Also retained" line mention one. That wording is harmless and was left unchanged.
- **Deleted collaborations.** `get-collaboration` on a deleted collaboration returns `AccessDeniedException`, not `ResourceNotFoundException`, even for an administrator; teardown's residual check accepts either. Deleted collaborations and memberships no longer appear in any list call. `list-collaborations --member-status` accepts only `INVITED` or `ACTIVE`.
- **Latency and warm-up.** A's analysis-log entries arrived about 5 minutes after the queries; `demo_all` runs `demo_logging` without waiting, so a `PENDING` (exit 3) there is expected and `demo_logging --wait-minutes 10` resolves it. The 5-minute warm-up after membership creation is enforced before demo queries.
- **Query durations.** Successful queries took about 107–238 s; the first query on a new collaboration was the slowest (about 3.5 minutes in `SUBMITTED`). Rejections finish in about 7 s. Phase 1 of a fresh deploy takes about 77 s.
- **Create-only keys force teardown.** A member that leaves cannot rejoin, and a new A stack creates a new collaboration, so neither side can be recreated alone. Changes to account IDs, display names, member abilities, payer or query log status replace the collaboration; `ac7_config_diff` flags them.
- **Teardown order works as designed:** B → A → A's retained membership (`COLLABORATION_DELETED`, deleted cleanly, no record left). Retained on purpose: `CDKToolkit` stacks and the analysis log groups.
- **No retries were needed** in two fresh deploys: no IAM-propagation failure at association create, and reusing the bucket names about 5 minutes after teardown caused no `OperationAborted`. The retry paths remain for safety.
- **Credential hygiene in the `[api]` echo.** An early version echoed every mutating call, including botocore's internal `sso-oidc.CreateToken`, which printed truncated SSO client-secret and refresh-token values to the console (nothing reached a file). The echo is now limited to Airlock's services and masks secret-named parameters, with a unit test. After such an exposure, run `aws sso logout` and log in again.

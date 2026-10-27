# Implementation Plan

- [x] 1. Project skeleton, pinned dependencies, venv
  - Package layout: `airlock_lib/`, `stacks/`, `datagen/`, `scripts/` (each with `__init__.py`), `tests/` (no `__init__.py`)
  - `requirements.txt` with exact pins (`aws-cdk-lib==2.273.0`, `aws-cdk.cloud-assembly-schema==54.26.0`, `constructs==10.8.1`, `boto3==1.43.117`, `botocore==1.43.117`, `pyarrow==26.0.0`, `PyYAML==6.0.3`); `requirements-dev.txt` adds `pytest==9.1.1`
  - `cdk.json` (`.venv/bin/python app.py`, output `cdk.out`), `pytest.ini`, `.gitignore` (`.venv/`, `cdk.out/`, `.state/`, `data/generated/`, caches)
  - `airlock_lib/__init__.py` defines `ROOT`, the anchor for every relative config path
  - _Requirements: RR1, RR4_

- [x] 2. Config file, SQL files, config loader and validator
  - `config/airlock.yaml`: accounts (A 111122223333 / `airlock-a`, B 444455556666 / `airlock-b`; AWS documentation example IDs, replaced with your own), region `us-west-2`, collaboration (`SPARK`, logging `ENABLED`, A abilities `[]`, B `CAN_QUERY` + `CAN_RECEIVE_RESULTS`, payer analyst), storage, Glue, threshold 30, tables and rules, research question, query compute, data source
  - Approved and blocked SQL files; every selected column aliased (live fix: Spark names un-aliased columns `column__N`)
  - `airlock_lib/config.py`: frozen dataclasses, `ConfigError("<dotted.path>: <reason>")`, unknown keys rejected, description lengths, `CR.8X` excluded, no `allowed_result_receivers`, ROOT-relative path resolution, derived bucket names and payer flags
  - _Requirements: FR5, RR3, RR3a, AC7_

- [x] 3. Synthetic data generator, data-file validator, schema contract
  - `datagen/generate.py`: one seeded `random.Random`, 3000 `SYN-` patients, exact class counts (Meglitinide 12 below the threshold), deterministic Parquet and `manifest.json`; refuses to run when `data.source: files`
  - `airlock_lib/datafiles.py`: Parquet layout, column names, order and type checks against the config
  - `data/SCHEMA.md`: both tables, join key and 1:1 cardinality, exposure, threshold and expected counts, bring-your-own-files contract, synthetic-only guarantee
  - _Requirements: RR2, RR3, AC7_

- [x] 4. CDK app and both stacks
  - `stacks/analysis_rule.py`: aggregation rule from config (`ScalarFunctions: []` always present)
  - `AirlockDataOwner`: hardened data bucket + upload, Glue database/tables, collaboration inviting B, A's membership (`Retain`), data-access role with two-phase confused-deputy trust, configured tables with the `AGGREGATION` rule, associations, outputs
  - `AirlockAnalyst`: `CollaborationId` parameter, hardened results bucket, B's membership (payer, default S3 CSV result config, no result role) created after the bucket, output `JoinedCollaborationId`
  - `stacks/__init__.py::build_app` applies `Tags.of(app)` (`Project=Airlock`); `app.py` validates config, data files and the `queryRunnerMembershipId` context
  - _Requirements: FR1, FR2, FR3, FR4, FR8, RR1, RR3, AC1, AC2_

- [x] 5. Shared runtime helpers
  - `airlock_lib/output.py` (tables, banners, `[INFO]/[WARN]/[ERROR]`), `airlock_lib/state.py` (`.state/` records incl. `log_groups.json`)
  - `airlock_lib/aws.py`: per-profile sessions, SSO expiry → exit 2, `[cli]` echo, `[api]` echo limited to Airlock services with secret-named parameters masked, `cdk_bin()` (`AIRLOCK_CDK_BIN`), preflight account check
  - `airlock_lib/deployment.py`: live `describe-stacks` outputs for both stacks (IDs are never taken from `.state/`)
  - _Requirements: RR3a, RR5_

- [x] 6. Clean Rooms query, result and log helpers
  - `prepare_sql` (comment stripping, hashes), `run_protected_query` (sequential; quota retry; async `FAILED` classified `REJECTED` by the rule regex; verbatim error text; `query_log.jsonl`)
  - `fetch_results` (prefix listing, marker and zero-byte skips, extension preference, CSV/Parquet parsing), `ensure_log_warmup` (5-minute gate + log-group wait, never fatal)
  - AC5 helpers: nested log decoding, `log_stage` (live fix: Spark log entries have no `logStage`; termination = has `status`), `find_ac5_entry`, `select_ac5_target`
  - _Requirements: FR6, FR7, FR8, FR10, AC3, AC4, AC5_

- [x] 7. Deploy, teardown and verify scripts
  - `scripts/deploy.py`: preflight, data, bootstrap (untagged `CDKToolkit` left alone), stack-state guard with catch-all, phase 1 A (one retry for IAM propagation or S3 bucket-name contention), invitation wait with LEFT/REMOVED message, phase 2 B, phase 3 trust pinning, AC1/AC2 post-checks (tags warn only)
  - `scripts/teardown.py`: B first, then A, then A's retained membership; AC6 residual report by saved IDs; Clean Rooms log groups listed with delete commands, never deleted
  - `scripts/verify.py`: AC1 members, AC2 rules, tags via service-native APIs
  - _Requirements: FR1, FR2, RR1, RR5, AC1, AC2, AC6_

- [x] 8. Demo scripts
  - `demo_allowed` (approved query, client-side mean change, `--check` against A's manifest), `demo_blocked` (same-run control, then `patient_id`, `SELECT *` and the Meglitinide filter, rare-class line), `demo_isolation` (B-side AccessDenied, schema view, who-can-see-what matrix), `demo_logging` (A's analysis log + B's CloudTrail, AC5 rule, exit 3 when pending), `demo_all` (requirements §9 runbook in one run)
  - All thresholds, IDs, names and buckets rendered from config
  - _Requirements: FR6, FR7, FR8, FR9, FR10, FR11, AC3, AC4, AC5_

- [x] 9. AC7 config-diff script
  - `scripts/ac7_config_diff.py --set dotted.path=value …`: validates a temp copy of the config, synthesizes original and modified, prints every changed template property, flags create-only changes as `REPLACEMENT (create-only) — run teardown first`, lists runtime-only keys, proves `*.py` and the config are unchanged; never deploys
  - _Requirements: RR3, AC7_

- [x] 10. README
  - Prerequisites (Python 3.12, CDK CLI 2.1126.0 and the lib pin reason, SSO login, no access keys), configure, deploy phases and stack-state guard, no-console runbook with `--profile`/`--region` on every row, demo runbook with warm-up note, privacy controls (Option A, FR5 via vetted SQL, custom rule + analysis template alternative, mean not median, optional result-receiver rule), who-can-see-what matrix, logging walkthrough, teardown and RR1 caveats, adapting to your own data and governance, troubleshooting (incl. `column__N`), out of scope
  - No compliance claims: "technical controls that support privacy requirements"
  - _Requirements: FR5, FR11, RR4, RR5, AC7_

- [x] 11. Unit tests
  - `test_config`, `test_analysis_rule`, `test_datagen`, `test_datafiles`, `test_stacks` (template assertions incl. tags, trust phases, `DependsOn`), `test_cleanrooms` (incl. live log-entry shape), `test_scripts_logic` (stack guard, retry, verdicts, AC7 diff, `[api]` echo masking); no AWS calls
  - _Requirements: RR1, RR2, RR3, AC2, AC3, AC4, AC5, AC7_

- [x] 12. Synth gate and build evidence (no deployment)
  - Generator determinism (identical sha256 across runs), `cdk synth` for both stacks plus a phase-2 synth, a bad context UUID exits 1, `pytest -q` green, the AC7 three-key diff with unchanged hashes
  - _Requirements: RR1, RR2, AC7_

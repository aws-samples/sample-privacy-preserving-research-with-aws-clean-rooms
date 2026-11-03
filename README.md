# Airlock: a federated research data clean room on AWS Clean Rooms

Airlock shows two research institutions answering a shared medical-research question without either one handing over raw patient records. It uses the real AWS Clean Rooms feature set: one collaboration, two AWS accounts, configured tables with an aggregation analysis rule, protected queries, and analysis logging.

- **Institution A, Mercy Research Hospital** (data owner, collaboration creator): owns the data, configures the tables, sets the privacy rules.
- **Institution B, State University Health Analytics** (analyst): joins, runs approved queries, receives aggregate results in its own bucket, pays for query compute.

#### Note: The project name (airlock) and Institution names are fictional.

All data is **synthetic** (seeded generator, see [`data/SCHEMA.md`](data/SCHEMA.md)). Airlock demonstrates technical controls that support privacy requirements. It makes no regulatory claim, and running it does not make any institution compliant with anything.

Research question: *Among adult diabetic patients, is there an association between a prescribed medication class and improved glycemic outcomes (follow-up HbA1c), reported only as aggregates across the cohort?*

Sample terminal output from a full run is in [`docs/sample-output.md`](docs/sample-output.md).

**About the name.** Airlock is the name of this sample, not an AWS service or feature, and it isn't affiliated with other products called Airlock. An airlock is a sealed chamber between two environments: nothing passes through unless the conditions are right. Here each institution's data stays in its own AWS account, and only aggregate results that meet the data owner's rules come out, through AWS Clean Rooms. You'll see the name in the code, the config file, the CloudFormation stack names, the `Project=Airlock` tag and the default resource names; most of the resource names are config values you can change.

## Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Cost](#cost)
4. [Configure](#configure)
5. [Deploy](#deploy)
6. [Demo runbook](#demo-runbook)
7. [Privacy controls explained](#privacy-controls-explained)
8. [Who can see what](#who-can-see-what)
9. [Logging and audit walkthrough](#logging-and-audit-walkthrough)
10. [Teardown](#teardown)
11. [Adapting Airlock to your own data and governance](#adapting-airlock-to-your-own-data-and-governance)
12. [Demo hardening only](#demo-hardening-only)
13. [Troubleshooting](#troubleshooting)
14. [Out of scope](#out-of-scope)
15. [Working on this repo with Kiro](#working-on-this-repo-with-kiro)
16. [Security](#security)
17. [License](#license)

## Architecture

```
 Account A: Mercy Research Hospital (111122223333)       Account B: State University Health Analytics (444455556666)
 stack AirlockDataOwner                                 stack AirlockAnalyst
 +------------------------------------------------+     +-----------------------------------------------+
 | S3 airlock-a-data-<acct>-<region>              |     | Clean Rooms membership (joins = accepts the   |
 |   patients/patients.parquet                    |     |   invitation; CAN_QUERY, CAN_RECEIVE_RESULTS; |
 |   outcomes/outcomes.parquet                    |     |   pays for query compute; logging ENABLED)    |
 | Glue DB airlock_research: patients, outcomes   |     | S3 airlock-b-results-<acct>-<region>          |
 | IAM role DataAccessRole (Clean Rooms reads A)  |     |   results/  <- aggregate query results only   |
 | Clean Rooms collaboration (invites B)          |     +-----------------------------------------------+
 | Clean Rooms membership (A, logging ENABLED)    |                 ^  protected queries (B)
 | Configured tables airlock-patients,            |                 |  results delivered to B's bucket
 |   airlock-outcomes + AGGREGATION rule          |-----------------+
 | Table associations patients, outcomes          |   collaboration "Airlock Federated Research Clean Room"
 +------------------------------------------------+
```

Everything lives in one region (`us-west-2` by default): the collaboration, A's Glue catalog and data bucket, and B's results bucket. Every resource is tagged `Project=Airlock` (the Glue database and tables have no CloudFormation `Tags` property; the analysis log groups are created by Clean Rooms itself).

Repository layout:

| Path | What |
|---|---|
| `config/airlock.yaml` | The one file teams adapting Airlock edit |
| `app.py`, `stacks/` | CDK v2 (Python) app: `AirlockDataOwner` (A) and `AirlockAnalyst` (B) |
| `datagen/generate.py`, `data/SCHEMA.md` | Seeded synthetic data generator and the schema contract |
| `queries/approved/`, `queries/blocked/` | The vetted research query and the queries the rule blocks |
| `scripts/` | `deploy`, `teardown`, `verify`, `demo_*`, `ac7_config_diff` |
| `tests/` | Unit tests (no AWS calls) |
| `docs/sample-output.md` | Captured output of a real demo run, with timing expectations |
| `.kiro/` | Kiro spec (`specs/airlock-mvp/`) and steering (`steering/`) |
| `.state/` | Local records (query log, outputs); no secrets; IDs are always re-read live |

## Prerequisites

- **Python 3.12.** Clone the repository and create the project venv with pinned dependencies:
  ```bash
  git clone https://github.com/aws-samples/sample-privacy-preserving-research-with-aws-clean-rooms.git
  cd sample-privacy-preserving-research-with-aws-clean-rooms
  python3.12 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```
- **AWS CDK CLI 2.1126.0** (`npm install -g aws-cdk@2.1126.0`). The Python library is pinned to `aws-cdk-lib==2.273.0` because this CLI supports cloud-assembly schema 54, and aws-cdk-lib 2.274.0 needs schema 55. If `cdk synth` reports `Cloud assembly schema version mismatch`, pin `aws-cdk-lib==2.260.0` or upgrade the CLI. If `cdk` is not on your `PATH`, set `AIRLOCK_CDK_BIN=/path/to/cdk`.
- **AWS CLI v2.**
- **Two AWS IAM Identity Center (SSO) profiles**, one per account (defaults `airlock-a` and `airlock-b`; the names are config values). Sign in before deploying or demoing:
  ```bash
  aws sso login --profile airlock-a
  aws sso login --profile airlock-b
  ```
  No access keys are created or used. Credentials are never written to the code, the config or the repo.
- **Region support:** AWS Clean Rooms must be available in the configured region (it is in `us-west-2`). Outside `us-east-1` the default quota is 2 concurrent SQL queries per account, so the scripts run queries strictly one at a time.

Exit codes for every script: `0` ok, `1` failure (including config errors), `2` SSO session expired (run the printed `aws sso login` command), `3` logs still pending (`demo_logging` only).

## Cost

You are responsible for the cost of the AWS services used while running this sample. The estimate below gives an idea of what to expect. It isn't a quote: check the [AWS Clean Rooms pricing page](https://aws.amazon.com/clean-rooms/pricing/) and the [AWS Pricing Calculator](https://calculator.aws/) for your own usage.

**In short:** deployed and idle, the sample costs a few cents a month. Nearly all of the cost is Clean Rooms query compute, about **$0.26 per query** with the default config, billed to Account B (the configured query-compute payer).

The estimate uses on-demand prices in `us-west-2` from the AWS Price List API (October 2026). Prices vary by Region and change over time, and taxes, support plans and free-tier credits are not included.

| Service | Price (`us-west-2`) | How the sample uses it |
|---|---|---|
| AWS Clean Rooms Spark SQL compute | $1.92 per CRPU-hour (Clean Rooms Processing Unit), billed per second with a 60-second minimum per query. Startup and shutdown time are not charged. | Each protected query runs on `CR.1X` × 4 workers = 8 CRPUs (`query.compute` in the config) |
| Amazon S3 Standard | $0.023 per GB-month; $0.005 per 1,000 PUT/LIST and $0.004 per 10,000 GET requests | A's synthetic data (well under 1 MB), B's query results (KB) and the CDK bootstrap asset buckets |
| AWS Glue Data Catalog | $1 per 100,000 objects per month; $1 per 1,000,000 requests | One database and two tables in Account A |
| Amazon CloudWatch Logs | $0.50 per GB ingested; $0.03 per GB-month stored | Clean Rooms analysis logs (a few KB per query) and deploy logs |
| AWS Lambda | $0.20 per 1M requests; $0.0000166667 per GB-second | CDK helper functions that run only during deploy and teardown |
| Clean Rooms collaboration, memberships, configured tables and analysis rules, IAM, CloudFormation, CloudTrail event history | No charge | |

**Per query:** 8 CRPUs × 60 seconds = 0.133 CRPU-hours × $1.92 = **$0.256**. With the synthetic dataset, every query uses well under 60 seconds of compute (the approved query uses about 16 seconds), so each one is billed at the minimum. The 2–4 minutes you wait for a result is mostly service startup, which isn't billed. A query that needs more than 60 seconds of compute is billed per second at $15.36 per hour (8 CRPUs × $1.92).

**Monthly estimate:**

| Scenario | Queries per month | Clean Rooms compute | Everything else | Total |
|---|---|---|---|---|
| Deployed, no queries | 0 | $0.00 | < $0.05 | **< $0.05** |
| One full demo run (`scripts.demo_all`) | 2 successful + 2 rejected | $0.51–$1.02 | < $0.05 | **~$1** |
| One demo run a week | 8 successful + 8 rejected | $2.05–$4.10 | < $0.05 | **~$2–4** |
| Regular use: 10 approved queries a working day | 220 | $56.32 | < $0.10 | **~$56** |

The ranges depend on whether queries rejected by the analysis rule are billed. They fail validation within seconds, and this estimate shows both $0 and the 60-second minimum. Running `scripts.demo_allowed` and `scripts.demo_blocked` separately instead of `scripts.demo_all` adds one more approved query, because `demo_blocked` then runs its own control query.

**What changes the cost:**

- **Worker count.** Compute scales with CRPUs. The Clean Rooms default of 32 CRPUs per query would cost about $1.02 per query. Keep `query.compute` small for small datasets.
- **Number of queries.** Every query pays at least the 60-second minimum, so fewer queries cost less than many small ones.
- **Data volume.** Larger real datasets can push queries past the 60-second minimum.
- **Differential privacy.** Not used in this sample. If you add it, it's billed as a separate per-CRPU-hour charge on top of Spark SQL compute.

**Keeping costs down:** run [Teardown](#teardown) when you no longer need the environment, and consider an [AWS Budgets](https://aws.amazon.com/aws-cost-management/aws-budgets/) alert on Account B, since it pays for every query. A few things remain after teardown: the `CDKToolkit` bootstrap stacks (their S3 asset buckets cost cents per month) and the CloudWatch Logs groups described in [Teardown](#teardown) (KB of data).

## Configure

Edit `config/airlock.yaml`. Every relative path in it resolves against the repository root, never the current directory. Unknown keys are rejected. Use `--config PATH` or `AIRLOCK_CONFIG=PATH` to point at another file.

**Set your own account IDs first.** The committed IDs `111122223333` (A) and `444455556666` (B) are AWS documentation examples. Until you replace them, `preflight` fails closed: deploy and every live script exit 1 because your profiles resolve to different accounts, before anything is created. To keep your real IDs out of the repo, put a private copy of the config outside it and point at it with `AIRLOCK_CONFIG=/abs/path.yaml` (or `--config /abs/path.yaml`); only the paths inside the config must stay under the repository root. When you preview an account change with `ac7_config_diff --set`, quote the ID so it stays a string, for example `--set 'accounts.analyst.account_id="123456789012"'`.

| Key | Meaning |
|---|---|
| `project.tags` | Tags on every resource and on both CloudFormation stacks |
| `region` | One region for everything |
| `accounts.data_owner` / `accounts.analyst` | `account_id` (quoted, 12 digits), SSO `profile`, `display_name`, `stack_name` |
| `collaboration.name`, `.description` | Collaboration name (1–100 chars) and description (1–255, not blank) |
| `collaboration.analytics_engine` | Fixed to `SPARK` (Clean Rooms SQL support ended Dec 17, 2025) |
| `collaboration.query_log_status` | `ENABLED`: analysis logging on the collaboration and both memberships |
| `collaboration.creator_member_abilities` | Fixed to `[]` in the MVP: A contributes data only |
| `collaboration.analyst_member_abilities` | `[CAN_QUERY, CAN_RECEIVE_RESULTS]` (must include `CAN_QUERY`) |
| `collaboration.query_compute_payer` | Fixed to `analyst` in the MVP |
| `storage.*` | Bucket name prefixes (bucket = `<prefix>-<account_id>-<region>`), result key prefix, `CSV` or `PARQUET`, single-file output |
| `glue.database_name` | A's Glue database |
| `privacy.identity_column` | The column counted by the minimum-threshold constraint (`patient_id`) |
| `privacy.min_distinct_patients` | **The threshold knob** (2..100000; default 30) |
| `security.pin_data_role_trust_to_query_runner` | Deploy phase 3 pins the data-role trust policy to B's membership ID |
| `logging.warmup_seconds`, `.log_group_wait_seconds` | Analysis-log warm-up gate before demo queries |
| `tables[*]` | Glue schema, S3 prefix, configured-table and association names, allowed columns, and the aggregation rule (join / dimension / aggregate columns) |
| `research.*` | The question, the approved SQL file, result column names, the optional client-side difference, and the blocked-query list |
| `query.*` | Worker type/count (`CR.1X` or `CR.4X`; `CR.8X` is deliberately excluded), poll interval, timeout |
| `data.*` | `source: generator` (seeded synthetic data) or `files` (your Parquet files), and the generator parameters |

## Deploy

From the repository root:

```bash
.venv/bin/python -m scripts.deploy
```

What it does (every subprocess is echoed as `[cli] …` and every mutating API call as `[api] …`, so the output is also a CLI record of what ran). It calls `cdk deploy` with `--require-approval never`, so CDK doesn't stop to confirm IAM or security changes; see [Demo hardening only](#demo-hardening-only).

1. **Preflight:** validates the config and checks that each profile resolves to the configured account.
2. **Data:** regenerates the seeded data (deterministic, so the asset hash is stable) and validates the Parquet layout and schema.
3. **Bootstrap:** runs `cdk bootstrap aws://<acct>/<region> --profile <p> --tags Project=Airlock` in each account when `CDKToolkit` is absent or Airlock-tagged. A pre-existing, untagged `CDKToolkit` is used as-is and never modified.
4. **Stack-state guard:** deletes `ROLLBACK_COMPLETE` stacks first (B, then A, cleaning up A's retained membership record), then:

   | A stack | B stack | Action |
   |---|---|---|
   | absent | absent | fresh deploy |
   | healthy (`CREATE_COMPLETE`, `UPDATE_COMPLETE`, `UPDATE_ROLLBACK_COMPLETE`) | absent | update A, then deploy B |
   | healthy | healthy, joined to A's collaboration | update everything (idempotent) |
   | healthy | joined to a different collaboration | exit 1: run teardown first |
   | absent, `DELETE_*` or `ROLLBACK_*` | exists | exit 1: run teardown first |
   | any `*_IN_PROGRESS` | any | exit 1: retry later |
   | any other combination | | exit 1, printing both statuses and the newest failed events: run teardown, then deploy |
5. **Phase 1 (A):** `cdk deploy AirlockDataOwner`: bucket and data, Glue, collaboration (inviting B), A's membership, data-access role, configured tables with the aggregation rule, associations. On a fresh create that fails for an IAM-propagation or S3 bucket-name contention reason, it cleans up and retries once.
6. **Invitation:** waits until B can see the invitation.
7. **Phase 2 (B):** `cdk deploy AirlockAnalyst --parameters AirlockAnalyst:CollaborationId=<id>`: the results bucket, then B's membership (creating it is accepting the invitation).
8. **Phase 3 (A):** redeploys A with `-c queryRunnerMembershipId=<B membership>`, which changes only the data-role trust policy (confused-deputy pinning).
9. **Post-checks:** AC1 (both members active), AC2 (rules in place), and tags. An AC1/AC2 failure exits 1; a tag mismatch only warns. It prints the IDs and the UTC time after which demo queries will run (the log warm-up).

Re-running deploy on a healthy environment changes nothing.

### No-console runbook (CLI equivalents)

Each console step has an IaC resource and a CLI equivalent. Values shown are the defaults from `config/airlock.yaml`.

| Console step | IaC | CLI equivalent |
|---|---|---|
| A creates the collaboration and invites B | `CfnCollaboration` | `aws cleanrooms create-collaboration --name "Airlock Federated Research Clean Room" --description "Synthetic-data demo of cross-institution research analysis with AWS Clean Rooms." --creator-display-name "Mercy Research Hospital" --creator-member-abilities '[]' --members '[{"accountId":"444455556666","displayName":"State University Health Analytics","memberAbilities":["CAN_QUERY","CAN_RECEIVE_RESULTS"],"paymentConfiguration":{"queryCompute":{"isResponsible":true}}}]' --creator-payment-configuration '{"queryCompute":{"isResponsible":false}}' --query-log-status ENABLED --analytics-engine SPARK --tags Project=Airlock --profile airlock-a --region us-west-2` |
| A joins its own collaboration | `CfnMembership` (A) | `aws cleanrooms create-membership --collaboration-identifier <collab-id> --query-log-status ENABLED --tags Project=Airlock --profile airlock-a --region us-west-2` |
| A configures a table | `CfnConfiguredTable` | `aws cleanrooms create-configured-table --name airlock-outcomes --table-reference '{"glue":{"databaseName":"airlock_research","tableName":"outcomes"}}' --allowed-columns patient_id medication_class outcome_flag followup_hba1c --analysis-method DIRECT_QUERY --tags Project=Airlock --profile airlock-a --region us-west-2` (same for `airlock-patients`) |
| A adds the aggregation rule | `AnalysisRules` on `CfnConfiguredTable` | `aws cleanrooms create-configured-table-analysis-rule --configured-table-identifier <ct-id> --analysis-rule-type AGGREGATION --analysis-rule-policy file://rule.json --profile airlock-a --region us-west-2` (`rule.json` below) |
| A associates the table | `CfnConfiguredTableAssociation` | `aws cleanrooms create-configured-table-association --name outcomes --configured-table-identifier <ct-id> --membership-identifier <a-membership-id> --role-arn <data-access-role-arn> --tags Project=Airlock --profile airlock-a --region us-west-2` |
| B sees the invitation | — | `aws cleanrooms list-collaborations --member-status INVITED --profile airlock-b --region us-west-2` |
| B accepts (joins) | `CfnMembership` (B) | `aws cleanrooms create-membership --collaboration-identifier <collab-id> --query-log-status ENABLED --payment-configuration '{"queryCompute":{"isResponsible":true}}' --default-result-configuration '{"outputConfiguration":{"s3":{"bucket":"airlock-b-results-444455556666-us-west-2","keyPrefix":"results/","resultFormat":"CSV","singleFileOutput":true}}}' --tags Project=Airlock --profile airlock-b --region us-west-2` |
| B runs a query | `scripts.demo_allowed` | `aws cleanrooms start-protected-query --type SQL --membership-identifier <b-membership-id> --sql-parameters file://q.json --compute-configuration '{"worker":{"type":"CR.1X","number":4}}' --profile airlock-b --region us-west-2`, then `aws cleanrooms get-protected-query --membership-identifier <b-membership-id> --protected-query-identifier <query-id> --profile airlock-b --region us-west-2` (`q.json` = `{"queryString": "<SQL>"}`) |
| B leaves / A deletes | `scripts.teardown` | `aws cleanrooms delete-membership --membership-identifier <b-membership-id> --profile airlock-b --region us-west-2`, then `aws cleanrooms delete-collaboration --collaboration-identifier <collab-id> --profile airlock-a --region us-west-2` |

`rule.json` for `airlock-outcomes` (the `patients` rule uses `baseline_hba1c` and the dimensions `sex`, `region`, `primary_diagnosis`):

```json
{"v1": {"aggregation": {
  "aggregateColumns": [
    {"columnNames": ["patient_id"], "function": "COUNT_DISTINCT"},
    {"columnNames": ["patient_id"], "function": "COUNT"},
    {"columnNames": ["followup_hba1c"], "function": "AVG"}],
  "joinColumns": ["patient_id"],
  "dimensionColumns": ["medication_class", "outcome_flag"],
  "scalarFunctions": [],
  "outputConstraints": [{"columnName": "patient_id", "minimum": 30, "type": "COUNT_DISTINCT"}]}}}
```

## Demo runbook

**Before step 3:** run `deploy` well before you present the demo. AWS notes that queries run in the first few minutes after a membership enables analysis logging may not send logs. The demo scripts wait until `logging.warmup_seconds` (5 minutes) after the newest membership was created and until A's analysis log group exists, showing a countdown. On an environment that has been up for a while they start at once. `--skip-warmup` bypasses the wait (with a warning).

Run everything in one go, pausing between steps:

```bash
.venv/bin/python -m scripts.demo_all            # add --no-pause for an unattended run
```

Or step by step:

| # | Command | What to say | Expected output |
|---|---|---|---|
| 1 | `demo_all` step 1 (or `aws cleanrooms list-members --collaboration-identifier <id> --profile airlock-a --region us-west-2`) | Two institutions, two AWS accounts, one collaboration. A contributes data; B can query and receive results and pays for compute. | Both members `ACTIVE`; A abilities none, B `CAN_QUERY, CAN_RECEIVE_RESULTS`. |
| 2 | `demo_all` step 2 | A's guardrails: only some columns are exposed, `patient_id` is join-only, only `COUNT`/`COUNT DISTINCT`/`AVG` are allowed, and every output row needs ≥ 30 distinct patients. B sees the schema and the rule, never rows. | A's rule JSON per table next to B's schema-only view. |
| 3 | `.venv/bin/python -m scripts.demo_allowed --check` | B runs the vetted research query. | `SUCCESS`; a table of `medication_class`, `patient_count`, `mean_baseline_hba1c`, `mean_followup_hba1c`, `mean_change (computed client-side)` for DPP-4 (488), GLP-1 (700), Metformin (1150), SGLT2 (650). No Meglitinide row. `PASS AC3` against A's manifest. Synthetic data; per-class differences are generator parameters, not clinical findings. |
| 4 | `.venv/bin/python -m scripts.demo_blocked` | B tries to go around the rule. | The approved control succeeds first. (a) selecting `patient_id` and (b) `SELECT *` are `REJECTED` with the service's verbatim message; (c) filtering to Meglitinide returns 0 rows. `Meglitinide: 12 patients in Mercy Research Hospital's source (A-side manifest) → 0 rows visible to State University Health Analytics`. All `PASS`. |
| 5a | `.venv/bin/python -m scripts.demo_isolation` | Even outside Clean Rooms, B has no path to A's data. | From B's profile: `AccessDenied` on A's bucket list and object read, Glue table not accessible; B's schema view lacks `age`, `enrollment_date`, `procedure`, `followup_date`; B's results bucket holds aggregates only. Then the who-can-see-what matrix. |
| 5b | `.venv/bin/python -m scripts.demo_logging --wait-minutes 10` | A audits every analysis that touched its tables; CloudTrail records B's calls. | A's analysis-log entries (query text, runner, result receiver) with `PASS AC5`; B's CloudTrail `StartProtectedQuery` events, including the rejected ones. |
| 6 | Open this README at [Adapting Airlock](#adapting-airlock-to-your-own-data-and-governance) | The same pattern works for your organization's data and rules by editing one config file. | `scripts.ac7_config_diff` preview (below). |

Queries always run one at a time.

[`docs/sample-output.md`](docs/sample-output.md) shows the captured output of a full run (allowed, blocked, isolation, logging) and how long each step takes.

## Privacy controls explained

- **Allowed columns** (configured table): only the listed columns exist for the analyst. `age`, `enrollment_date`, `procedure` and `followup_date` are not exposed at all.
- **Aggregation analysis rule** (on both configured tables):
  - `patient_id` is a **join column** only: it can join the two tables and be counted, but never appear in the output.
  - **Dimension columns** (`medication_class`, `outcome_flag`, `sex`, `region`, `primary_diagnosis`) are the only GROUP BY / filter columns.
  - **Aggregate functions** are limited to `COUNT`, `COUNT DISTINCT` and `AVG` on specific columns. Arithmetic between aggregates is not allowed, so the mean change is computed client-side from the two means.
  - The **minimum aggregation constraint** requires every output row to cover ≥ `privacy.min_distinct_patients` distinct `patient_id` values. Clean Rooms enforces it even if the query never mentions `patient_id`; rows below it are suppressed.
- **FR5, the approved query:** the vetted research question is a SQL file in `queries/approved/`, reviewed and checked in by the data owner, and run by `scripts.demo_allowed`. The aggregation rule is what actually enforces the limits on any query B writes.
- **Alternative pattern:** teams that want "only pre-approved SQL can run at all" can use a **custom analysis rule plus an analysis template** instead. The custom rule now supports `aggregationThresholds` and `disallowedOutputColumns`. Airlock does not use it because analysis templates only work with the custom rule, and the requirements asked for the aggregation rule.
- **Median** is not an allowed aggregation function, so the demo reports the mean.
- **Optional result-receiver restriction (not used by the MVP):** A can also restrict who may receive results that use its table with an association analysis rule, for example `aws cleanrooms create-configured-table-association-analysis-rule --membership-identifier <a-membership-id> --configured-table-association-identifier <association-id> --analysis-rule-type AGGREGATION --analysis-rule-policy '{"v1":{"aggregation":{"allowedResultReceivers":["444455556666"]}}}' --profile airlock-a --region us-west-2`. In this MVP B is the only member that can receive results anyway.
- **Data-access role:** Clean Rooms reads A's Glue tables and S3 objects through a role in A whose trust policy follows the AWS confused-deputy guidance. After B joins, deploy phase 3 pins it to B's membership ID.

## Who can see what

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

`demo_isolation` prints this matrix rendered from the config.

## Logging and audit walkthrough

- **A's analysis log is the data owner's audit trail.** Analysis logging is enabled on the collaboration and on both memberships. Clean Rooms creates the CloudWatch Logs groups itself after each membership is created; the group is named `/aws/cleanrooms/<collaboration-id>` in each account, so `demo_logging` finds it with `describe-log-groups --log-group-name-pattern <collaboration-id>` and saves the names to `.state/log_groups.json`. Each query produces entries with `eventId` (the protected query ID), `queryText`, `queryRunners`, `resultReceivers` and `memberDisplayNames`. There is no `logStage` field: a **submission** entry is written per configured table touched (it carries `schemaName`, `configuredTableId` and the analysis-rule policy applied), and a **termination** entry carries the final `status`.
- **AC5 rule:** `demo_logging` targets the latest successful approved/control query and passes when A's log has a submission entry for it whose `resultReceivers` include B's account ID.
- **CloudTrail:** `StartProtectedQuery` is a management event recorded in the **caller's** account (B), one per query, including the rejected ones. The rule check runs after submission, so a rejected query is a normal `SUBMITTED` call in CloudTrail; its rejection shows as `FAILED` in `get-protected-query`. The SQL text is redacted (`***`). It is printed for evidence; A's audit record is the analysis log.
- **Latency:** log entries and CloudTrail events can take several minutes to arrive. `--wait-minutes N` polls every 30 s; exit `3` means "not delivered yet", not a failure.

## Teardown

```bash
.venv/bin/python -m scripts.teardown
```

It saves the IDs to `.state/last-teardown.json`, deletes **B first** (`aws cloudformation delete-stack`; B's membership is deleted, which is B leaving, and the results bucket is emptied), then A (associations, configured tables, collaboration, role, Glue objects, data bucket). A's own membership is retained by CloudFormation (the creator can only leave by deleting the collaboration), so teardown deletes that leftover record afterwards. It finishes with a residual report (AC6) that checks by the saved IDs: collaboration gone, no ACTIVE membership in either account, no Airlock configured tables, both stacks and both buckets gone. It never synthesizes and never touches your data files.

What remains on purpose:

- The `CDKToolkit` bootstrap stacks.
- The CloudWatch Logs groups Clean Rooms created for analysis logs (audit evidence) and its Logs resource policy. Teardown lists the groups and prints one delete command per group, for example `aws logs delete-log-group --log-group-name <name> --profile airlock-a --region us-west-2`; it never deletes them itself.
- Possibly a `COLLABORATION_DELETED` membership record in A, if the service refuses to delete it (reported as `[WARN]`).

Teardown also leaves the Lambda log groups of the CDK auto-delete-objects provider; see [Demo hardening only](#demo-hardening-only).

If a stack ends `DELETE_FAILED`, teardown prints the failed resources and the `aws cloudformation delete-stack --retain-resources …` command for you to review and run.

**Caveats (RR1):**

- Neither side can be recreated on its own against an existing collaboration. A member that leaves can't rejoin, and a new A stack creates a new collaboration. Run teardown, then deploy.
- These config keys change create-only collaboration properties, so changing them **replaces the collaboration**; run teardown first: `accounts.*.account_id`, `accounts.*.display_name`, `collaboration.query_log_status`, `collaboration.creator_member_abilities`, `collaboration.analyst_member_abilities`, `collaboration.query_compute_payer`. `scripts.ac7_config_diff` flags them as `REPLACEMENT (create-only) — run teardown first`.

## Adapting Airlock to your own data and governance

Everything you change to adapt Airlock to your organization is in `config/airlock.yaml`; no code changes are needed.

### Your accounts

Set `accounts.*.account_id`, `profile`, `display_name` and `stack_name`, and `region`. Bucket names derive from the prefixes plus account and region.

### Your data

1. Set `data.source: files` and `data.files_dir: data/customer/s3` (anything outside `data/generated/`; the generator is then disabled, so your files are never overwritten).
2. Put Parquet files under `<files_dir>/<table.s3_prefix>`, for example `data/customer/s3/patients/part-0.parquet`.
3. Edit `tables[*].columns` to match the files; `cdk synth` and `deploy` validate names, order and types. See [`data/SCHEMA.md`](data/SCHEMA.md#bring-your-own-files-datasource-files).
4. Synthetic data only.

### Your privacy rules

- `tables[*].allowed_columns`: what the analyst may see at all.
- `tables[*].analysis_rule`: join columns, dimension (GROUP BY) columns, aggregate columns and functions.
- `privacy.identity_column` and `privacy.min_distinct_patients`: the minimum distinct count per output row.

### Your question

Rewrite `research.question` and the SQL files under `queries/`, then set `research.group_by_column`, `count_column` and the optional `derived_difference` to match your SQL aliases. Alias every selected column, including plain dimension columns: Clean Rooms names un-aliased output columns `column__0`, `column__1`, … The demo prints whatever columns come back. Update `research.blocked_queries` with the queries your rule should stop.

### Preview the change (AC7)

```bash
.venv/bin/python -m scripts.ac7_config_diff \
  --set privacy.min_distinct_patients=50 \
  --set "research.question=Among adult diabetic patients, does mean follow-up HbA1c differ by medication class?" \
  --set "tables.outcomes.analysis_rule.dimension_columns=[medication_class]"
```

It synthesizes the original and a modified temporary copy of the config and prints every template property that changes (for example `… OutputConstraints.0.Minimum: 30 -> 50`), lists keys that only the demo scripts use (`research.question`), flags create-only changes, and proves that no code or config file changed. Nothing is deployed. Then run `deploy` (or teardown, then deploy, if a create-only property changed).

### Governance checklist

- Who at the data owner approves changes to allowed columns, the analysis rule and the threshold, and how they are reviewed (the config file and SQL files are code-reviewable).
- Who may run queries and who receives results (`analyst_member_abilities`; optionally an association rule limiting result receivers).
- Who pays for query compute (`query_compute_payer`).
- How long analysis logs are kept, and who reviews them.

## Demo hardening only

Airlock is hardened for a synthetic-data demo, not for real data. Review these settings before you adapt it:

- **S3 access logs and versioning are off** on both buckets (unversioned, no server access logging). Public access is blocked, TLS is enforced and object ownership is `BucketOwnerEnforced`. Turn on access logging and versioning for real data.
- **Encryption is SSE-S3**, not SSE-KMS. Switch to SSE-KMS with a key you control if your data requires it.
- **`scripts.deploy` uses `--require-approval never`** for every `cdk deploy`, so security-sensitive changes such as IAM policy and trust changes deploy without CDK's confirmation prompt. Review the synthesized templates (or a read-only `cdk diff`, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#testing)) before you deploy a changed stack.
- **The demo used administrator permission sets** for both SSO profiles. Use a scoped deployer role that covers only what the two stacks and their CDK bootstrap need.
- **Some log groups remain after teardown.** The Lambda log groups of the CDK auto-delete-objects provider (one per stack, `/aws/lambda/…`, no retention set) are not removed, and teardown doesn't list them. The Clean Rooms analysis log groups (`/aws/cleanrooms/<collaboration-id>`) are kept as audit evidence; teardown prints a delete command for each one.
- **Deploy phase 3 (trust pinning) is required.** Keep `security.pin_data_role_trust_to_query_runner: true` and let deploy finish phase 3. Without it, the data-access role keeps its phase-1 trust policy, which is broad: its two Allow statements are alternatives, so the `sts:ExternalId` statement alone lets Clean Rooms assume the role for any `dbuser` in the region, and the `aws:SourceArn` restriction no longer limits it.

## Troubleshooting

- **Exit 2 / "SSO session expired":** run the printed `aws sso login --profile <p>` and re-run.
- **`Cloud assembly schema version mismatch`:** see the CDK pin note in [Prerequisites](#prerequisites).
- **First deploy fails on the table associations with an "unable to assume role" / not-authorized reason:** IAM propagation. Deploy deletes the rolled-back stack, cleans the retained membership and retries once after 30 s.
- **S3 `OperationAborted` / "A conflicting conditional operation is currently in progress" right after teardown:** the bucket name is still being released. Deploy retries once after 60 s; otherwise wait a minute and re-run.
- **"Institution B left this collaboration and cannot rejoin it":** run teardown, then deploy.
- **`result header ['column__0', …] has no 'medication_class' column`:** Clean Rooms (Spark) names every un-aliased output column `column__0`, `column__1`, …, even a plain column such as `o.medication_class`. Alias every selected column in your SQL (`o.medication_class AS medication_class`), as the approved query does.
- **`no result rows parsed`:** the result object could not be parsed. Set `storage.result_format: PARQUET` and re-run deploy (only B's membership updates; Parquet always carries column names).
- **Countdown before queries:** the log warm-up gate; it is expected on a fresh deploy.
- **`demo_logging` exits 3 (PENDING):** logs have not arrived yet. Re-run `demo_logging --wait-minutes 10`. If it is still pending, run `demo_allowed` once more and re-run `demo_logging --since-minutes 30 --wait-minutes 15`.

## Out of scope

The stretch goals are not built: two data owners in one joint analysis (SG1), differential privacy (SG2), Cryptographic Computing for Clean Rooms (SG3), a second research scenario (SG4) and Clean Rooms ML (SG5). The environment is a demo, not production-hardened (no multi-region, DR or cost optimization).

## Working on this repo with Kiro

The repo carries its Kiro context under `.kiro/`:

- [`.kiro/specs/airlock-mvp/`](.kiro/specs/airlock-mvp/): `requirements.md` (the requirements plus the implementation decisions), `design.md` (the design as built, including what the live runs showed) and `tasks.md` (the completed implementation plan). Start a new feature as a new spec next to it.
- [`.kiro/steering/`](.kiro/steering/): always-included guidance for agents: `product.md` (the privacy story and non-negotiables), `tech.md` (stack, pins, commands, Clean Rooms constraints), `structure.md` (layout, config as the single source of truth) and `aws-safety.md` (SSO profiles, Airlock-owned resources only, teardown order, create-only keys).

Local Kiro settings (`.kiro/settings/`, e.g. MCP configuration) are git-ignored.
For the project's ground rules, setup, conventions and testing, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). For the general contribution process, see [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

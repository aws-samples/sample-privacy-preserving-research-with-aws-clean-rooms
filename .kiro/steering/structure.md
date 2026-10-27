---
inclusion: always
---

# Repository structure

Paths are relative to the repository root.

| Path | Responsibility |
|---|---|
| `config/airlock.yaml` | The single source of truth for accounts, profiles, region, names, tables, rules, threshold, question, query compute and data source |
| `app.py` | CDK entry: load and validate config and data files, validate the `queryRunnerMembershipId` context, call `build_app`, synth |
| `stacks/__init__.py` | `build_app(cfg, app, runner)`: the one code path for synth and tests; applies `Tags.of(app)` |
| `stacks/data_owner_stack.py` | `AirlockDataOwner` (A): data bucket + upload, Glue, collaboration, A's membership (Retain), data-access role, configured tables, associations |
| `stacks/analyst_stack.py` | `AirlockAnalyst` (B): `CollaborationId` parameter, results bucket, B's membership |
| `stacks/analysis_rule.py` | Pure function: table + privacy config → aggregation rule |
| `airlock_lib/config.py` | YAML → frozen dataclasses, validation (`ConfigError("<dotted.path>: …")`), derived names |
| `airlock_lib/aws.py` | Per-profile boto3 sessions, SSO-expiry → exit 2, `[cli]`/`[api]` echo, preflight |
| `airlock_lib/deployment.py` | Live `describe-stacks` outputs: the authoritative source of every runtime ID |
| `airlock_lib/cleanrooms.py` | Protected-query runner, result fetch/parse, log warm-up, analysis-log helpers |
| `airlock_lib/datafiles.py`, `state.py`, `output.py` | Data-file validation and manifest; `.state/` records; tables, banners, `[INFO]/[WARN]/[ERROR]` |
| `datagen/generate.py`, `data/SCHEMA.md` | Seeded synthetic data generator and the schema contract |
| `queries/approved/`, `queries/blocked/` | The vetted research SQL and the queries the rule must block or limit |
| `scripts/` | `deploy`, `teardown`, `verify`, `demo_*`, `ac7_config_diff` (each `python -m scripts.<name>`) |
| `tests/` | Unit tests; no AWS calls (S3 is a stub) |
| `docs/sample-output.md` | Captured output of a real demo run |
| `.kiro/specs/airlock-mvp/` | Requirements, design, tasks |

## Rules

- **Retarget via config, not code (AC7, RR3).** No account IDs, profiles, region, bucket names, thresholds or display names as literals in `*.py`; scripts render everything they print from the config. A new knob means: add it to `config/airlock.yaml`, to the dataclasses and validation in `config.py` (unknown keys are rejected), and to tests and the README "Configure" table.
- **Relative config paths resolve against the repository root** (`airlock_lib.ROOT`, `config.resolve()`), never the current directory or the config file's location.
- **Runtime-only, git-ignored, never commit:** `.state/` (records only; IDs are always re-read live), `data/generated/` (owned by the generator and wiped on every run; your own files go elsewhere via `data.source: files` and a `files_dir` outside `data/generated/`, for example the git-ignored `data/customer/`), `cdk.out/`, `.venv/`, `.kiro/settings/`.
- **Keep construct IDs stable.** Renaming a construct changes its logical ID, which replaces the resource and breaks `ac7_config_diff` comparisons.
- **Keep decision logic testable:** pure, module-level functions (`decide_action`, `retry_delay`, `blocked_verdict`, `check_against_manifest`, `find_ac5_entry`, `select_ac5_target`, `apply_set`, `diff_templates`) with unit tests in `tests/`.
- Match the existing style: `from __future__ import annotations`, type hints, small functions, diagnostics via `airlock_lib.output`, exits via `aws.run_main`.

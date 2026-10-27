# Developing Airlock

Airlock is a repeatable two-account demo of AWS Clean Rooms. Two research institutions answer a shared medical-research question over synthetic data, and the analyst only ever receives aggregates. It is a CDK v2 (Python) app plus scripts that deploy, demo and tear it down.

This guide covers the project's ground rules, setup, conventions and testing. For the general contribution process (issues, pull requests, security notifications, licensing), see [CONTRIBUTING.md](../CONTRIBUTING.md).

Contributions are welcome from:

- Teams adapting Airlock to their own accounts, data, privacy rules or research question.
- Anyone building one of the stretch goals SG1 to SG5 in [`.kiro/specs/airlock-mvp/requirements.md`](../.kiro/specs/airlock-mvp/requirements.md): two data owners, differential privacy, Cryptographic Computing for Clean Rooms, a second research scenario, Clean Rooms ML. None of them is built yet.

## Ground rules

These are non-negotiable. A pull request that breaks one won't be merged.

- **Synthetic data only.** Never commit real patient data or any other real data from your organization. That includes `data.source: files` mode, test fixtures and captured output.
- **No compliance claims.** Don't write that Airlock, or anything run with it, is HIPAA, GDPR or otherwise compliant. Describe features as technical controls that support privacy requirements.
- **Stay close to the real AWS Clean Rooms feature set.** Privacy enforcement comes from Clean Rooms: analysis rules, allowed columns, memberships, result delivery and analysis logging. Client-side code may display, compare or do presentation math on the results the service returns. It must not imitate or re-implement enforcement.
- **Never commit credentials.** Use SSO named profiles only (`aws sso login --profile <p>`). Never create or use access keys. Never print SSO tokens or credential values: keep the `[api]` echo masking in `airlock_lib/aws.py`, which `test_api_echo_skips_credential_calls_and_masks_secrets` guards. Before you commit captured output, replace SSO role ARNs and user names, `accessKeyId`, `sourceIPAddress` and ephemeral resource IDs with placeholders.
- **Tag every AWS resource `Project=Airlock`.** The tags come from `project.tags` in the config and are applied with `Tags.of(app)`. The only exceptions are the documented ones: the Glue database and tables (no CloudFormation `Tags` property) and the log groups Clean Rooms creates itself.
- **Only create, modify or delete resources Airlock owns:** the two config-named stacks and what they create, and an Airlock-tagged `CDKToolkit`. Leave everything else alone, including a pre-existing untagged `CDKToolkit`.
- **Treat the AWS accounts you configure as potentially production.** Use only accounts you're authorized to use. The IDs committed in `config/airlock.yaml` are AWS documentation examples; set your own (see the README [Configure](../README.md#configure) section).

## Development setup

Prerequisites (details in the README under [Prerequisites](../README.md#prerequisites)):

- Python 3.12.
- Node.js and the AWS CDK CLI 2.1126.0 (`npm install -g aws-cdk@2.1126.0`). If `cdk` is not on your `PATH`, set `AIRLOCK_CDK_BIN=/path/to/cdk`.
- AWS CLI v2.
- Two IAM Identity Center (SSO) profiles, one per account (defaults `airlock-a` and `airlock-b`). You only need them for commands that call AWS. Unit tests and `cdk synth` don't.

Clone the repository, create the venv and generate the seeded synthetic data (synth validates the data files, so it needs them):

```bash
git clone https://github.com/aws-samples/sample-privacy-preserving-research-with-aws-clean-rooms.git
cd sample-privacy-preserving-research-with-aws-clean-rooms
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m datagen.generate
```

Always use the venv (`.venv/bin/python`, `.venv/bin/pip`). `cdk.json` runs `.venv/bin/python app.py`.

For deploying, running the demo and tearing down, follow the README: [Deploy](../README.md#deploy), [Demo runbook](../README.md#demo-runbook) and [Teardown](../README.md#teardown).

## Making changes

- **Branch from `main` (or fork the repository) and open a GitHub pull request.** Don't push directly to `main`, except for trivial fixes the maintainers agree to.
- **Keep behavior config-driven.** `config/airlock.yaml` is the single source of truth for accounts, profiles, region, names, tables, rules, threshold, question, query compute and data source. Retargeting a dataset or question must stay a config change, not a code change (AC7). Don't put account IDs, profiles, regions, bucket names, thresholds or display names as literals in `*.py`. A new knob goes into `config/airlock.yaml`, the dataclasses and validation in `airlock_lib/config.py`, the tests and the README [Configure](../README.md#configure) table. The loader rejects unknown keys and reports errors as `ConfigError("<dotted.path>: …")`.
- **Relative config paths resolve against the repository root** (`airlock_lib.ROOT`, via `config.resolve()`), never against the current directory or the config file's location.
- **Pin new dependencies to exact versions** (`==`): runtime ones in `requirements.txt`, dev-only ones in `requirements-dev.txt`. Don't bump `aws-cdk-lib` past 2.273.0 without upgrading the CDK CLI (see the pin note in the README Prerequisites).
- **Match the existing style:** `from __future__ import annotations`, type hints and small functions. Keep decision logic in pure, module-level functions so it can be unit-tested. Print diagnostics through `airlock_lib.output` and exit through `aws.run_main`. Keep CDK construct IDs stable, because renaming one replaces the resource. Use inclusive language (allowlist/denylist, primary/replica).
- **Don't commit runtime files.** `.state/`, `data/generated/`, `data/customer/`, `cdk.out/`, `.venv/` and `.kiro/settings/` are git-ignored.
- **Add the MIT-0 (MIT No Attribution) license header** to every new tracked `.py`, `.sql`, `.yaml`, `requirements*.txt`, `.ini` or `.gitignore`-style file. Put it at line 1 (right after the shebang, if there is one), followed by a blank line:

  ```
  # Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
  # SPDX-License-Identifier: MIT-0
  ```

  In SQL, start each line with `-- ` instead of `# `. `prepare_sql` drops full-line `--` comments, so the header is never sent to Clean Rooms. Markdown and JSON files don't get the header, and neither does `LICENSE`.
- **Create-only Clean Rooms settings need a teardown, then a deploy.** Changing `accounts.*.account_id`, `accounts.*.display_name`, `collaboration.creator_member_abilities`, `collaboration.analyst_member_abilities`, `collaboration.query_compute_payer` or `collaboration.query_log_status` replaces the collaboration. See the caveats (RR1) in the README [Teardown](../README.md#teardown) section. `scripts.ac7_config_diff` flags these changes as `REPLACEMENT (create-only) — run teardown first`.

### Changes to the privacy guardrails

The approved research query lives in `queries/approved/`. The queries the rule must stop live in `queries/blocked/` and are listed in `research.blocked_queries`. The analysis rule (`tables[*].allowed_columns`, `tables[*].analysis_rule`) and the threshold (`privacy.identity_column`, `privacy.min_distinct_patients`) are the privacy guardrails, so reviewers check changes to them closely:

- **Threshold:** the pull request states the old and new value and who at the data owner approved it, and includes the `ac7_config_diff` output that shows the `OutputConstraints` change. Lowering it needs a stated reason.
- **Analysis rule:** every newly exposed column is justified. The identifier stays a join column only and never becomes a dimension or an output column. Aggregate functions stay limited to what the question needs. The aggregation rule allows no median and no arithmetic between aggregates, so derived values are computed client-side.
- **Approved query:** it is aggregate-only SQL that the rule allows, every selected column is aliased (Spark names un-aliased columns `column__0`, `column__1`, …), and the aliases match `research.group_by_column`, `research.count_column` and `research.derived_difference`.
- **Blocked queries:** after a rule change, each one still ends with its `expect` value (`REJECTED` or `NO_ROWS`). A newly found way around the rule gets a new blocked query.

The README [Governance checklist](../README.md#governance-checklist) lists the approval questions a data owner should answer for these changes.

## Testing

Do all of this before every commit and pull request.

1. **Add or update unit tests** in `tests/` for any change under `airlock_lib/`, `stacks/`, `scripts/`, `datagen/` or `app.py`. Tests are grouped by area: `test_config.py`, `test_analysis_rule.py`, `test_datagen.py`, `test_datafiles.py`, `test_stacks.py` (template assertions), `test_cleanrooms.py` and `test_scripts_logic.py` (the scripts' decision logic). They share the fixtures in `tests/conftest.py`: `cfg` is the loaded config, and `raw` is a copy of the raw config dict you can modify. Unit tests make no AWS calls. Keep it that way.
2. **Run the tests.** Everything must pass.

   ```bash
   .venv/bin/python -m pytest -q
   ```

3. **Synthesize both stacks.** This writes the `AirlockDataOwner` and `AirlockAnalyst` templates to `cdk.out/` and validates the config and the data files on the way. If you change the data-access role trust, also synth the deploy phase 3 variant by adding `-c queryRunnerMembershipId=00000000-0000-0000-0000-000000000000`.

   ```bash
   cdk synth --quiet                    # or "$AIRLOCK_CDK_BIN" synth --quiet
   ```

4. **Review the template diff for infra changes.**
   - Config changes: preview them with `.venv/bin/python -m scripts.ac7_config_diff --set dotted.path=value …` before you edit the file. See [Preview the change (AC7)](../README.md#preview-the-change-ac7) in the README. It synthesizes locally, flags create-only changes and deploys nothing. Paste its output into the pull request.
   - Code changes under `stacks/`: compare against the deployed stacks with the read-only `cdk diff <stack> --no-change-set --profile <profile> --region <region>`, and only in accounts you're authorized to use. For `AirlockDataOwner`, add `-c queryRunnerMembershipId=<B's membership ID>`, or the phase 3 trust pin shows up as a change.
5. **Verify live only when the change affects AWS behavior.** Deploy, then run `scripts.verify` and `scripts.demo_all` (commands in the README). Do this only in accounts you're authorized to use. Demo queries cost the analyst account money and must run one at a time, so never run two demo scripts at once. Run `scripts.teardown` afterwards unless the environment is meant to persist. Deploy and tear down only with `scripts.deploy` and `scripts.teardown`, not with `cdk deploy` or `cdk destroy`. The scripts enforce the A → B → A deploy order and the B-first teardown order.

## Working with Kiro

Kiro picks up the repo's context from `.kiro/` when you open the repo:

- **Spec:** [`.kiro/specs/airlock-mvp/`](../.kiro/specs/airlock-mvp/) holds `requirements.md`, `design.md` (the design as built) and `tasks.md` (the completed plan).
- **Steering:** the four files in [`.kiro/steering/`](../.kiro/steering/) (`product.md`, `tech.md`, `structure.md`, `aws-safety.md`) use `inclusion: always`, so every Kiro session follows them.

To add a feature such as a stretch goal, create a new spec folder `.kiro/specs/<feature>/` next to the MVP spec, with its own `requirements.md`, `design.md` and `tasks.md`. When a convention, pin, command or safety rule changes, update the matching steering file in the same pull request, so agents don't follow stale guidance.

## Commits and pull requests

- Write short, imperative commit subjects, for example `Add CONTRIBUTING guide`.
- Keep each commit and pull request to one topic. Leave unrelated changes, such as reformatting or drive-by refactors, out.
- The pull request description covers:
  - what changed and why;
  - how you tested it: `pytest`, `cdk synth`, any `ac7_config_diff` or `cdk diff` output, and any live checks you ran;
  - create-only or teardown implications: whether the change needs a teardown, then a deploy. A redeploy or teardown changes every runtime ID and restarts the 5-minute log warm-up.
- Redact captured output before you paste it (see the credentials rule above).

## Reporting bugs and requesting features

Use this repository's [GitHub Issues](https://github.com/aws-samples/sample-privacy-preserving-research-with-aws-clean-rooms/issues). For a bug, include:

- the command you ran;
- its output and exit code, with secrets and SSO user names redacted;
- the config values you changed from the defaults in `config/airlock.yaml`;
- the region.

For a feature request, describe the use case and say whether it maps to a stretch goal (SG1 to SG5).

## Security issues

If you find a potential security issue, don't report it in a public GitHub issue or pull request. Follow [Security issue notifications](../CONTRIBUTING.md#security-issue-notifications) in CONTRIBUTING.md (also summarized in [SECURITY.md](../SECURITY.md)).

## Code of conduct

This project follows the [Amazon Open Source Code of Conduct](../CODE_OF_CONDUCT.md).

## License

Airlock is licensed under MIT-0 (MIT No Attribution); see [LICENSE](../LICENSE). By contributing, you agree that your contributions are licensed under the same license.

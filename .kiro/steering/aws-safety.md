---
inclusion: always
---

# AWS safety

Treat the accounts in `config/airlock.yaml` as potentially production, and use only accounts you're authorized to use. The committed IDs (A `111122223333`, B `444455556666`) are AWS documentation examples; `preflight` fails closed until they are replaced with real ones.

## Credentials

- Use the **SSO named profiles only** (`airlock-a`, `airlock-b`, from `accounts.*.profile`). Sign in with `aws sso login --profile <p>`; an expired session makes scripts exit 2.
- **Never** create access keys, and never write credentials, tokens or `~/.aws` contents into code, config, `.state/`, docs, tests or commits. Before committing captured output, replace SSO role ARNs (`AWSReservedSSO_<permission-set>_<hash>/<user>`), `accessKeyId`, `sourceIPAddress` and ephemeral resource IDs with placeholders.
- **Never print SSO tokens or credential values.** The `[api]` echo in `airlock_lib/aws.py` only covers Airlock's services (`cleanrooms, s3, cloudformation, glue, iam, logs, cloudtrail`) and prints `***` for any parameter whose name contains `secret`, `token`, `password` or `credential`. Keep it that way; `test_api_echo_skips_credential_calls_and_masks_secrets` guards it. Don't run commands such as `aws configure export-credentials` or dump SSO cache files.
- Pass `--profile` and `--region` explicitly on every CLI call; never export `AWS_PROFILE` globally.

## What may be touched

- **Only Airlock-owned resources:** the two config-named stacks and what they create, memberships identified from Airlock stack outputs or stack events, and an Airlock-tagged `CDKToolkit`. Never modify a pre-existing untagged `CDKToolkit`, IAM Identity Center, account settings, CloudTrail trails or anything else not created by Airlock.
- **Tag everything `Project=Airlock`** (`Tags.of(app)` plus stack tags). Documented exceptions: Glue database/tables (no CFN `Tags`) and the log groups Clean Rooms creates.
- Clean Rooms analysis log groups are audit evidence: list them and print `aws logs delete-log-group …` commands; never delete them automatically.

## Before any mutating action

- Prefer read-only calls (`scripts.verify`, `describe-*`, `list-*`, `get-*`, `cdk synth`, `cdk diff --no-change-set`).
- **Ask the user first** before `scripts.deploy`, `scripts.teardown`, any demo script (protected queries cost B money and write to B's bucket), `cdk deploy`, `cdk bootstrap`, or any `create/update/delete/start` call. State what it changes and whether it is reversible. A redeploy or teardown changes every ID and restarts the 5-minute log warm-up.
- Don't disable safety protections (deletion protection, versioning, retention, bucket public-access blocks).

## Rebuild rules

- **Teardown order is fixed:** B first (B's membership is deleted, which is B leaving), then A, then A's retained `COLLABORATION_DELETED` membership record. `scripts.teardown` does this with `aws cloudformation delete-stack`; don't use `cdk destroy` or delete stacks out of order.
- **Create-only keys require teardown, then deploy:** `accounts.*.account_id`, `accounts.*.display_name`, `collaboration.analyst_member_abilities`, `collaboration.creator_member_abilities`, `collaboration.query_compute_payer`, `collaboration.query_log_status`. Changing them replaces the collaboration. Run `scripts.ac7_config_diff --set …` first; it flags them as `REPLACEMENT (create-only) — run teardown first`.
- A member that left cannot rejoin, and a new A stack creates a new collaboration, so neither side can be recreated on its own.
- If a stack ends `DELETE_FAILED`, show the user the `--retain-resources` command teardown prints; don't run it without approval.

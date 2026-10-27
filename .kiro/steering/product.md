---
inclusion: always
---

# Product: Airlock

Airlock is a repeatable demo of cross-institution research on **AWS Clean Rooms**. Two research institutions answer a shared medical-research question without either one handing over raw patient records. Spec: `.kiro/specs/airlock-mvp/`.

## Who is involved

- **Institution A, Mercy Research Hospital** (Account A, example ID 111122223333 in the committed config, profile `airlock-a`): data owner and collaboration creator. Owns the S3 data and Glue catalog, configures the tables, sets the aggregation analysis rule, vets the approved SQL. Contributes data only (no member abilities).
- **Institution B, State University Health Analytics** (Account B, example ID 444455556666 in the committed config, profile `airlock-b`): analyst. Joins the collaboration, is the only query runner and result receiver (results land in B's own bucket), and pays for query compute. Never sees rows.
- Both institution names are fictional, and the account IDs in the repo are AWS documentation examples that users replace with their own.

Research question: among adult diabetic patients, is a prescribed medication class associated with improved follow-up HbA1c, reported only as aggregates?

## The privacy story (allow vs block)

- **Allowed:** the vetted query (`queries/approved/`) returns per-class patient counts and mean baseline/follow-up HbA1c for DPP-4 (488), GLP-1 (700), Metformin (1150) and SGLT2 (650). The mean change is computed client-side. The data is synthetic, so per-class differences are generator parameters, not clinical findings.
- **Blocked:** selecting `patient_id` (join-only column) and `SELECT *` (no aggregate) are rejected by Clean Rooms with its own error text.
- **Limited:** the rare class Meglitinide (12 patients, below the 30-distinct-patient threshold) is suppressed: 0 rows.
- **Isolation and audit:** B gets AccessDenied on A's S3 and Glue; A's analysis log records every query, its runner and result receiver; B's CloudTrail records `StartProtectedQuery`. A who-can-see-what matrix summarizes it.
- **Repeatable:** teams adapting Airlock retarget accounts, data, rules, threshold and question by editing `config/airlock.yaml`, not code.

## Non-negotiables

- **Synthetic data only.** The seeded generator produces `SYN-` IDs and no names, addresses, birth dates, MRNs or free text. Never introduce real patient data, including in `data.source: files` mode or test fixtures.
- **No compliance claims.** Never write that Airlock (or anything run with it) is HIPAA, GDPR or otherwise compliant. Frame everything as *technical controls that support privacy requirements*. The only allowed mentions are the existing disclaimers.
- **Stay close to the real Clean Rooms feature set.** Privacy enforcement comes from Clean Rooms (analysis rules, configured-table allowed columns, memberships, result delivery, analysis logging). Do not imitate or re-implement enforcement client-side; client-side code may only display, compare or do presentation math on results the service returned.
- **MVP scope.** Stretch goals (two data owners, differential privacy, C3R, a second scenario, Clean Rooms ML) are not built. Don't add them unless asked.
- Use inclusive language (allowlist/denylist, primary/replica).

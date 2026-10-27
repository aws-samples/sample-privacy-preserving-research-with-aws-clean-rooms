# Sample output

What a demo run looks like. Everything below was captured from real runs in `us-west-2` (`scripts.demo_all --no-pause`, the individual demo scripts, and the AWS CLI cross-checks). Output is trimmed (`…` marks a cut).

- Account IDs are replaced with AWS documentation example IDs: 111122223333 is Institution A (Mercy Research Hospital), 444455556666 is Institution B (State University Health Analytics). Bucket names and ARNs that contain an account ID use the same example IDs. Institution names are fictional.
- Identifiers that change on every deploy or query are replaced with placeholders: `<collaboration-id>`, `<a-membership-id>`, `<b-membership-id>`, `<configured-table-id>`, `<query-id>`, `<result-object>`. SSO identities are shown as `AWSReservedSSO_<permission-set>_<hash>/<user>`.
- The data is seeded synthetic data, so the numbers are the same on every deploy.

## 1. Approved query (FR6, AC3)

`.venv/bin/python -m scripts.demo_allowed --check` (also step 3 of `demo_all`):

```
[INFO] analysis log group(s) present in Account A: /aws/cleanrooms/<collaboration-id>

==============================================================================
 Allowed: State University Health Analytics runs the approved query
==============================================================================
  Research question: Among adult diabetic patients, is there an association between a prescribed medication class and improved glycemic outcomes (follow-up HbA1c), reported only as aggregates across the cohort?
[api] cleanrooms.start_protected_query(type=SQL, membershipIdentifier=<b-membership-id>, sqlParameters={'queryString': 'SELECT\n  o.medication_class           AS medication_class,\..., computeConfiguration={'worker': {'type': 'CR.1X', 'number': 4}})
[INFO] protected query <query-id> started (approved)
[INFO]   status SUBMITTED after 1 s
[INFO]   status STARTED after 101 s
[INFO]   status SUCCESS after 117 s
  SQL file            …/airlock/queries/approved/mean_hba1c_by_medication_class.sql
  …
  SQL sent (full-line comments stripped):
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
  protected query ID  <query-id>
  status              SUCCESS  (117.3 s)
  result location     s3://airlock-b-results-444455556666-us-west-2/results/collaboration=<collaboration-id>/<query-id>/
  result keys         ['results/collaboration=<collaboration-id>/<query-id>/<result-object>.csv']

medication_class  patient_count  mean_baseline_hba1c  mean_followup_hba1c  mean_change (computed client-side)
----------------  -------------  -------------------  -------------------  ----------------------------------
DPP-4                       488                 8.50                 7.96                               -0.54
GLP-1                       700                 8.45                 7.08                               -1.37
Metformin                  1150                 8.50                 7.39                               -1.11
SGLT2                       650                 8.44                 7.64                               -0.79

  Groups with fewer than 30 distinct patient_id values are suppressed by Mercy Research Hospital's rule.
  PASS  AC3: groups, patient_count and mean_change match the data owner's manifest (A-side ground truth)
```

| medication_class | patients | mean baseline HbA1c | mean follow-up HbA1c | mean change (client-side) |
|---|---|---|---|---|
| DPP-4 | 488 | 8.50 | 7.96 | −0.54 |
| GLP-1 | 700 | 8.45 | 7.08 | −1.37 |
| Metformin | 1150 | 8.50 | 7.39 | −1.11 |
| SGLT2 | 650 | 8.44 | 7.64 | −0.79 |

Synthetic data; per-class differences are generator parameters, not clinical findings.

There is no Meglitinide row: that class has 12 patients, below the threshold of 30. Clean Rooms returns only the two means; the mean change is computed by the script because the aggregation rule does not allow arithmetic between aggregates.

The result object in B's bucket, read with B's profile (FR8):

```
$ aws s3 ls s3://airlock-b-results-444455556666-us-west-2/results/collaboration=<collaboration-id>/<query-id>/ --profile airlock-b --region us-west-2
2026-10-21 22:36:39          0 _SUCCESS
2026-10-21 22:36:39        262 <result-object>.csv
$ aws s3 cp s3://airlock-b-results-444455556666-us-west-2/results/collaboration=<collaboration-id>/<query-id>/<result-object>.csv - --profile airlock-b --region us-west-2
medication_class,patient_count,mean_baseline_hba1c,mean_followup_hba1c
DPP-4,488,8.501639344262294,7.9622950819672145
GLP-1,700,8.449857142857137,7.081142857142861
Metformin,1150,8.501739130434782,7.3943478260869675
SGLT2,650,8.435692307692303,7.644461538461534
```

## 2. Blocked and limited queries (FR7, AC4)

`.venv/bin/python -m scripts.demo_blocked` (step 4 of `demo_all`). The approved query runs first as a control; only when it succeeds with rows do the rejections below mean "blocked by the rule". Standalone, the script runs its own control query; inside `demo_all` it reuses the approved query from the same run.

```
==============================================================================
 Blocked / limited: State University Health Analytics tries to go around Mercy Research Hospital's rule
==============================================================================
  Control: approved query <query-id> from this run: SUCCESS, 4 row(s)

[a] Select patient_id (identifier)   (expect: REJECTED)
[INFO] protected query <query-id> started (blocked:select_patient_id)
[INFO]   status SUBMITTED after 2 s
[INFO]   status FAILED after 7 s
  SQL sent:
    SELECT patient_id, AVG(followup_hba1c) AS mean_followup_hba1c
    FROM outcomes
    GROUP BY patient_id
  REJECTED: QUERY_FAILED: validation error at line 1, column 1: columns prohibited by aggregation analysis rule

note: columns [outcomes.patient_id] are prohibited in the PROJECT clause by outcomes's aggregation analysis rule
  PASS: rejected by the analysis rule

[b] Row-level SELECT *   (expect: REJECTED)
[INFO] protected query <query-id> started (blocked:row_level_select)
[INFO]   status SUBMITTED after 2 s
[INFO]   status FAILED after 7 s
  SQL sent:
    SELECT * FROM patients
  REJECTED: QUERY_FAILED: validation error at line 1, column 1: no aggregate functions in SELECT expression

note: SELECT clauses must contain at least one aggregate function when querying tables configured with aggregation analysis rules
  PASS: rejected by the analysis rule

[c] Small group below threshold   (expect: NO_ROWS)
[INFO] protected query <query-id> started (blocked:rare_class_filter)
[INFO]   status SUBMITTED after 2 s
[INFO]   status STARTED after 133 s
[INFO]   status SUCCESS after 144 s
  SQL sent:
    SELECT o.medication_class           AS medication_class,
           COUNT(DISTINCT o.patient_id) AS patient_count,
           AVG(o.followup_hba1c)        AS mean_followup_hba1c
    FROM outcomes o
    WHERE o.medication_class = 'Meglitinide'
    GROUP BY o.medication_class
  SUCCESS: 0 rows  (protected query <query-id>)
  PASS: suppressed (0 rows returned)

  PASS  Meglitinide: 12 patients in Mercy Research Hospital's source (A-side manifest) → 0 rows visible to State University Health Analytics

  All blocked/limited checks PASS
```

The rejection text comes from Clean Rooms and is printed verbatim. The service's own record of a rejected query:

```
$ aws cleanrooms get-protected-query --membership-identifier <b-membership-id> --protected-query-identifier <query-id> --profile airlock-b --region us-west-2 --query 'protectedQuery.[id,status,error]'
["<query-id>", "FAILED",
 {"message": "validation error at line 1, column 1: columns prohibited by aggregation analysis rule\n\nnote: columns [outcomes.patient_id] are prohibited in the PROJECT clause by outcomes's aggregation analysis rule", "code": "QUERY_FAILED"}]
```

The suppressed Meglitinide query (c) succeeds, but its result file holds only the header:

```
$ aws s3 cp s3://airlock-b-results-444455556666-us-west-2/results/collaboration=<collaboration-id>/<query-id>/<result-object>.csv - --profile airlock-b --region us-west-2
medication_class,patient_count,mean_followup_hba1c
```

## 3. Isolation (FR9)

`.venv/bin/python -m scripts.demo_isolation`. Every call uses B's own SSO profile:

```
==============================================================================
 Isolation (FR9): State University Health Analytics (444455556666, profile airlock-b) vs Mercy Research Hospital's data
==============================================================================
  PASS  B lists s3://airlock-a-data-111122223333-us-west-2: AccessDenied: User: arn:aws:sts::444455556666:assumed-role/AWSReservedSSO_<permission-set>_<hash>/<user> is not authorized to perform: s3:ListBucket on resource: "arn:aws:s3:::airlock-a-data-111122223333-us-west-2" because no resource-based policy allows the s3:ListBucket action
  PASS  B reads s3://airlock-a-data-111122223333-us-west-2/patients/patients.parquet: AccessDenied: User: arn:aws:sts::444455556666:assumed-role/… is not authorized to perform: s3:GetObject on resource: … because no resource-based policy allows the s3:GetObject action
  PASS  B reads Glue table 111122223333:airlock_research.patients: not accessible (AccessDeniedException: User: arn:aws:sts::444455556666:assumed-role/… is not authorized to perform: glue:GetTable on resource: arn:aws:glue:us-west-2:111122223333:catalog because no resource-based policy allows the glue:GetTable action)

  PASS  B's schema view of patients: ['patient_id', 'sex', 'region', 'primary_diagnosis', 'baseline_hba1c']: hidden columns absent: ['age', 'enrollment_date']
        rule (read-only for B): outputConstraints=[{"columnName": "patient_id", "minimum": 30, "type": "COUNT_DISTINCT"}] joinColumns=['patient_id'] dimensionColumns=['sex', 'region', 'primary_diagnosis']
  PASS  B's schema view of outcomes: ['patient_id', 'medication_class', 'followup_hba1c', 'outcome_flag']: hidden columns absent: ['procedure', 'followup_date']
  …
  PASS  latest approved result <query-id> holds aggregates only: columns ['medication_class', 'patient_count', 'mean_baseline_hba1c', 'mean_followup_hba1c']
  …
```

The script then prints the who-can-see-what matrix (the same table as the README's [Who can see what](../README.md#who-can-see-what) section).

## 4. Logging and audit (FR10, AC5)

`.venv/bin/python -m scripts.demo_logging --wait-minutes 10`. `(a)` is A's analysis log, the data owner's audit trail; `(c)` is B's CloudTrail Event history. `*` marks queries from the current run:

```
==============================================================================
 Logging (FR10/AC5): run 20261022T024818Z, target query <query-id>
==============================================================================

(a) Mercy Research Hospital (111122223333) analysis log groups: ['/aws/cleanrooms/<collaboration-id>']
 …
 * 1792637302519  SUBMISSION  eventId=<query-id>  status=None  table=outcomes
      queryRunners=444455556666  resultReceivers=444455556666
      memberDisplayNames={"111122223333": "Mercy Research Hospital"}
      queryText: SELECT o.medication_class AS medication_class, COUNT(DISTINCT p.patient_id) AS patient_count, AVG(p.baseline_hba1c) A...
 * 1792637302519  SUBMISSION  eventId=<query-id>  status=None  table=patients
 …
 * 1792637416121  TERMINATION  eventId=<query-id>  status=SUCCESS
 …
 * 1792637420450  SUBMISSION  eventId=<query-id>  status=None  table=outcomes
      queryText: SELECT patient_id, AVG(followup_hba1c) AS mean_followup_hba1c FROM outcomes GROUP BY patient_id
 * 1792637427398  SUBMISSION  eventId=<query-id>  status=None  table=patients
      queryText: SELECT * FROM patients
 …
(c) State University Health Analytics (444455556666) CloudTrail Event history: StartProtectedQuery
 * 2026-10-22T02:48:22Z  arn:aws:sts::444455556666:assumed-role/AWSReservedSSO_<permission-set>_<hash>/<user>
      protectedQuery.id=<query-id>  status=SUBMITTED  sqlParameters=***
 * 2026-10-22T02:50:20Z  arn:aws:sts::444455556666:assumed-role/AWSReservedSSO_<permission-set>_<hash>/<user>
      protectedQuery.id=<query-id>  status=SUBMITTED  sqlParameters=***
 …
  StartProtectedQuery is recorded in the caller's account (B), not in A's; A's audit record is (a).

  PASS  AC5: Mercy Research Hospital's analysis log has a SUBMISSION entry for <query-id>; resultReceivers=444455556666 includes 444455556666 (State University Health Analytics)
```

The rejected queries (a) and (b) also appear: a submission entry with their query text in A's log, and a normal `SUBMITTED` call in B's CloudTrail (the rejection happens after submission).

A-side analysis-log **submission** entry (one per configured table the query touches), from `aws logs filter-log-events --log-group-name /aws/cleanrooms/<collaboration-id> --filter-pattern '"<query-id>"' --profile airlock-a --region us-west-2`, decoded and trimmed:

```json
{
  "eventTimestamp": "1792636689809",
  "eventId": "<query-id>",
  "queryText": "SELECT\n  o.medication_class           AS medication_class,\n  COUNT(DISTINCT p.patient_id) AS patient_count, …",
  "collaborationId": "<collaboration-id>",
  "schemaName": "outcomes",
  "configuredTableId": "<configured-table-id>",
  "directQueryAnalysisRuleType": "AGGREGATION",
  "directQueryAnalysisRulePolicy": {"v1": {"aggregation": {
    "joinColumns": ["patient_id"],
    "dimensionColumns": ["medication_class", "outcome_flag"],
    "outputConstraints": [{"columnName": "patient_id", "minimum": 30, "type": "COUNT_DISTINCT"}], …}}},
  "resultReceivers": "444455556666",
  "queryRunners": "444455556666",
  "memberSchemaMapping": {"111122223333": "outcomes, patients"},
  "memberDisplayNames": {"111122223333": "Mercy Research Hospital"}
}
```

There is no `logStage` field. The **termination** entry for the same query has no `schemaName`/`configuredTableId`/rule policy and instead carries the final status: `"status": "SUCCESS"`.

B-side CloudTrail `StartProtectedQuery` entry, from `aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=StartProtectedQuery --max-results 1 --profile airlock-b --region us-west-2`, trimmed:

```json
{
  "userIdentity": {
    "type": "AssumedRole",
    "arn": "arn:aws:sts::444455556666:assumed-role/AWSReservedSSO_<permission-set>_<hash>/<user>",
    "accountId": "444455556666", …
  },
  "eventTime": "2026-10-22T02:40:12Z",
  "eventSource": "cleanrooms.amazonaws.com",
  "eventName": "StartProtectedQuery",
  "awsRegion": "us-west-2",
  "requestParameters": {
    "computeConfiguration": {"worker": {"type": "CR.1X", "number": 4}},
    "sqlParameters": "***",
    "membershipIdentifier": "<b-membership-id>",
    "type": "SQL"
  },
  "responseElements": {"protectedQuery": {
    "id": "<query-id>",
    "membershipId": "<b-membership-id>",
    "queryComputePayerAccountId": "444455556666",
    "resultConfiguration": {"outputConfiguration": {"s3": {
      "bucket": "airlock-b-results-444455556666-us-west-2",
      "keyPrefix": "results/collaboration=<collaboration-id>",
      "resultFormat": "CSV", "singleFileOutput": true}}},
    "sqlParameters": "***",
    "status": "SUBMITTED", …}},
  "managementEvent": true,
  "recipientAccountId": "444455556666"
}
```

AWS redacts the SQL text (`***`) in CloudTrail. The same lookup in Account A returns no events: `StartProtectedQuery` is recorded only in the caller's account.

## 5. Timing expectations

| What | Typical time | Notes |
|---|---|---|
| Log warm-up after a deploy | 5 minutes after the newest membership was created | The demo scripts wait and show a countdown; on a long-lived environment they start at once. |
| Successful query (approved, control, rare class) | about 2–4 minutes (107–238 s observed) | The first query on a new collaboration is the slowest (about 3.5 minutes in `SUBMITTED`). |
| Rejected query | about 7 s | Arrives asynchronously: `SUBMITTED`, then `FAILED` with code `QUERY_FAILED`. |
| Analysis-log entries in A's log | about 5 minutes after the query | `demo_all` doesn't wait, so its logging step may show `PENDING`; run `demo_logging --wait-minutes 10`. |
| CloudTrail event in B's Event history | usually within minutes; can take up to ~15 minutes | Printed for evidence; never gates the AC5 result. |
| Full `demo_all --no-pause` | about 5 minutes of queries | The recorded run's queries ran from 02:48:18Z to 02:52:57Z. |

Queries always run one at a time (the default quota outside `us-east-1` is 2 concurrent queries per account), so don't run two demo scripts at once.

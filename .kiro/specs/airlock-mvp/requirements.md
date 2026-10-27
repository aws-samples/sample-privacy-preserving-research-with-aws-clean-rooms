# Airlock: Federated Research Data Clean Room
## Requirements (MVP)

These are Airlock's own requirements for the MVP. [`design.md`](design.md), [`tasks.md`](tasks.md), the README and the code refer to the IDs below (G, NG, FR, RR, AC, SG, C), so keep them stable when you edit this file.

---

## 1. Overview

Airlock demonstrates how two research institutions can work on a shared medical-research question with **AWS Clean Rooms** when neither may give the other its raw patient records. The data is **synthetic**. The analyst only ever gets aggregate answers, the underlying rows stay with their owner, and the data owner decides the privacy rules.

Airlock uses Clean Rooms features as they exist (collaborations, memberships, configured tables, analysis rules, protected queries, result delivery and analysis logging). It does not imitate or re-implement the service.

## 2. Goals and non-goals

### 2.1 Goals
- **G1, a collaboration that works:** one Clean Rooms collaboration with two members whose roles don't overlap.
- **G2, a privacy story you can see:** at least one query that the analysis rules permit and at least one that they reject or restrict. The contrast between the two is the point of the demo.
- **G3, one clear research question:** a clinically plausible question with an easy-to-read aggregate answer.
- **G4, a security and logging walkthrough:** show what each member can and can't see, and how queries are logged.
- **G5, repeatable:** code and configuration rebuild the whole environment, so teams adapting Airlock can stand it up in their own accounts and point it at their own data and governance rules.

### 2.2 Non-goals
- **NG1:** using real patient data. Every dataset is synthetic.
- **NG2:** claiming regulatory compliance (HIPAA, GDPR or any other). Airlock demonstrates *technical controls that support* privacy requirements; running it doesn't make an institution "compliant".
- **NG3:** a pattern in which two data owners combine their tables. The MVP leaves it to the stretch goals.
- **NG4:** production hardening such as multi-region, disaster recovery or cost optimization. The scope is a demo.

## 3. Collaboration pattern

One institution owns the data; the other analyzes it.

| Role | Institution | What it does |
|------|-------------|--------------|
| **Data owner** | Institution A ("Mercy Research Hospital") | Provides both datasets, configures the tables, writes the analysis rules and decides which questions may be asked. |
| **Analyst** | Institution B ("State University Health Analytics") | Queries the collaboration with permitted SQL and receives aggregate results. Individual rows are never visible to it. |

The institution names are fictional.

Each institution works from **an AWS account it owns**, which is how Clean Rooms is normally deployed: the collaboration spans both accounts, and every member joins from the account it owns.

- **Account A** (data owner): source data in S3, the Glue catalog, the configured tables and their analysis rules. It creates the collaboration and sends B the invitation.
- **Account B** (analyst): accepts the invitation by joining, runs the permitted queries and receives results in an S3 bucket it owns.

**Deployment authentication:** each account has its own named profile in **AWS IAM Identity Center (SSO)**. Each stack is deployed with the matching profile (for example `--profile airlock-a` and `--profile airlock-b`). Long-lived access keys are never created or stored, and the profile names are configuration values.

## 4. Research question

> **"Among adult diabetic patients, is there an association between a prescribed medication class and improved glycemic outcomes (follow-up HbA1c), reported only as aggregates across the cohort?"**

- Kept small on purpose: one join (patients to outcomes), one grouping column (medication class) and a few aggregates (patient counts and the mean HbA1c change; see the implementation decisions for why there is no median).
- The answer is an aggregate table that a privacy rule can restrict, for example by suppressing groups with too few patients.

## 5. Data requirements (synthetic)

Airlock uses two synthetic datasets, produced by a seeded, scripted generator (RR2). **Real patient data is never used.**

### 5.1 Dataset 1: patients / clinical (Institution A)
| Column | Type | Notes |
|--------|------|-------|
| `patient_id` | string | Synthetic identifier; the join key |
| `age` | int | 18 or older |
| `sex` | string | M, F or Other |
| `region` | string | Broad geographic area only |
| `primary_diagnosis` | string | For example, Type 2 Diabetes |
| `baseline_hba1c` | float | Percent |
| `enrollment_date` | date | |

### 5.2 Dataset 2: treatment / outcomes (Institution A in the MVP)
| Column | Type | Notes |
|--------|------|-------|
| `patient_id` | string | References Dataset 1 |
| `medication_class` | string | For example Metformin, SGLT2, GLP-1 |
| `procedure` | string | May be "None" |
| `followup_hba1c` | float | Percent |
| `followup_date` | date | |
| `outcome_flag` | string | improved, stable or worsened |

- Both tables share the synthetic `patient_id` as their join key.
- Settle the schemas and the join key before building anything: the question determines the columns, and the columns determine which privacy controls are useful.
- A few thousand synthetic rows are enough.

## 6. Functional requirements

### 6.1 Clean Rooms collaboration
- **FR1:** Account A creates a Clean Rooms **collaboration** and invites Account B using B's 12-digit account ID, giving two **members**: A as creator and data owner, B as analyst. B accepts by creating a membership from its own account.
- **FR2:** Member abilities: A contributes data (querying is optional for A); B can run queries and receive results.
- **FR3:** Account A registers both datasets as **configured tables**, backed by A's S3 data and Glue catalog, and associates them with the collaboration.

### 6.2 Analysis rules (privacy controls)
- **FR4:** Both configured tables carry an **aggregation analysis rule**, so queries can only return aggregates. The rule sets:
  - the permitted aggregate functions (COUNT, AVG);
  - the dimension column(s) that results are grouped by (for example `medication_class`);
  - a **minimum aggregation constraint**, so that no group with fewer than N patients is returned.
- **FR5:** The approved research question is provided as a vetted **SQL analysis template**, so the analyst runs a reviewed query rather than arbitrary SQL. The implementation decisions below describe how the MVP meets this.
- **FR6 (allowed):** Mean follow-up HbA1c per medication class, from the approved aggregate query, **comes back** for every group at or above the threshold.
- **FR7 (blocked or limited):** A query **is rejected or returns suppressed output** if it reads individual rows, projects `patient_id`, or would expose a group smaller than the minimum.

### 6.3 Result delivery
- **FR8:** Results reach the designated result receiver (Institution B) only through the collaboration's controlled delivery: they're written to B's configured S3 location.

### 6.4 Security and logging walkthrough
- **FR9:** Show that the analyst (B) can't reach A's underlying rows and only gets permitted aggregate outputs.
- **FR10:** Show **query logging** (Clean Rooms analysis logs and CloudTrail), which gives the data owner an audit record of every analysis that touched its data and of who got the results.
- **FR11:** Include a short "who can see what" matrix in the walkthrough.

## 7. Repeatability and reuse

- **RR1:** The environment is **infrastructure as code** (CloudFormation or CDK) with one stack per account: one for **Account A** holding the data, configured tables and collaboration, and one for **Account B** holding the membership and results bucket. Tearing down or rebuilding either side gives the same result every time.
- **RR2:** The synthetic data comes from a seeded generator script that can be re-run, and the schema is written down as a contract.
- **RR3:** Values that teams adapting Airlock change are **configuration parameters**, not literals in code: the research question, the table schemas, the analysis-rule threshold, the bucket names and the **two account IDs**.
- **RR3a:** Deployment signs in with **a named AWS SSO profile per account**; the profile names are parameters, and long-lived access keys are never used. Credentials come from `aws sso login` and never appear in code, configuration or the repository.
- **RR4:** A `README` covers the prerequisites, deployment, running the allowed and blocked queries, teardown, and **how to adapt Airlock to other datasets and governance rules**.
- **RR5:** Every console step has a documented IaC or CLI equivalent, including the **cross-account invitation and its acceptance**, so the demo can be reproduced without the console.

## 8. Acceptance criteria (EARS)

- **AC1:** WHEN Account A's stack is deployed and Account B's stack has accepted the invitation, THE SYSTEM SHALL have one Clean Rooms collaboration in which A and B are both members, each joined from its own account.
- **AC2:** WHEN Institution A's configured tables are created, THE SYSTEM SHALL attach to each one an aggregation analysis rule that enforces a minimum count.
- **AC3:** WHEN Institution B runs the approved query, THE SYSTEM SHALL return per-medication-class aggregates, only for groups that reach the minimum count.
- **AC4:** WHEN Institution B submits a query that asks for row-level data or for `patient_id`, THE SYSTEM SHALL reject it or suppress its output.
- **AC5:** WHEN a query runs, THE SYSTEM SHALL write a log entry that identifies the query and the member receiving its results.
- **AC6:** WHEN the stacks are destroyed, THE SYSTEM SHALL remove the collaboration resources and leave no configured tables behind.
- **AC7:** WHEN a team edits only the documented configuration parameters, THE SYSTEM SHALL target another dataset or research question without any code change.

## 9. Demo script (happy path)

1. Introduce the institutions, their accounts and the collaboration between them.
2. Walk through the configured tables in Institution A and their analysis rule: the privacy guardrails.
3. Switch to Institution B and run the approved query: **aggregate results come back**.
4. Still as Institution B, try a row-level or `patient_id` query: **rejected or suppressed**.
5. Show the logs of the queries just run, and the who-can-see-what matrix.
6. Open the README section on adapting Airlock to your own data.

## 10. Stretch goals (after the MVP)

- **SG1:** A two-data-owner pattern: each owner brings one table, and the joint analysis only works with both.
- **SG2:** **Differential privacy** on top of the aggregation rule.
- **SG3:** Joins on encrypted columns with **Cryptographic Computing for Clean Rooms (C3R)**.
- **SG4:** A second research scenario (for example genomics or student outcomes) to show that the pattern carries over.
- **SG5:** A privacy-preserving lookalike or modeling scenario built on **Clean Rooms ML**.

## 11. Constraints and caveats

- **C1:** Only synthetic data; the generator's design rules out real patient data.
- **C2:** No compliance claims. Airlock's features are presented as technical controls that *support* privacy requirements.
- **C3:** Keep the research question and the analysis simple; the MVP favors a small, clear scope over breadth.

## 12. Reference

- AWS Clean Rooms User Guide: https://docs.aws.amazon.com/clean-rooms/latest/userguide/what-is.html

## Implementation decisions

Decisions taken for the MVP build. They refine the requirements above; they do not replace them.

- **Option A, aggregation analysis rule.** Both configured tables carry an `AGGREGATION` analysis rule (FR4). FR5 is satisfied by a vetted SQL file, `queries/approved/mean_hba1c_by_medication_class.sql`, reviewed by the data owner and run by `scripts.demo_allowed`, because Clean Rooms analysis templates only work with the *custom* analysis rule. The README documents the alternative pattern (custom analysis rule + analysis template, with `aggregationThresholds` / `disallowedOutputColumns`) for teams that want "only pre-approved SQL can run at all".
- **IaC: AWS CDK v2 in Python**, one stack per account: `AirlockDataOwner` (Account A) and `AirlockAnalyst` (Account B) (RR1).
- **Accounts and auth:** Account A (Mercy Research Hospital, SSO profile `airlock-a`) and Account B (State University Health Analytics, SSO profile `airlock-b`). The committed config uses the AWS documentation example IDs 111122223333 (A) and 444455556666 (B); replace them with your own. All values live in `config/airlock.yaml` (RR3, RR3a).
- **Region: `us-west-2`** for everything (collaboration, Glue, both buckets).
- **Analytics engine: Spark.** Clean Rooms SQL engine support ended Dec 17, 2025; Spark supports aggregation analysis rules.
- **Mean only, no median.** Median is not an aggregation-rule function. The demo reports patient counts, mean baseline HbA1c and mean follow-up HbA1c per medication class.
- **Mean change computed client-side.** The aggregation rule does not allow arithmetic between aggregates, so `mean_change = mean_followup_hba1c − mean_baseline_hba1c` is computed by the demo script from the two returned means and labelled "computed client-side".
- **Roles:** Institution B is the only query runner, the only result receiver (results land in B's S3 bucket) and the query-compute payer. Institution A contributes data only (no member abilities).
- **Threshold: 30.** Every output row must cover at least 30 distinct `patient_id` values (`COUNT_DISTINCT` output constraint, `privacy.min_distinct_patients`).
- **Rare class: Meglitinide, 12 synthetic patients**, deliberately below the threshold, so the "limit" case of FR7 shows a suppressed group.
- **Documented residual for AC6:** the CloudWatch Logs analysis log groups that Clean Rooms creates (`/aws/cleanrooms/<collaboration-id>`) are retained after teardown as audit evidence. Teardown prints one `aws logs delete-log-group` command per group and never deletes them itself. The `CDKToolkit` bootstrap stacks are also retained.

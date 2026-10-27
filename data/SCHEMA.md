# Airlock schema contract

This is the agreed contract between the two institutions: the columns, the join key, and which columns the clean room exposes. The research question drives the columns, and the columns drive the privacy controls. The source of truth for names and types is `config/airlock.yaml` (`tables[*]`); this document describes the default configuration.

All data is **synthetic**. The demo shows technical controls that support privacy requirements; it does not make any institution compliant with any regulation.

## Tables

Both tables are owned by Institution A (Mercy Research Hospital), stored as Parquet in A's S3 bucket and registered in A's Glue database `airlock_research`.

### `patients` (Dataset 1, patient / clinical)

| Column | Glue type | Domain / range | Generation rule |
|---|---|---|---|
| `patient_id` | string | `SYN-000001` … `SYN-003000` | Sequential synthetic ID with the `SYN-` prefix |
| `age` | int | 18–89 (adults only) | `round(gauss(58, 12))`, clamped to 18..89 |
| `sex` | string | `M`, `F`, `Other` | Weighted choice 0.48 / 0.50 / 0.02 |
| `region` | string | `Northeast`, `Southeast`, `Midwest`, `Southwest`, `West` | Weighted choice (coarse geography only) |
| `primary_diagnosis` | string | `Type 2 Diabetes`, `Type 2 Diabetes with CKD`, `Type 2 Diabetes with CVD` | Weighted choice 0.85 / 0.10 / 0.05 |
| `baseline_hba1c` | double | 6.5–13.5 (%) | `gauss(8.4, 1.1)`, clamped, rounded to 0.1 |
| `enrollment_date` | date | 2025-01-01 … 2025-12-31 | Start date + `randint(0, 364)` days |

### `outcomes` (Dataset 2, treatment / outcomes)

| Column | Glue type | Domain / range | Generation rule |
|---|---|---|---|
| `patient_id` | string | same IDs as `patients` | Foreign key to `patients.patient_id` |
| `medication_class` | string | `Metformin`, `GLP-1`, `SGLT2`, `DPP-4`, `Meglitinide` | Exact per-class counts (below), shuffled with the seeded RNG |
| `procedure` | string | `None`, `Diabetes self-management education`, `Medical nutrition therapy` | Weighted choice 0.70 / 0.20 / 0.10 |
| `followup_hba1c` | double | 5.0–14.0 (%) | `baseline + gauss(class mean_change, class sd_change)`, clamped, rounded to 0.1 |
| `followup_date` | date | enrollment + 150..210 days | `enrollment_date + randint(150, 210)` days |
| `outcome_flag` | string | `improved`, `stable`, `worsened` | `delta = round(followup - baseline, 1)`: `improved` if ≤ -0.5, `worsened` if ≥ 0.5, else `stable` |

The requirements call HbA1c a "float"; it is stored as double precision (Parquet `double`, Glue `double`). In Parquet, `age` is int32, dates are date32, and everything else is string.

## Join key and cardinality

- Join key: `patient_id`, present in both tables.
- Cardinality: exactly one `outcomes` row per `patients` row (1:1). The inner join therefore keeps every patient once, and for any group `AVG(followup_hba1c) - AVG(baseline_hba1c)` equals the group's mean change.

## What the clean room exposes (configured tables)

| Table | Allowed columns | Join | Dimensions (GROUP BY / filter) | Aggregates |
|---|---|---|---|---|
| `patients` (`airlock-patients`) | `patient_id`, `sex`, `region`, `primary_diagnosis`, `baseline_hba1c` | `patient_id` | `sex`, `region`, `primary_diagnosis` | `COUNT_DISTINCT(patient_id)`, `COUNT(patient_id)`, `AVG(baseline_hba1c)` |
| `outcomes` (`airlock-outcomes`) | `patient_id`, `medication_class`, `outcome_flag`, `followup_hba1c` | `patient_id` | `medication_class`, `outcome_flag` | `COUNT_DISTINCT(patient_id)`, `COUNT(patient_id)`, `AVG(followup_hba1c)` |

Columns not in the allowed list (`age`, `enrollment_date`, `procedure`, `followup_date`) are not visible to the analyst at all, not even as schema. `patient_id` can be used to join and to count, but never selected.

**Minimum aggregation threshold:** every output row must cover at least **30 distinct `patient_id` values** (`privacy.min_distinct_patients`). Rows below the threshold are suppressed (absent from the result).

## Expected per-class counts (default seed 20261022)

| `medication_class` | Patients | Visible to the analyst? |
|---|---|---|
| Metformin | 1150 | yes |
| GLP-1 | 700 | yes |
| SGLT2 | 650 | yes |
| DPP-4 | 488 | yes |
| Meglitinide | 12 | **no**: below the threshold of 30, deliberately, to demonstrate suppression |

Total: 3000 patients, 3000 outcome rows.

## Regenerate

From the repository root:

```bash
.venv/bin/python -m datagen.generate
```

Output (the generator deletes and recreates only `data/generated/`):

- `data/generated/s3/patients/patients.parquet`, `data/generated/s3/outcomes/outcomes.parquet`: uploaded to A's bucket.
- `data/generated/local/{patients,outcomes}.csv`: human preview only, never uploaded.
- `data/generated/local/manifest.json`: the data owner's ground truth (per-class `n`, mean baseline, mean follow-up, mean change, outcome-flag counts, sha256 of each Parquet file). It has no timestamps, so the same seed gives byte-identical files.

## Synthetic-data guarantee (C1)

- Every value comes from one `random.Random(seed)` instance; there is no other entropy source and no external input.
- There are no names, addresses, birth dates, medical record numbers or free text.
- Every ID carries the `SYN-` prefix.

## Bring your own files (`data.source: files`)

To run the same clean room on another dataset:

1. Set `data.source: files` and `data.files_dir` to a directory outside `data/generated/` (for example `data/customer/s3`). Validation rejects a `files_dir` under `data/generated/`, so the generator can never wipe your files.
2. Put Parquet files only, one directory per table prefix: `<files_dir>/<tables[*].s3_prefix><file>.parquet` (for example `data/customer/s3/patients/part-0.parquet`). Hidden files are ignored and never uploaded; any other file is an error.
3. Make each file's column names, order and types match `tables[*].columns`. Type mapping: string→`string`, int32→`int`, int64→`bigint`, double→`double`, date32→`date`, timestamp→`timestamp`, bool→`boolean`. `cdk synth` and `scripts.deploy` validate this before anything is deployed.
4. Use synthetic data only; never load real patient records into this demo.
5. The generator is disabled in this mode (`python -m datagen.generate` exits 1), and `demo_allowed --check` and the rare-class line skip the manifest comparison.

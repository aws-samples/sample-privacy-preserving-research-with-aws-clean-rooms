-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

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

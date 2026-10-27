-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

-- Limited: the rare class has fewer distinct patients than the threshold -> suppressed
SELECT o.medication_class           AS medication_class,
       COUNT(DISTINCT o.patient_id) AS patient_count,
       AVG(o.followup_hba1c)        AS mean_followup_hba1c
FROM outcomes o
WHERE o.medication_class = 'Meglitinide'
GROUP BY o.medication_class

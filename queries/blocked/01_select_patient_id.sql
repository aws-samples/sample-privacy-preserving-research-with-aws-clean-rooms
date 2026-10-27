-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

-- Blocked: tries to return the identifier (join-only column)
SELECT patient_id, AVG(followup_hba1c) AS mean_followup_hba1c
FROM outcomes
GROUP BY patient_id

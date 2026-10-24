-- Connection PoC draft: NOT validated on BigQuery yet.
-- Inspect the actual output schema before writing a projection/filter.
-- Set useQueryCache=false and maximumBytesBilled=104857600 on the query job.
-- This is relationship inspection, NOT a full revenue aggregation.
SELECT *
FROM GRAPH_EXPAND('{{PROJECT_ID}}.{{DATASET_ID}}.ec_semantics_poc')
LIMIT 100;

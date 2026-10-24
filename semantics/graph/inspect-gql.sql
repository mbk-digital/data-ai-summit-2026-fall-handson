-- Alternative PoC draft: NOT validated. Requires appropriate reservation.
-- Do not automatically create reservations or use this as a fallback.
GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.ec_semantics_poc`
MATCH (r:Refund)-[:RefundOf]->(i:Item)-[:ForProduct]->(p:Product)
WHERE i.order_item_id = 'I-A-A'
RETURN p.product_id, i.order_item_id, r.refund_id,
       r.refunded_date, r.refund_amount_jpy;

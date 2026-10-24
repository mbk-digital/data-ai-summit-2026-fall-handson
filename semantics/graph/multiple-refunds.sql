-- Fixed relationship check. Does not compute total revenue.
-- Run with useQueryCache=false and maximumBytesBilled=104857600.
SELECT Product_product_id AS product_id,
       Item_order_item_id AS order_item_id,
       Refund_refund_id AS refund_id,
       Refund_refunded_date AS refunded_date,
       Refund_refund_amount_jpy AS refund_amount_jpy
FROM GRAPH_EXPAND('{{PROJECT_ID}}.{{DATASET_ID}}.ec_semantics_poc')
WHERE Item_order_item_id = 'I-A-A'
ORDER BY refund_id;

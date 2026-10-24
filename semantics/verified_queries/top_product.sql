-- Example question: Which product had the highest net sales in August 2026?
SELECT product_id, SUM(net_sales_jpy) AS net_sales_jpy
FROM `{{PROJECT_ID}}.{{DATASET_ID}}.v_revenue_events`
WHERE event_date >= DATE '2026-08-01'
  AND event_date < DATE '2026-09-01'
GROUP BY product_id
ORDER BY net_sales_jpy DESC, product_id ASC
LIMIT 1;

-- Example question: What were net sales, COGS and gross profit for August 2026?
-- Half-open interval and explicit DATE literals. Parameterization is a later cloud integration step.
SELECT
  SUM(gross_sales_jpy) AS gross_sales_jpy,
  SUM(discount_jpy) AS discount_jpy,
  SUM(refund_jpy) AS refund_jpy,
  SUM(net_sales_jpy) AS net_sales_jpy,
  SUM(cogs_jpy) AS cogs_jpy,
  SUM(gross_profit_jpy) AS gross_profit_jpy
FROM `{{PROJECT_ID}}.{{DATASET_ID}}.v_revenue_events`
WHERE event_date >= DATE '2026-08-01'
  AND event_date < DATE '2026-09-01';

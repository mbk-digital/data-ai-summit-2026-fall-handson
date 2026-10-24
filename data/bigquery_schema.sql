-- SQL template; no dataset is created here. Location must be confirmed first.
-- Integer JPY, synthetic data only. These constraints are validated by the seed generator.
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.products` (
  product_id STRING NOT NULL OPTIONS(description='Unique product identifier'),
  product_name STRING NOT NULL,
  category STRING NOT NULL
);
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.campaigns` (
  campaign_id STRING NOT NULL,
  campaign_name STRING NOT NULL
);
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.orders` (
  order_id STRING NOT NULL,
  ordered_date DATE NOT NULL OPTIONS(description='Date the order was placed; JST business date'),
  status STRING NOT NULL OPTIONS(description='Order status: COMPLETED, SHIPPED or CANCELLED')
);
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.order_items` (
  order_item_id STRING NOT NULL,
  order_id STRING NOT NULL,
  product_id STRING NOT NULL,
  campaign_id STRING NOT NULL,
  quantity INT64 NOT NULL,
  unit_price_jpy INT64 NOT NULL OPTIONS(description='Per-unit price before line discount; integer JPY, excludes tax and shipping'),
  discount_jpy INT64 NOT NULL OPTIONS(description='Total line discount, not a per-unit discount'),
  unit_cost_jpy INT64 NOT NULL OPTIONS(description='Per-unit cost snapshot at order time')
);
CREATE TABLE IF NOT EXISTS `{{PROJECT_ID}}.{{DATASET_ID}}.refunds` (
  refund_id STRING NOT NULL,
  order_item_id STRING NOT NULL OPTIONS(description='One order item can have multiple refund records; do not duplicate sales by joining directly'),
  refunded_date DATE NOT NULL OPTIONS(description='Date the refund was paid; JST business date'),
  refund_amount_jpy INT64 NOT NULL OPTIONS(description='Actual refund amount after discounts; positive integer JPY'),
  returned_quantity INT64 NOT NULL,
  recovered_cost_jpy INT64 NOT NULL OPTIONS(description='Cost of returned items that could be put back into inventory; integer JPY')
);

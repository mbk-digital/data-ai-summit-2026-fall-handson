-- Created successfully on BigQuery US in the development project, 2026-09-06.
-- New participant projects remain unverified.
-- Resolve placeholders from explicit participant configuration before execution.
-- Plain CREATE intentionally refuses to replace an existing graph.
CREATE PROPERTY GRAPH `{{PROJECT_ID}}.{{DATASET_ID}}.ec_semantics_poc`
NODE TABLES (
  `{{PROJECT_ID}}.{{DATASET_ID}}.products` AS Product
    KEY (product_id) LABEL Product PROPERTIES ALL COLUMNS,
  `{{PROJECT_ID}}.{{DATASET_ID}}.order_items` AS Item
    KEY (order_item_id) LABEL Item PROPERTIES ALL COLUMNS,
  `{{PROJECT_ID}}.{{DATASET_ID}}.refunds` AS Refund
    KEY (refund_id) LABEL Refund PROPERTIES ALL COLUMNS
)
EDGE TABLES (
  `{{PROJECT_ID}}.{{DATASET_ID}}.order_items` AS ItemProduct
    KEY (order_item_id)
    SOURCE KEY (order_item_id) REFERENCES Item (order_item_id)
    DESTINATION KEY (product_id) REFERENCES Product (product_id)
    LABEL ForProduct NO PROPERTIES,
  `{{PROJECT_ID}}.{{DATASET_ID}}.refunds` AS RefundItem
    KEY (refund_id)
    SOURCE KEY (refund_id) REFERENCES Refund (refund_id)
    DESTINATION KEY (order_item_id) REFERENCES Item (order_item_id)
    LABEL RefundOf NO PROPERTIES
);

"""Reproducible synthetic fixtures. All monetary values are integer JPY."""

import csv
import sqlite3
from pathlib import Path
from .settings import config

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE products (
  product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, category TEXT NOT NULL
);
CREATE TABLE campaigns (
  campaign_id TEXT PRIMARY KEY, campaign_name TEXT NOT NULL
);
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, ordered_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('COMPLETED', 'SHIPPED', 'CANCELLED'))
);
CREATE TABLE order_items (
  order_item_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders,
  product_id TEXT NOT NULL REFERENCES products,
  campaign_id TEXT NOT NULL REFERENCES campaigns,
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  unit_price_jpy INTEGER NOT NULL CHECK(unit_price_jpy >= 0),
  discount_jpy INTEGER NOT NULL CHECK(discount_jpy >= 0),
  unit_cost_jpy INTEGER NOT NULL CHECK(unit_cost_jpy >= 0),
  CHECK(discount_jpy <= quantity * unit_price_jpy)
);
CREATE TABLE refunds (
  refund_id TEXT PRIMARY KEY,
  order_item_id TEXT NOT NULL REFERENCES order_items,
  refunded_date TEXT NOT NULL, refund_amount_jpy INTEGER NOT NULL CHECK(refund_amount_jpy >= 0),
  returned_quantity INTEGER NOT NULL CHECK(returned_quantity >= 0),
  recovered_cost_jpy INTEGER NOT NULL CHECK(recovered_cost_jpy >= 0)
);
"""

# Same SELECT/UNION logic is also rendered into the BigQuery view.
EVENT_SELECT = """
SELECT 'SALE' AS event_type, i.order_item_id AS event_id,
       o.ordered_date AS event_date, o.order_id, i.order_item_id,
       i.product_id, p.product_name, p.category, i.campaign_id,
       i.quantity AS sold_quantity, 0 AS returned_quantity,
       i.quantity * i.unit_price_jpy AS gross_sales_jpy,
       i.discount_jpy AS discount_jpy, 0 AS refund_jpy,
       i.quantity * i.unit_price_jpy - i.discount_jpy AS net_sales_jpy,
       i.quantity * i.unit_cost_jpy AS cogs_jpy,
       i.quantity * i.unit_price_jpy - i.discount_jpy
         - i.quantity * i.unit_cost_jpy AS gross_profit_jpy
FROM order_items i JOIN orders o ON o.order_id = i.order_id
JOIN products p ON p.product_id = i.product_id
WHERE o.status = 'COMPLETED'
UNION ALL
SELECT 'REFUND', r.refund_id, r.refunded_date, o.order_id, i.order_item_id,
       i.product_id, p.product_name, p.category, i.campaign_id,
       0, r.returned_quantity, 0, 0, r.refund_amount_jpy,
       -r.refund_amount_jpy, -r.recovered_cost_jpy,
       -r.refund_amount_jpy + r.recovered_cost_jpy
FROM refunds r JOIN order_items i ON i.order_item_id = r.order_item_id
JOIN orders o ON o.order_id = i.order_id
JOIN products p ON p.product_id = i.product_id
WHERE o.status = 'COMPLETED'
"""

FIELDS = {
    "products": ["product_id", "product_name", "category"],
    "campaigns": ["campaign_id", "campaign_name"],
    "orders": ["order_id", "ordered_date", "status"],
    "order_items": ["order_item_id", "order_id", "product_id", "campaign_id", "quantity", "unit_price_jpy", "discount_jpy", "unit_cost_jpy"],
    "refunds": ["refund_id", "order_item_id", "refunded_date", "refund_amount_jpy", "returned_quantity", "recovered_cost_jpy"],
}


def fixture():
    """Small hand-designed dataset: higher August gross sales, lower profit."""
    rows = {
        "products": [("P-A", "ワイヤレスヘッドホン", "Electronics"),
                     ("P-B", "コーヒーメーカー", "Home"),
                     ("P-C", "Tシャツ", "Apparel")],
        "campaigns": [("NONE", "通常販売"), ("SUMMER", "夏のセール")],
        "orders": [], "order_items": [], "refunds": [],
    }
    # id, date, product, quantity, unit price, line discount, unit cost, campaign, status
    sales = [
        ("J-A", "2026-07-10", "P-A", 10, 10000, 0, 6000, "NONE", "COMPLETED"),
        ("J-B", "2026-07-11", "P-B", 10, 8000, 0, 4000, "NONE", "COMPLETED"),
        ("J-C", "2026-07-12", "P-C", 20, 2000, 0, 800, "NONE", "COMPLETED"),
        ("A-A", "2026-08-10", "P-A", 20, 10000, 40000, 6000, "SUMMER", "COMPLETED"),
        ("A-B", "2026-08-11", "P-B", 20, 8000, 10000, 4000, "SUMMER", "COMPLETED"),
        ("A-C", "2026-08-12", "P-C", 30, 2000, 15000, 800, "SUMMER", "COMPLETED"),
        ("A-X", "2026-08-13", "P-A", 100, 10000, 0, 6000, "NONE", "CANCELLED"),
        # 出荷済みだが未完了。会社の定義（COMPLETED のみ）では数えない。定義を知らない
        # Agent が含めると、Q01 は 440,000 円、Q04 の最大減少は Apparel になる。
        ("A-Y", "2026-08-14", "P-A", 20, 10000, 0, 6000, "NONE", "SHIPPED"),
        ("S-A", "2026-09-01", "P-A", 1, 10000, 0, 6000, "NONE", "COMPLETED"),
    ]
    for key, date, product, qty, price, discount, cost, campaign, status in sales:
        rows["orders"].append(("O-" + key, date, status))
        rows["order_items"].append(("I-" + key, "O-" + key, product, campaign, qty, price, discount, cost))
    rows["refunds"] = [
        ("R-A1", "I-A-A", "2026-08-20", 32000, 4, 24000),
        ("R-A2", "I-A-A", "2026-08-22", 48000, 6, 0),
        ("R-B1", "I-A-B", "2026-08-25", 15000, 2, 8000),
        ("R-J1", "I-J-A", "2026-08-03", 20000, 2, 12000),
        ("R-S1", "I-A-C", "2026-09-02", 1500, 1, 800),
    ]
    return rows


def connect(rows=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for table, records in (fixture() if rows is None else rows).items():
        placeholders = ",".join("?" for _ in FIELDS[table])
        db.executemany("INSERT INTO " + table + " VALUES (" + placeholders + ")", records)
    db.execute("CREATE VIEW revenue_events AS " + EVENT_SELECT)
    validate(db)
    return db


def validate(db):
    invalid = db.execute("""
        SELECT i.order_item_id
        FROM order_items i JOIN refunds r ON r.order_item_id = i.order_item_id
        JOIN orders o ON o.order_id = i.order_id
        GROUP BY i.order_item_id
        HAVING SUM(r.refund_amount_jpy) > i.quantity * i.unit_price_jpy - i.discount_jpy
           OR SUM(r.returned_quantity) > i.quantity
           OR SUM(r.recovered_cost_jpy) > i.quantity * i.unit_cost_jpy
           OR MIN(r.refunded_date) < o.ordered_date
           OR o.status != 'COMPLETED'
    """).fetchall()
    if invalid:
        raise ValueError("Invalid refund totals, dates or order status")
    if db.execute("""SELECT 1 FROM refunds r JOIN order_items i
        ON i.order_item_id = r.order_item_id
        WHERE r.recovered_cost_jpy > r.returned_quantity * i.unit_cost_jpy""").fetchone():
        raise ValueError("Recovered cost exceeds returned inventory cost")


def export_seed(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows = fixture()
    db = connect(rows)
    db.close()
    for table, records in rows.items():
        with (destination / (table + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(FIELDS[table])
            writer.writerows(records)
    return {table: len(records) for table, records in rows.items()}

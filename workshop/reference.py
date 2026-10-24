"""Independent-of-LLM reference answers; not an AI Agent simulation."""

import json
from .data import ROOT, config, connect

SOURCE = "v_revenue_events"


def cases():
    return [json.loads(line) for line in (ROOT / "data/public_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def reference_answer(case, db):
    cfg = config()
    answer = {
        "case_id": case["case_id"], "status": "OK",
        "period": case["period"], "definition_version": cfg["definition_version"],
        "measures": {}, "dimensions": {}, "data_sources": [SOURCE],
        "customer_message_ja": "ローカルの基準集計です。AI Agentの実行結果ではありません。",
    }
    start, end = case["period"]["start"], case["period"]["end_exclusive"]
    kind = case["kind"]
    if kind == "unanswerable":
        answer.update(status="INSUFFICIENT_DATA", data_sources=[], reason_code="FUTURE_OUTCOME_NOT_OBSERVED",
                      customer_message_ja="過去の販売データだけでは、来月の利益増加を断定できません。")
    elif kind == "total":
        field = case["metric"]
        if field not in {"net_sales_jpy", "gross_profit_jpy", "refund_jpy", "cogs_jpy", "gross_sales_jpy"}:
            raise ValueError("Unsupported metric")
        value = db.execute("SELECT COALESCE(SUM(" + field + "), 0) FROM revenue_events WHERE event_date >= ? AND event_date < ?", (start, end)).fetchone()[0]
        answer["measures"][field] = {"value": value, "unit": "JPY"}
    elif kind == "top_product":
        field = case["metric"]
        if field not in {"net_sales_jpy", "sold_quantity"}:
            raise ValueError("Unsupported ranking metric")
        row = db.execute("SELECT product_id, SUM(" + field + ") AS value FROM revenue_events WHERE event_date >= ? AND event_date < ? GROUP BY product_id ORDER BY value DESC, product_id LIMIT 1", (start, end)).fetchone()
        answer["dimensions"] = {"product_id": row["product_id"]}
        answer["measures"][field] = {"value": row["value"], "unit": "JPY" if field.endswith("jpy") else "ITEM"}
    elif kind == "largest_profit_decline":
        previous = cfg["previous_period"]
        row = db.execute("""
          SELECT category,
            SUM(CASE WHEN event_date >= ? AND event_date < ? THEN gross_profit_jpy ELSE 0 END)
            - SUM(CASE WHEN event_date >= ? AND event_date < ? THEN gross_profit_jpy ELSE 0 END) AS delta
          FROM revenue_events GROUP BY category ORDER BY delta ASC, category LIMIT 1
        """, (start, end, previous["start"], previous["end_exclusive"])).fetchone()
        answer["comparison_period"] = previous
        answer["dimensions"] = {"category": row["category"]}
        answer["measures"] = {"gross_profit_delta_jpy": {"value": row["delta"], "unit": "JPY"}}
    else:
        raise ValueError("Unsupported case kind: " + kind)
    return answer


def all_answers():
    db = connect()
    try:
        return [reference_answer(case, db) for case in cases()]
    finally:
        db.close()


def illustrative_baseline(answers):
    """Deliberately incorrect teaching fixtures, NEVER measured model outputs."""
    result = json.loads(json.dumps(answers))
    by_id = {answer["case_id"]: answer for answer in result}
    by_id["Q01"]["measures"]["net_sales_jpy"]["value"] = 420000
    by_id["Q02"]["measures"]["gross_profit_jpy"]["value"] = 131000
    by_id["Q03"]["dimensions"]["product_id"] = "P-A"
    by_id["Q03"]["measures"]["net_sales_jpy"]["value"] = 200000
    by_id["Q05"]["measures"]["refund_jpy"]["value"] = 95000
    by_id["Q08"].update(status="OK", reason_code="", customer_message_ja="来月は必ず利益が増えます。")
    for answer in result:
        answer["fixture_label"] = "illustrative_errors_not_model_output"
    return result

"""Deterministic answer contract checks, without an LLM judge."""

from decimal import Decimal, InvalidOperation


def check_answer(answer, expected, *, check_sources=True, allow_extra_dimensions=False):
    if answer is None:
        return ["missing_answer"]
    errors = []
    for key in ("status", "period", "definition_version"):
        if key not in answer or answer[key] != expected[key]:
            errors.append(key + "_mismatch")
    dimensions = answer.get('dimensions')
    if not isinstance(dimensions, dict) or (not allow_extra_dimensions and dimensions != expected['dimensions']) or any(dimensions.get(k) != v for k, v in expected['dimensions'].items()):
        errors.append('dimensions_mismatch')
    if "comparison_period" in expected and answer.get("comparison_period") != expected["comparison_period"]:
        errors.append("comparison_period_mismatch")
    if expected["status"] == "INSUFFICIENT_DATA" and answer.get("reason_code") != expected["reason_code"]:
        errors.append("reason_code_mismatch")
    actual_measures = answer.get("measures")
    if not isinstance(actual_measures, dict):
        errors.append("invalid_measures")
        actual_measures = {}
    if set(actual_measures) != set(expected["measures"]):
        errors.append("measure_keys_mismatch")
    for name, correct in expected["measures"].items():
        candidate = actual_measures.get(name)
        if not isinstance(candidate, dict):
            errors.append(name + ":missing_measure")
            continue
        if candidate.get("unit") != correct["unit"]:
            errors.append(name + ":unit_mismatch")
        value = candidate.get("value")
        try:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Value must be a JSON number")
            number = Decimal(str(value))
            if not number.is_finite() or number != Decimal(str(correct["value"])):
                errors.append(name + ":value_mismatch")
        except (InvalidOperation, ValueError):
            errors.append(name + ":invalid_number")
    sources = answer.get("data_sources")
    if check_sources and (not isinstance(sources, list) or any(not isinstance(source, str) for source in sources)):
        errors.append("invalid_data_sources")
    elif check_sources and set(sources) != set(expected["data_sources"]):
        errors.append("data_sources_mismatch")
    if not isinstance(answer.get("customer_message_ja"), str) or not answer["customer_message_ja"].strip():
        errors.append("missing_customer_message")
    return errors


def evaluate(answers, expected_answers):
    known = {item["case_id"] for item in expected_answers}
    indexed = {}
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("case_id"), str):
            raise ValueError("Every answer must be an object with a string case_id")
        case_id = answer["case_id"]
        if case_id not in known:
            raise ValueError("Unknown case_id: " + case_id)
        if case_id in indexed:
            raise ValueError("Duplicate case_id: " + case_id)
        indexed[case_id] = answer
    results = []
    for expected in expected_answers:
        errors = check_answer(indexed.get(expected["case_id"]), expected)
        results.append({"case_id": expected["case_id"], "success": not errors, "errors": errors})
    correct = sum(item["success"] for item in results)
    total = len(results)
    return {
        "evaluation_scope": "public_structured_answer_contract_only",
        "total_cases": total, "successful_cases": correct,
        "success_rate": correct / total if total else None,
        "cost_per_successful_task_jpy": None,
        "latency_ms": None,
        "limitations": [
            "No Cloud or LLM was invoked by this evaluator.",
            "Source names are checked; true provenance and prose correctness are not verified.",
            "This small public set is for development, not production quality claims or official ranking.",
        ],
        "results": results,
    }

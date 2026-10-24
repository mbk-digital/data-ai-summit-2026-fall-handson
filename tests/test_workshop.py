import copy
import json
import tempfile
import unittest
from pathlib import Path

from workshop.bigquery import render
from workshop.data import config, connect, export_seed, fixture
from workshop.evaluation import evaluate
from workshop.reference import all_answers, illustrative_baseline
from workshop.__main__ import read_jsonl
from workshop.settings import configure_project


class FinancialDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()

    def tearDown(self):
        self.db.close()

    def total(self, metric, month="2026-08"):
        return self.db.execute("SELECT SUM(" + metric + ") FROM revenue_events WHERE substr(event_date,1,7) = ?", (month,)).fetchone()[0]

    def test_independently_calculated_august_totals(self):
        # Manual arithmetic: 420000 - 65000 - (32000+48000+15000+20000).
        self.assertEqual(self.total("gross_sales_jpy"), 420000)
        self.assertEqual(self.total("discount_jpy"), 65000)
        self.assertEqual(self.total("refund_jpy"), 115000)
        self.assertEqual(self.total("net_sales_jpy"), 240000)
        # Sale cost 224000, recovered inventory 44000.
        self.assertEqual(self.total("cogs_jpy"), 180000)
        self.assertEqual(self.total("gross_profit_jpy"), 60000)

    def test_july_sales_are_not_retroactively_reduced(self):
        self.assertEqual(self.total("net_sales_jpy", "2026-07"), 220000)
        self.assertEqual(self.total("gross_profit_jpy", "2026-07"), 104000)

    def test_cancelled_orders_are_excluded(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM revenue_events WHERE order_id='O-A-X'").fetchone()[0], 0)

    def test_multiple_refunds_do_not_duplicate_sale(self):
        row = self.db.execute("SELECT SUM(gross_sales_jpy), SUM(refund_jpy), SUM(net_sales_jpy) FROM revenue_events WHERE order_item_id='I-A-A'").fetchone()
        self.assertEqual(tuple(row), (200000, 80000, 80000))

    def test_date_boundary_excludes_september_events(self):
        value = self.db.execute("SELECT SUM(net_sales_jpy) FROM revenue_events WHERE event_date >= '2026-08-01' AND event_date < '2026-09-01'").fetchone()[0]
        self.assertEqual(value, 240000)
        self.assertEqual(self.total("net_sales_jpy", "2026-09"), 8500)

    def test_refund_without_recovered_inventory_reduces_profit(self):
        row = self.db.execute("SELECT cogs_jpy, gross_profit_jpy FROM revenue_events WHERE event_id='R-A2'").fetchone()
        self.assertEqual(tuple(row), (0, -48000))

    def test_invalid_refunds_are_rejected(self):
        rows = fixture()
        rows["refunds"].append(("BAD", "I-A-A", "2026-08-28", 100000, 1, 0))
        with self.assertRaises(ValueError):
            connect(rows)

    def test_future_reference_is_insufficient_data(self):
        answer = all_answers()[-1]
        self.assertEqual(answer["status"], "INSUFFICIENT_DATA")
        self.assertEqual(answer["measures"], {})

    def test_rank_and_decline_have_manual_gold_values(self):
        answers = {answer["case_id"]: answer for answer in all_answers()}
        self.assertEqual(answers["Q03"]["dimensions"], {"product_id": "P-B"})
        self.assertEqual(answers["Q03"]["measures"]["net_sales_jpy"]["value"], 135000)
        self.assertEqual(answers["Q04"]["dimensions"], {"category": "Electronics"})
        self.assertEqual(answers["Q04"]["measures"]["gross_profit_delta_jpy"]["value"], -64000)
        self.assertEqual(answers["Q07"]["dimensions"], {"product_id": "P-C"})
        self.assertEqual(answers["Q07"]["measures"]["sold_quantity"]["value"], 30)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.gold = all_answers()

    def test_reference_and_intentional_errors(self):
        self.assertEqual(evaluate(self.gold, self.gold)["successful_cases"], 8)
        report = evaluate(illustrative_baseline(self.gold), self.gold)
        self.assertEqual(report["successful_cases"], 3)
        self.assertIsNone(report["cost_per_successful_task_jpy"])

    def test_missing_case_is_failure_not_removed_from_denominator(self):
        report = evaluate(self.gold[:-1], self.gold)
        self.assertEqual(report["total_cases"], 8)
        self.assertEqual(report["successful_cases"], 7)
        self.assertEqual(report["results"][-1]["errors"], ["missing_answer"])

    def test_duplicate_and_unknown_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(self.gold + [self.gold[0]], self.gold)
        with self.assertRaises(ValueError):
            evaluate([{"case_id": "PRIVATE-01"}], self.gold)

    def test_correct_value_with_wrong_period_or_unit_fails(self):
        answers = copy.deepcopy(self.gold)
        answers[0]["period"]["start"] = "2026-07-01"
        answers[1]["measures"]["gross_profit_jpy"]["unit"] = "USD"
        report = evaluate(answers, self.gold)
        self.assertEqual(report["successful_cases"], 6)

    def test_boolean_string_and_nonfinite_values_fail(self):
        for value in (True, "240000", float("nan"), float("inf")):
            with self.subTest(value=value):
                answers = copy.deepcopy(self.gold)
                answers[0]["measures"]["net_sales_jpy"]["value"] = value
                self.assertEqual(evaluate(answers, self.gold)["successful_cases"], 7)

    def test_wrong_definition_or_missing_source_fails(self):
        answers = copy.deepcopy(self.gold)
        answers[0]["definition_version"] = "old"
        answers[1]["data_sources"] = []
        answers[3]["comparison_period"]["start"] = "2026-06-01"
        self.assertEqual(evaluate(answers, self.gold)["successful_cases"], 5)

    def test_unsupported_forecast_claim_fails(self):
        answers = copy.deepcopy(self.gold)
        answers[-1]["status"] = "OK"
        self.assertEqual(evaluate(answers, self.gold)["successful_cases"], 7)

    def test_additional_unrequested_measure_fails(self):
        answers = copy.deepcopy(self.gold)
        answers[0]["measures"]["predicted_profit"] = {"value": 1000000, "unit": "JPY"}
        self.assertEqual(evaluate(answers, self.gold)["successful_cases"], 7)

    def test_jsonl_errors_report_line_and_disallow_nan(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "answers.jsonl"
            path.write_text('{"case_id":"Q01"}\n{"value":NaN}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 2"):
                read_jsonl(path)


class ExportTests(unittest.TestCase):
    def test_seed_export_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertEqual(export_seed(directory)["refunds"], 5)
            before = {p.name: p.read_bytes() for p in directory.glob("*.csv")}
            export_seed(directory)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.glob("*.csv")})

    def test_sql_render_targets_only_new_project(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config()
            cfg["project_id"] = "participant-demo-project"
            render(cfg, Path(temp))
            view = (Path(temp) / "02_revenue_events.sql").read_text()
            self.assertIn("`participant-demo-project.ec_workshop.refunds`", view)
            self.assertNotIn("unrelated-demo-project", view)
            self.assertNotIn("{{", view)

    def test_sql_identifier_injection_is_rejected(self):
        cfg = config()
        cfg["project_id"] = "participant-demo-project"
        cfg["dataset_id"] = "bad`; DROP TABLE products; --"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                render(cfg, Path(temp))


class ParticipantSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.shared = self.root / "config/workshop.json"
        self.shared.write_text(json.dumps({"project_id": None, "dataset_id": "ec_workshop"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_checkout_requires_own_project(self):
        self.assertIsNone(config(root=self.root)["project_id"])
        with self.assertRaisesRegex(ValueError, "configure"):
            config(require_project=True, root=self.root)

    def test_project_is_saved_without_changing_shared_config(self):
        before = self.shared.read_bytes()
        configure_project("my-summit-project", root=self.root)
        self.assertEqual(config(require_project=True, root=self.root)["project_id"], "my-summit-project")
        self.assertEqual(self.shared.read_bytes(), before)

    def test_shared_update_preserves_participant_settings(self):
        configure_project("my-summit-project", root=self.root)
        shared = json.loads(self.shared.read_text())
        shared["dataset_id"] = "ec_workshop_v2"
        self.shared.write_text(json.dumps(shared))
        merged = config(require_project=True, root=self.root)
        self.assertEqual(merged["project_id"], "my-summit-project")
        self.assertEqual(merged["dataset_id"], "ec_workshop_v2")

    def test_repeat_configuration_preserves_location(self):
        path = self.root / "config/workshop.local.json"
        path.write_text(json.dumps({"project_id": "my-summit-project", "bigquery_location": "US"}))
        configure_project("another-summit-project", root=self.root)
        self.assertEqual(config(root=self.root)["bigquery_location"], "US")

    def test_invalid_project_is_not_saved(self):
        with self.assertRaises(ValueError):
            configure_project("invalid/project", root=self.root)
        self.assertFalse((self.root / "config/workshop.local.json").exists())


if __name__ == "__main__":
    unittest.main()

import unittest
from workshop.data_agent import context, extract, fingerprint


class DataAgentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"project_id": "participant-project", "dataset_id": "ec_workshop"}

    def test_comparison_changes_one_factor_at_a_time(self):
        raw, defined, view = [context(self.cfg, v) for v in ("raw", "defined", "view")]
        self.assertEqual(raw["datasourceReferences"], defined["datasourceReferences"])
        self.assertEqual(defined["systemInstruction"], view["systemInstruction"])
        self.assertNotEqual(raw["systemInstruction"], defined["systemInstruction"])
        self.assertNotIn("240000", str(view))
        self.assertNotIn("draft", defined["systemInstruction"])
        self.assertEqual(raw["options"], view["options"])

    def test_evidence_does_not_turn_progress_into_answer(self):
        response = [{"systemMessage": {"text": {"parts": ["Working"], "textType": "THOUGHT"}}},
                    {"systemMessage": {"text": {"parts": ["Answer"], "textType": "FINAL_RESPONSE"}}},
                    {"systemMessage": {"data": {"generatedSql": "SELECT 1"}}},
                    {"systemMessage": {"data": {"result": {"data": [{"value": 1}]}}}},
                    {"error": {"message": "failed"}}, {"usageMetadata": {"totalTokens": 9}}]
        result = extract(response)
        self.assertEqual(result["texts"][0]["textType"], "THOUGHT")
        self.assertEqual(result["sql"], ["SELECT 1"])
        self.assertEqual(result["data"], [{"data": [{"value": 1}]}])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["usage_fields"]), 1)

    def test_config_identity_is_order_independent(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))

    def test_business_cost_column_is_not_api_usage(self):
        result = extract([{"systemMessage": {"data": {"result": {"data": [{"recovered_cost_jpy": 12000}]}}}}])
        self.assertEqual(result["usage_fields"], [])

    def test_teardown_refuses_modified_agent_and_defaults_to_plan(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from workshop.data_agent import AgentClient
        from workshop.cloud import CloudError
        with tempfile.TemporaryDirectory() as folder:
            cfg = dict(self.cfg, data_agent_location="us", bigquery_location="US")
            client = AgentClient(cfg)
            client.folder = Path(folder)
            name = client.parent + "/dataAgents/ec-poc-raw-0123456789abcdef"
            resource = {"description": "owner", "dataAnalyticsAgent": {"publishedContext": {"systemInstruction": "original"}}}
            record = {"name": name, "description": "owner", "state": "present", "resource": resource}
            (Path(folder) / "agent.resource.json").write_text(json.dumps(record))
            client.request = Mock(return_value=resource)
            self.assertEqual(client.teardown(), 1)
            self.assertEqual([call.args[0] for call in client.request.call_args_list], ["GET"])
            client.request = Mock(return_value=dict(resource, dataAnalyticsAgent={"changed": True}))
            with self.assertRaises(CloudError):
                client.teardown(execute=True)
            self.assertEqual([call.args[0] for call in client.request.call_args_list], ["GET"])

import unittest

from workshop.cloud import Client, CloudError, LABELS, ensure_owned_dataset, seed_insert_sql, validate_cloud_config


class CloudSafetyTests(unittest.TestCase):
    def test_unmanaged_dataset_cannot_be_reused(self):
        with self.assertRaisesRegex(CloudError, "not marked"):
            ensure_owned_dataset({"location": "US", "labels": {}}, "US")

    def test_region_mismatch_is_rejected(self):
        with self.assertRaisesRegex(CloudError, "location differs"):
            ensure_owned_dataset({"location": "EU", "labels": LABELS}, "US")

    def test_own_dataset_is_reusable(self):
        ensure_owned_dataset({"location": "us", "labels": LABELS}, "US")

    def test_region_must_be_explicit(self):
        with self.assertRaises(CloudError):
            validate_cloud_config({"dataset_id": "ec_workshop", "bigquery_location": None})

    def test_seed_values_are_parameters_and_append_is_guarded(self):
        name = "customer's text; DROP TABLE products;"
        sql, params = seed_insert_sql("`example-project.ec_workshop.products`", "products", [("P-A", name, "Home")])
        self.assertNotIn(name, sql)
        self.assertEqual(params[1]["parameterValue"]["value"], name)
        self.assertIn("ASSERT (SELECT COUNT(*)", sql)
        self.assertIn("BEGIN TRANSACTION", sql)

    def test_query_has_bytes_cap_and_parses_integer(self):
        client = Client({"project_id": "example-project", "bigquery_location": "US"})
        calls = []

        def fake_request(method, url, payload=None):
            calls.append(payload)
            return {"jobComplete": True, "jobReference": {"jobId": "job-123"},
                    "schema": {"fields": [{"name": "net", "type": "INTEGER"}]},
                    "rows": [{"f": [{"v": "240000"}]}], "totalBytesProcessed": "1000"}

        client.request = fake_request
        self.assertEqual(client.query("SELECT 240000 AS net"), [{"net": 240000}])
        self.assertEqual(calls[0]["maximumBytesBilled"], "104857600")
        self.assertEqual(calls[0]["location"], "US")
        self.assertEqual(client.jobs[0]["job_id"], "job-123")

    def test_partial_query_results_are_not_silently_accepted(self):
        client = Client({"project_id": "example-project", "bigquery_location": "US"})
        client.request = lambda *args: {"jobComplete": True, "jobReference": {"jobId": "job-123"}, "pageToken": "next"}
        with self.assertRaisesRegex(CloudError, "row limit"):
            client.query("SELECT 1")


if __name__ == "__main__":
    unittest.main()


def diagnose(billing, missing=None, stage='lab1', extra_permissions=(), quiet=False, printed=None):
    import json
    from unittest.mock import Mock, patch
    from workshop.cloud import preflight, stage_requirements
    apis, perms = stage_requirements(stage)
    perms = perms + list(extra_permissions)
    def command(args, account=None):
        if args[0]=='projects':return json.dumps({'lifecycleState':'ACTIVE'})
        if args[0]=='billing':
            if isinstance(billing, Exception):raise billing
            return json.dumps({'billingEnabled':billing})
        return '\n'.join(apis)
    c=Mock(project='participant-project', location='US', account=None, cfg={'dataset_id':'ec_workshop'})
    c.request.side_effect=[{'permissions':[p for p in perms if p!=missing and not p.startswith('bigquery.tables.')]}, None]
    capture = (lambda *a, **k: printed.append(' '.join(map(str, a)))) if printed is not None else None
    with patch('workshop.cloud.gcloud',side_effect=command), patch('workshop.cloud.save'), \
         patch('builtins.print', side_effect=capture), \
         patch('workshop.cloud.stage_requirements', side_effect=lambda selected: (apis, perms) if selected == stage else stage_requirements(selected)):
        return preflight(c,stage,quiet), c



class PreflightTests(unittest.TestCase):
    def test_unknown_billing_continues_but_is_not_verified(self):
        r,c=diagnose(CloudError('permission denied'))
        self.assertEqual(r['billing_enabled'],'unknown')
        self.assertTrue(r['can_attempt_bigquery'])
        self.assertTrue(r['can_attempt_stage'])
        self.assertFalse(r['ready_for_bigquery'])
        self.assertFalse(r['prerequisites_verified'])
        self.assertTrue(r['warnings'])
        self.assertEqual(c.request.call_count,2)

    def test_disabled_billing_stops_attempt(self):
        r,_=diagnose(False)
        self.assertFalse(r['can_attempt_stage'])
        self.assertFalse(r['can_attempt_bigquery'])
        self.assertFalse(r['ready_for_bigquery'])
        self.assertFalse(r['prerequisites_verified'])

    def test_quiet_distinguishes_disabled_from_unknown_billing(self):
        # False は停止し unknown は続行するため、参加者への対処が違う。
        printed = []
        diagnose(False, quiet=True, printed=printed)
        self.assertTrue(any('課金が無効です' in line for line in printed))
        self.assertFalse(any('確認できませんでした' in line for line in printed))
        printed = []
        diagnose(CloudError('permission denied'), quiet=True, printed=printed)
        self.assertTrue(any('課金が有効か確認できませんでした' in line for line in printed))
        self.assertFalse(any('課金が無効です' in line for line in printed))

    def test_quiet_prints_nothing_when_everything_is_fine(self):
        printed = []
        diagnose(True, quiet=True, printed=printed)
        self.assertEqual(printed, [])

    def test_lab1_excludes_runtime_and_observability(self):
        r,_=diagnose(True)
        self.assertEqual(r['missing_apis'],[])
        self.assertEqual(r['missing_permissions'],[])
        self.assertTrue(r['prerequisites_verified'])
        self.assertTrue(r['can_attempt_stage'])
        self.assertFalse(r['table_operations_verified'])
        self.assertFalse(r['model_access_verified'])
        self.assertFalse(any(p.startswith('aiplatform.') for p in r['granted_permissions']))

    def test_missing_agent_creation_stops_lab1_not_bigquery(self):
        r,_=diagnose(True,'geminidataanalytics.dataAgents.create')
        self.assertFalse(r['can_attempt_stage'])
        self.assertTrue(r['can_attempt_bigquery'])

    def test_bootstrap_unknown_billing_reaches_real_api_and_preserves_error(self):
        from unittest.mock import Mock, patch
        from workshop.cloud import bootstrap
        c=Mock(project='participant-project',cfg={'dataset_id':'ec_workshop'})
        c.request.side_effect=CloudError('actual API billing failure')
        report={'can_attempt_bigquery':True,'can_attempt_stage':True,'ready_for_bigquery':False}
        with patch('workshop.cloud.preflight',return_value=report), patch('workshop.cloud.connect'):
            with self.assertRaisesRegex(CloudError,'actual API billing failure'):
                bootstrap(c,'lab1')
        self.assertEqual(c.request.call_count,1)

    def test_bootstrap_known_disabled_billing_does_not_touch_resources(self):
        from unittest.mock import Mock, patch
        from workshop.cloud import bootstrap
        c=Mock()
        report={'can_attempt_bigquery':False,'can_attempt_stage':False}
        with patch('workshop.cloud.preflight',return_value=report):
            with self.assertRaises(CloudError):bootstrap(c,'lab1')
        c.request.assert_not_called()


class DatasetPermissionRegressionTests(unittest.TestCase):
    def test_project_preconditions_allow_attempt_but_do_not_claim_table_access(self):
        report, client = diagnose(True)
        self.assertTrue(report['can_attempt_bigquery'])
        self.assertTrue(report['can_attempt_stage'])
        self.assertTrue(report['ready_for_bigquery'])
        self.assertTrue(report['prerequisites_verified'])
        self.assertFalse(report['table_operations_verified'])
        self.assertEqual(report['deferred_permissions'], [
            'bigquery.tables.create', 'bigquery.tables.getData', 'bigquery.tables.updateData'])
        requested = client.request.call_args_list[0].args[2]['permissions']
        self.assertNotIn('bigquery.tables.create', requested)
        self.assertIn('bigquery.datasets.create', requested)
        self.assertIn('bigquery.jobs.create', requested)

    def test_missing_project_permissions_still_block(self):
        for permission in ('bigquery.datasets.create', 'bigquery.jobs.create'):
            with self.subTest(permission=permission):
                report, _ = diagnose(True, permission)
                self.assertFalse(report['can_attempt_bigquery'])
                self.assertFalse(report['can_attempt_stage'])
                self.assertIn(permission, report['missing_permissions'])
                self.assertFalse(report['ready_for_bigquery'])
                self.assertFalse(report['prerequisites_verified'])

    def test_table_creation_denied_after_dataset_creation_stops_before_seed(self):
        from unittest.mock import Mock, patch
        from workshop.cloud import bootstrap
        c = Mock(project='participant-project', location='US', cfg={'dataset_id': 'ec_workshop'})
        c.request.side_effect = [None, {'location': 'US', 'labels': LABELS}]
        c.query.side_effect = CloudError('403 bigquery.tables.create denied')
        with patch('workshop.cloud.preflight', return_value={
                'can_attempt_bigquery': True, 'can_attempt_stage': True}), \
             patch('workshop.cloud.connect'), patch('workshop.cloud.render'), \
             patch('pathlib.Path.read_text', return_value='CREATE TABLE'), \
             patch('workshop.cloud.fixture') as fixture, patch('builtins.print'):
            with self.assertRaisesRegex(CloudError, '403 bigquery.tables.create'):
                bootstrap(c)
        self.assertEqual(c.request.call_args_list[1].args[0], 'POST')
        c.query.assert_called_once_with('CREATE TABLE')
        fixture.assert_not_called()

    def test_existing_unmanaged_dataset_still_stops_before_sql(self):
        from unittest.mock import Mock, patch
        from workshop.cloud import bootstrap
        c = Mock(project='participant-project', location='US', cfg={'dataset_id': 'ec_workshop'})
        c.request.return_value = {'location': 'US', 'labels': {}}
        with patch('workshop.cloud.preflight', return_value={
                'can_attempt_bigquery': True, 'can_attempt_stage': True}), \
             patch('workshop.cloud.connect'):
            with self.assertRaisesRegex(CloudError, 'not marked'):
                bootstrap(c)
        c.query.assert_not_called()

    def test_real_read_and_write_denials_are_not_hidden(self):
        from unittest.mock import Mock, patch
        from workshop.cloud import bootstrap
        for successful_queries, permission in ((1, 'getData'), (2, 'updateData')):
            with self.subTest(permission=permission):
                c = Mock(project='participant-project', location='US',
                         cfg={'dataset_id': 'ec_workshop'})
                c.request.return_value = {'location': 'US', 'labels': LABELS}
                c.query.side_effect = ([[]] * successful_queries
                                       + [CloudError('403 bigquery.tables.' + permission)])
                with patch('workshop.cloud.preflight', return_value={
                        'can_attempt_bigquery': True, 'can_attempt_stage': True}), \
                     patch('workshop.cloud.connect'), patch('workshop.cloud.render'), \
                     patch('pathlib.Path.read_text', return_value='CREATE TABLE'), \
                     patch('workshop.cloud.save'), patch('workshop.cloud.fixture', return_value={
                         'products': [('P-A', 'sample', 'Home')]}), patch('builtins.print'):
                    with self.assertRaisesRegex(CloudError, permission):
                        bootstrap(c)
                self.assertEqual(c.query.call_count, successful_queries + 1)

    def test_diagnostic_scope_and_success_flags_for_every_stage(self):
        for stage in ('bigquery', 'lab1', 'lab1-catalog', 'lab1-graph', 'lab2', 'lab3', 'all'):
            with self.subTest(stage=stage):
                report, client = diagnose(True, stage=stage)
                self.assertEqual(report['schema_version'], 2)
                self.assertEqual(report['permission_check_scope'], 'project_preconditions')
                self.assertEqual(report['checked_permissions'],
                                 client.request.call_args_list[0].args[2]['permissions'])
                self.assertTrue(report['ready_for_bigquery'])
                self.assertTrue(report['prerequisites_verified'])
                self.assertFalse(report['table_operations_verified'])

    def test_new_table_or_routine_permissions_are_not_silently_deferred(self):
        for permission in ('bigquery.tables.delete', 'bigquery.routines.create'):
            with self.subTest(permission=permission):
                report, _ = diagnose(True, missing=permission, extra_permissions=(permission,))
                self.assertIn(permission, report['checked_permissions'])
                self.assertIn(permission, report['missing_permissions'])
                self.assertNotIn(permission, report['deferred_permissions'])
                self.assertFalse(report['can_attempt_stage'])
                self.assertFalse(report['prerequisites_verified'])

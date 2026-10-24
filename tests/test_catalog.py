import tempfile
import unittest
import hashlib
import json
import copy
from pathlib import Path
from unittest.mock import Mock, patch
from workshop.catalog import Catalog, concept, as_document, cleanup_catalog
from workshop.cloud import CloudError


class CatalogTests(unittest.TestCase):
    def test_only_body_is_sent_and_unknown_header_is_rejected(self):
        fields, body = concept(as_document('Refunds use refund dates.'))
        self.assertEqual(body, 'Refunds use refund dates.')
        self.assertNotIn('type: Metric', body)
        with self.assertRaises(ValueError):
            concept('---\ntype: Metric\ntitle: Test\nunknown: x\n---\ntext')

    def test_missing_catalog_location_fails_before_auth(self):
        with self.assertRaises(ValueError):
            Catalog({'project_id': 'participant-project', 'dataset_id': 'ec_workshop', 'bigquery_location': 'US'})

    def test_changed_entry_is_not_updated(self):
        with tempfile.TemporaryDirectory() as temp:
            c = Catalog({'project_id': 'participant-project', 'dataset_id': 'ec_workshop', 'bigquery_location': 'US', 'catalog_location': 'us'}, root=Path(temp))
            c.ensure = Mock(return_value={'name': 'type'})
            c.get = Mock(return_value={'name': 'foreign-entry'})
            c.c.request = Mock()
            with self.assertRaisesRegex(CloudError, '前回値'):
                c.sync(as_document('rule'))
            c.c.request.assert_not_called()

    def test_console_overview_edit_is_recorded_and_returned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = {'project_id': 'participant-project', 'dataset_id': 'ec_workshop',
                   'bigquery_location': 'US', 'catalog_location': 'us'}
            c = Catalog(cfg, root=root)
            starter = as_document('Refunds use [[date]].')
            completed = as_document('Refunds use refunded_date.')
            ident = 'ec-lab1-' + hashlib.sha256(cfg['dataset_id'].encode()).hexdigest()[:12]
            name = c.parent + '/entryGroups/' + ident + '/entries/business-rules'
            kind = c.parent + '/entryTypes/' + ident
            def entry(document):
                return {'name': name, 'createTime': '2026-09-01T00:00:00Z',
                        'updateTime': '2026-09-02T00:00:00Z', 'entryType': kind,
                        'entrySource': {'displayName': 'EC売上・粗利益の業務定義', 'description': 'ハンズオンの共通業務定義'},
                        'aspects': {'dataplex-types.global.overview': {'data': {'content': document, 'contentType': 'MARKDOWN'}}}}
            before, current = entry(starter), entry(completed)
            current['updateTime'] = '2026-09-03T00:00:00Z'
            current['aspects']['dataplex-types.global.overview']['updateTime'] = '2026-09-03T00:00:00Z'
            # Console saves add an empty links list (observed 2026-09-27).
            current['aspects']['dataplex-types.global.overview']['data']['links'] = []
            c.folder.mkdir(parents=True)
            c.ledger_path.write_text(json.dumps({name: {'state': 'present', 'snapshot': before, 'payload': {}}}))
            c.ensure = Mock(return_value={'name': kind})
            c.get = Mock(return_value=current)
            c.overview = Mock(side_effect=lambda value: next(iter(value['aspects'].items())))
            receipt = c.prepare_console_definition(starter)
            self.assertEqual(receipt['document'], completed)
            self.assertEqual(json.loads(c.ledger_path.read_text())[name]['source'], 'participant_console_edit')
            self.assertTrue(list(c.folder.glob('console-edit-*.json')))

            changed = copy.deepcopy(current)
            changed['entrySource']['description'] = 'unexpected change'
            c.get = Mock(return_value=changed)
            with self.assertRaisesRegex(CloudError, 'overview本文以外'):
                c.prepare_console_definition(starter)

            c.ledger_path.write_text(json.dumps({name: {'state': 'present', 'snapshot': before, 'payload': {}}}))
            linked = copy.deepcopy(current)
            linked['aspects']['dataplex-types.global.overview']['data']['links'] = [{'name': 'foreign'}]
            c.get = Mock(return_value=linked)
            with self.assertRaisesRegex(CloudError, 'overview本文以外'):
                c.prepare_console_definition(starter)

    def test_catalog_cleanup_refuses_foreign_children_before_delete(self):
        c = Mock()
        c.parent = 'projects/test-project/locations/us'
        group = c.parent + '/entryGroups/owned'
        snapshot = {'name': group}
        c.ledger = {group: {'state': 'present', 'snapshot': snapshot}}
        c.get.return_value = snapshot
        with patch('workshop.catalog.group_children', return_value=[{'name': group + '/entries/foreign'}]):
            with self.assertRaises(CloudError):
                cleanup_catalog(c, True)
        c.c.request.assert_not_called()

    def test_graph_blocks_bigquery_table_deletion(self):
        import json
        from workshop.cleanup import plan
        from workshop.cloud import LABELS
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'artifacts').mkdir()
            (root/'artifacts/cloud-resources.json').write_text(json.dumps({'project_id': 'participant-project', 'dataset_id': 'ec_workshop', 'tables': []}))
            c = Mock(project='participant-project', location='US', cfg={'dataset_id': 'ec_workshop'})
            c.request.return_value = {'location': 'US', 'labels': LABELS}
            c.query.return_value = [{'property_graph_name': 'unrelated_graph'}]
            with self.assertRaisesRegex(CloudError, 'Property Graph'):
                plan(c, root)
            self.assertEqual(c.request.call_count, 1)

    def test_delete_failure_preserves_intent_and_can_resume_when_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            c = Mock()
            c.folder = Path(temp)
            name = 'projects/participant-project/locations/us/entryGroups/owned/entries/rules'
            value = {'name': name}
            c.ledger = {name: {'state': 'present', 'snapshot': value}}
            c.get.return_value = value
            c.c.request.side_effect = OSError('lost delete response')
            with self.assertRaises(OSError):
                cleanup_catalog(c, True)
            self.assertEqual(c.ledger[name]['state'], 'delete_requested')
            c.get.return_value = None
            self.assertEqual(cleanup_catalog(c, True), [])
            self.assertEqual(c.ledger[name]['state'], 'deleted')

    def test_new_stages_include_catalog_and_graph_without_runtime(self):
        from workshop.cloud import stage_requirements
        apis, permissions = stage_requirements('lab1-graph')
        self.assertIn('dataplex.googleapis.com', apis)
        self.assertIn('dataplex.entries.update', permissions)
        self.assertIn('bigquery.propertyGraphs.delete', permissions)
        self.assertNotIn('aiplatform.googleapis.com', apis)

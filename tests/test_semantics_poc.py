import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from workshop import semantics_poc as module
from workshop.cloud import CloudError


class ResourceProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(module, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.poc = module.Poc({'project_id': 'participant-project', 'dataset_id': 'ec_workshop', 'bigquery_location': 'US'})
        self.poc.c.request = Mock()

    def test_unrecorded_resource_cannot_be_adopted(self):
        self.poc.get = Mock(return_value={'name': 'already-present'})
        with self.assertRaises(CloudError):
            self.poc.ensure('collection', 'id', {}, 'entryId')
        self.poc.c.request.assert_not_called()

    def test_changed_resource_is_not_overwritten(self):
        self.poc.get = Mock(return_value={'description': 'participant edit'})
        self.poc.ledger['collection/id'] = {'snapshot': {'description': 'original'}, 'payload': {}}
        with self.assertRaises(CloudError):
            self.poc.ensure('collection', 'id', {}, 'entryId')
        self.poc.c.request.assert_not_called()

    def test_interrupted_creation_is_not_retried_silently(self):
        self.poc.get = Mock(return_value=None)
        self.poc.ledger['collection/id'] = {'state': 'requested'}
        with self.assertRaises(CloudError):
            self.poc.ensure('collection', 'id', {}, 'entryId')
        self.poc.c.request.assert_not_called()

    def test_unchanged_resource_reused_without_write(self):
        snapshot = {'description': 'original'}
        self.poc.get = Mock(return_value=snapshot)
        self.poc.ledger['collection/id'] = {'state': 'present', 'snapshot': snapshot, 'payload': {'displayName': 'metric'}}
        self.assertEqual(self.poc.ensure('collection', 'id', {'displayName': 'metric'}, 'entryId'), snapshot)
        self.poc.c.request.assert_not_called()

    def test_entry_group_with_new_api_default_is_reused_without_write(self):
        name = 'projects/participant-project/locations/us/entryGroups/ec-lab1-x'
        snapshot = {'name': name, 'createTime': 'c', 'displayName': 'EC'}
        self.poc.get = Mock(return_value={**snapshot, 'changeRequestConfig': {}})
        self.poc.ledger[name] = {'state': 'present', 'snapshot': snapshot, 'payload': {}}
        self.poc.ensure('projects/participant-project/locations/us/entryGroups', 'ec-lab1-x', {}, 'entryGroupId')
        self.poc.c.request.assert_not_called()
        self.poc.get = Mock(return_value={**snapshot, 'changeRequestConfig': {'requireApproval': True}})
        with self.assertRaises(CloudError):
            self.poc.ensure('projects/participant-project/locations/us/entryGroups', 'ec-lab1-x', {}, 'entryGroupId')

"""エージェントの表示名。参加者が ID を照合せずに Console で見分けるためのもの。"""
import tempfile
import pathlib
import unittest
from unittest import mock

from workshop.data_agent import DISPLAY_NAMES, VARIANTS, AgentClient


class DisplayNameTests(unittest.TestCase):
    def test_every_variant_has_a_distinct_display_name(self):
        self.assertEqual(set(DISPLAY_NAMES), set(VARIANTS))
        self.assertEqual(len(set(DISPLAY_NAMES.values())), len(VARIANTS))

    def test_new_agent_is_created_with_its_display_name(self):
        cfg = {'project_id': 'participant-project', 'bigquery_location': 'US', 'data_agent_location': 'us',
               'dataset_id': 'ec_workshop'}
        with tempfile.TemporaryDirectory() as folder:
            client = AgentClient(cfg)
            client.folder = pathlib.Path(folder)
            created = {}

            def request(method, url, payload=None, missing_ok=False):
                if method == 'GET':
                    return None
                created.update(payload)
                return dict(payload)

            with mock.patch.object(client, 'request', side_effect=request):
                client.ensure('raw', {'datasourceReferences': {}})
        self.assertEqual(created['displayName'], DISPLAY_NAMES['raw'])
        # 所有の照合に使う description の印は表示名に置き換えない。
        self.assertTrue(created['description'].startswith('workshop_cli:summit_ec_v1:'))


if __name__ == '__main__':
    unittest.main()

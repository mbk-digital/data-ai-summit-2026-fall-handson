import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from workshop.cleanup import plan
from workshop.cloud import CloudError, LABELS


class CleanupTests(unittest.TestCase):
    def test_extra_table_refuses_entire_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'artifacts').mkdir()
            (root/'artifacts/cloud-resources.json').write_text(json.dumps({'project_id':'participant-project','dataset_id':'ec_workshop','tables':['products']}))
            c=Mock(project='participant-project',location='US',cfg={'dataset_id':'ec_workshop'})
            c.query.return_value=[]
            c.request.side_effect=[{'location':'US','labels':LABELS},{'tables':[{'tableReference':{'tableId':'my_work'}}]}]
            with self.assertRaises(CloudError):plan(c,root)
            self.assertTrue(all(call.args[0]=='GET' for call in c.request.call_args_list))

    def test_empty_recorded_dataset_can_be_deleted_without_delete_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'artifacts').mkdir()
            (root/'artifacts/cloud-resources.json').write_text(json.dumps({'project_id':'participant-project','dataset_id':'ec_workshop','tables':[]}))
            c=Mock(project='participant-project',location='US',cfg={'dataset_id':'ec_workshop'})
            c.query.return_value=[]
            c.request.side_effect=[{'location':'US','labels':LABELS},{}]
            result=plan(c,root)
            self.assertEqual(len(result),1)
            self.assertNotIn('deleteContents',result[0])

    def test_lost_inventory_is_explained_without_reconstructing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            c=Mock(project='participant-project',location='US',cfg={'dataset_id':'ec_workshop'})
            c.request.return_value={'location':'US','labels':LABELS}
            with self.assertRaisesRegex(CloudError,'削除台帳がありません'):
                plan(c,Path(folder))
            self.assertEqual(c.request.call_count,1)
            self.assertFalse((Path(folder)/'artifacts').exists())

    def test_deferred_owned_graph_does_not_allow_foreign_graph_or_execution(self):
        from workshop.cleanup import run
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'artifacts').mkdir()
            (root/'artifacts/cloud-resources.json').write_text(json.dumps({'project_id':'participant-project','dataset_id':'ec_workshop','tables':[]}))
            c=Mock(project='participant-project',location='US',cfg={'dataset_id':'ec_workshop'})
            c.request.return_value={'location':'US','labels':LABELS,'tables':[]}
            c.query.return_value=[{'property_graph_name':'ec_semantics_poc'}]
            self.assertEqual(len(plan(c,root,['ec_semantics_poc'])),1)
            with self.assertRaises(CloudError):run(c,True,root,['ec_semantics_poc'])
            c.query.return_value.append({'property_graph_name':'foreign_graph'})
            with self.assertRaises(CloudError):plan(c,root,['ec_semantics_poc'])
            self.assertTrue(all(call.args[0]=='GET' for call in c.request.call_args_list))

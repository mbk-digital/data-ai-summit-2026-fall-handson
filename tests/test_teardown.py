import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from workshop.teardown import orchestrate
from workshop.cloud import CloudError
from workshop.semantics_poc import Poc
from workshop.lab1 import example
from workshop.settings import ROOT


class TeardownTests(unittest.TestCase):
    def setup_clients(self):
        client = Mock(project='participant-project', cfg={'dataset_id':'ec_workshop'})
        poc = Mock(ledger={'graph:participant-project.ec_workshop.ec_semantics_poc':{'state':'present'}})
        agent = Mock(); agent.teardown.return_value = 2
        return poc, Mock(), agent, client

    def test_plan_never_executes_and_defers_only_validated_graph(self):
        poc, cat, agent, client = self.setup_clients()
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog') as catalog, patch('workshop.teardown.cleanup.run', return_value=[]) as bq:
            report = orchestrate(poc,cat,agent,client,root=Path(temp))
            poc.cleanup.assert_called_once_with(False)
            catalog.assert_called_once_with(cat,False)
            agent.teardown.assert_called_once_with(False)
            self.assertEqual(bq.call_args.kwargs['deferred_graphs'],['ec_semantics_poc'])
            self.assertEqual(report['state'],'planned')

    def test_late_preflight_failure_prevents_all_deletion(self):
        poc,cat,agent,client=self.setup_clients()
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog') as catalog, patch('workshop.teardown.cleanup.run',side_effect=CloudError('foreign graph')):
            with self.assertRaises(CloudError):
                orchestrate(poc,cat,agent,client,True,Path(temp))
            poc.cleanup.assert_called_once_with(False)
            agent.teardown.assert_called_once_with(False)
            catalog.assert_called_once_with(cat,False)

    def test_execute_order_and_stop_on_catalog_failure(self):
        poc,cat,agent,client=self.setup_clients();events=[]
        poc.cleanup.side_effect=lambda execute: events.append(('graph',execute))
        agent.teardown.side_effect=lambda execute: events.append(('agent',execute)) or 2
        def catalog(_,execute):
            events.append(('catalog',execute))
            if execute: raise CloudError('changed entry')
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog',side_effect=catalog), patch('workshop.teardown.cleanup.run',side_effect=lambda _,execute,*a,**kw:events.append(('bq',execute)) or []):
            with self.assertRaises(CloudError):orchestrate(poc,cat,agent,client,True,Path(temp))
            self.assertEqual(events,[('graph',False),('catalog',False),('agent',False),('bq',False),('graph',True),('catalog',True)])
            report=json.loads((Path(temp)/'artifacts/teardown.json').read_text())
            self.assertEqual(report['state'],'interrupted')

    def test_success_executes_in_dependency_order(self):
        poc,cat,agent,client=self.setup_clients();events=[]
        poc.cleanup.side_effect=lambda execute:events.append(('graph',execute))
        agent.teardown.side_effect=lambda execute:events.append(('agent',execute)) or 2
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog',side_effect=lambda _,e:events.append(('catalog',e))), patch('workshop.teardown.cleanup.run',side_effect=lambda _,e,*a,**k:events.append(('bq',e)) or []):
            report=orchestrate(poc,cat,agent,client,True,Path(temp))
            self.assertEqual([n for n,e in events if e],['graph','catalog','agent','bq'])
            self.assertEqual(report['state'],'completed')

    def test_observability_preflight_failure_prevents_deletion(self):
        poc,cat,agent,client=self.setup_clients();obs=Mock()
        obs.cleanup.side_effect=CloudError('changed observability')
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog'), patch('workshop.teardown.cleanup.run') as bq:
            with self.assertRaises(CloudError):orchestrate(poc,cat,agent,client,True,Path(temp),obs)
            poc.cleanup.assert_called_once_with(False)
            bq.assert_not_called()

    def test_observability_deletion_after_bigquery(self):
        poc,cat,agent,client=self.setup_clients();obs=Mock();events=[]
        obs.cleanup.side_effect=lambda e:events.append(('obs',e))
        with tempfile.TemporaryDirectory() as temp, patch('workshop.teardown.cleanup_catalog'), patch('workshop.teardown.cleanup.run',side_effect=lambda _,e,*a,**kw:events.append(('bq',e)) or []):
            report=orchestrate(poc,cat,agent,client,True,Path(temp),obs)
            self.assertEqual([n for n,e in events if e],['bq','obs'])
            self.assertEqual(report['completed_stages'][-1],'Data Agent Observability')

    def test_inspect_never_creates_graph_and_restores_client_after_error(self):
        p=object.__new__(Poc);p.cfg={'dataset_id':'ec_workshop'};p.c=Mock(project='participant-project',jobs=[])
        p.graph_rows=Mock(return_value=[{'name':'graph'}]);p.ledger={'graph:participant-project.ec_workshop.ec_semantics_poc':{'state':'present','snapshot':[{'name':'graph'}]}}
        original=p.c.request
        def fail(sql):
            self.assertNotIn('{{',sql);self.assertIn("'I-A-A'",sql)
            p.c.request('POST','query',{'query':sql})
            raise CloudError('query failed')
        p.c.query.side_effect=fail
        with self.assertRaises(CloudError):p.inspect_graph()
        self.assertIs(p.c.request,original)
        self.assertFalse(original.call_args.args[2]['useQueryCache'])
        p.graph_rows.return_value=[]
        p.c.query.reset_mock()
        with self.assertRaises(CloudError):p.inspect_graph()
        p.c.query.assert_not_called()

    def test_recording_has_real_receipts_without_credentials_or_thoughts(self):
        raw=(ROOT/'examples/lab1-recorded.json').read_text();bundle=json.loads(raw)
        self.assertEqual(len(bundle['records']),6)
        self.assertNotIn('THOUGHT',raw)
        self.assertNotRegex(raw,r'/Users/|[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<![A-Za-z0-9_])\d{12}(?![A-Za-z0-9_])')
        hashes=[]
        for item in bundle['records']:
            if item['variant']=='raw': continue
            receipt=item['record']['lab']['catalog_receipt']
            self.assertIn('workshop-recorded-project',receipt['entry'])
            self.assertEqual(hashlib.sha256(receipt['body'].encode()).hexdigest(),receipt['body_sha256'])
            hashes.append(receipt['body_sha256'])
        self.assertEqual(len(set(hashes)),1)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'examples').mkdir();(root/'examples/lab1-recorded.json').write_text(raw)
            page=example(root).read_text()
            self.assertEqual(page.count('<summary>Catalog から取得した定義</summary>'),4)
            self.assertIn('①は 2026 年 9 月 5 日、②③は 9 月 6 日',page)

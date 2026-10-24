import shutil
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from workshop.web import Runs, handler, public_record
from workshop.cloud import save
from workshop.root_evidence import review_evidence, RequestEvidence
import asyncio


class EvidenceReviewTests(unittest.TestCase):
    def evidence(self):
        return {'status':'SUCCESS','sql':["SELECT '2026-08-01'"], 'tables':[{'headers':['previous_gross_profit_jpy','current_gross_profit_jpy'], 'rows':[[40000,-24000]]}]}

    def test_arithmetic_and_literal_presence_not_sql_proof(self):
        result=review_evidence(self.evidence(),{'current_period':{'start':'2026-08-01','end_exclusive':'2026-09-01'}})
        self.assertTrue(result['structural_checks_passed'])
        self.assertEqual(result['calculated_differences'][0]['value'],-64000)
        self.assertEqual(result['period_boundary_literals'],{'2026-08-01':True,'2026-09-01':False})
        self.assertTrue(result['human_review_required'])

    def test_malformed_or_nonfinite_table_rejected(self):
        for rows in ([[40000]],[[True,2]],[['NaN',2]]):
            e=self.evidence();e['tables'][0]['rows']=rows
            self.assertFalse(review_evidence(e,{})['structural_checks_passed'])
        e=self.evidence();e['tables'][0]['headers']=['previous_gross_profit_jpy']*2
        self.assertFalse(review_evidence(e,{})['structural_checks_passed'])
        self.assertIsNone(review_evidence({'tables':[]},{})['structural_checks_passed'])

    def test_cached_evidence_still_reviewed(self):
        async def check(cache):
            fetched=[];reviewed=[]
            async def fetch(key,consumer):fetched.append(key);return self.evidence()
            def review(e):reviewed.append(e);return review_evidence(e,{})
            s=RequestEvidence(cache,fetch,review)
            await s.both('q')
            self.assertEqual(len(fetched),1 if cache else 2)
            self.assertEqual(len(reviewed),1)
            self.assertEqual(s.reviews[0]['retrieval_mode'],'shared_evidence' if cache else 'separate_retrieval')
        asyncio.run(check(True));asyncio.run(check(False))

    def test_failed_structure_prevents_success(self):
        async def check():
            async def fetch(*args):
                e=self.evidence();e['tables'][0]['rows']=[[4]];return e
            s=RequestEvidence(True,fetch,lambda e:review_evidence(e,{}))
            self.assertEqual((await s.both('q'))['status'],'ERROR')
            self.assertEqual(s.results[-1]['status'],'ERROR')
        asyncio.run(check())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.context=patch('workshop.web.current_context',return_value=({'project_id':'participant-project','dataset_id':'ec'},'context'))
        self.context.start();self.addCleanup(self.context.stop);self.addCleanup(self.temp.cleanup)
        self.m=Runs(self.root)
        self.body={'question':'分析してください','cache':'off','request_id':'a'*32,'context':'context','csrf':self.m.token}

    def test_idempotency_busy_and_changed_context(self):
        with patch('workshop.web.threading.Thread') as t:
            key=self.m.start(self.body);self.assertEqual(self.m.start(self.body),key)
            t.assert_called_once()
            with self.assertRaises(ValueError):self.m.start(dict(self.body,question='different'))
            with self.assertRaises(BlockingIOError):self.m.start(dict(self.body,request_id='b'*32))
            with patch('workshop.web.current_context',return_value=({},'changed')):
                with self.assertRaises(ValueError):self.m.start(dict(self.body,request_id='b'*32))

    def test_invalid_requests_do_not_start_process(self):
        for changes in ({'question':' '},{'question':'x'*2001},{'cache':True},{'request_id':'../run'},{'question':[]},{'context':'other'}):
            with self.assertRaises(ValueError):self.m.start(dict(self.body,**changes))
        self.assertIsNone(self.m.active)
        self.assertFalse((self.root/'artifacts/web').exists())

    def test_failed_worker_is_recorded_and_releases_slot(self):
        with patch('workshop.web.threading.Thread'):key=self.m.start(self.body)
        job=self.root/'artifacts/web'/key
        with patch('workshop.web.subprocess.Popen',side_effect=OSError('SDK unavailable')):
            self.m._worker(key,dict(question=self.body['question'],cache='off',context='context'),job)
        self.assertIsNone(self.m.active)
        self.assertEqual(self.m.state()['jobs'][0]['state'],'failed')
        self.assertEqual(self.m.state()['records'][0]['state'],'failed')
        self.assertNotIn('data_agent_calls',self.m.state()['records'][0])

    def test_running_job_after_restart_is_interrupted(self):
        with patch('workshop.web.threading.Thread'):key=self.m.start(self.body)
        save(self.root/'artifacts/lab2'/key/'run.json',{'run_id':key,'started_at':'2026-10-10T00:00:00Z','state':'running',
            'config':{'project_id':'participant-project','dataset_id':'ec'}})
        # While this server still runs it, the job stays running.
        state=self.m.state();self.assertEqual(state['jobs'][0]['state'],'running');self.assertEqual(state['records'][0]['state'],'running')
        restarted=Runs(self.root);state=restarted.state()
        self.assertIsNone(state['active'])
        self.assertEqual(state['jobs'][0]['state'],'interrupted')
        self.assertEqual(state['records'][0]['state'],'interrupted')
        self.assertIn('もう一度',state['records'][0]['error'])

    def test_run_started_during_state_read_is_not_interrupted(self):
        original=self.m.records
        def start_meanwhile():
            with patch('workshop.web.threading.Thread'):self.m.start(self.body)
            return original()
        with patch.object(self.m,'records',side_effect=start_meanwhile):state=self.m.state()
        self.assertNotIn('interrupted',[j['state'] for j in state['jobs']]+[r['state'] for r in state['records']])
        self.assertEqual(self.m.state()['jobs'][0]['state'],'running')

    def test_finished_record_is_not_overridden(self):
        with patch('workshop.web.threading.Thread'):key=self.m.start(self.body)
        save(self.root/'artifacts/lab2'/key/'run.json',{'run_id':key,'started_at':'2026-10-10T00:00:00Z','state':'completed',
            'config':{'project_id':'participant-project','dataset_id':'ec'}})
        self.assertEqual(Runs(self.root).state()['records'][0]['state'],'completed')

    def test_only_matching_project_records_and_allowlisted_fields(self):
        record={'run_id':'r','started_at':'2026-01-01','config':{'project_id':'participant-project','dataset_id':'ec'},'credentials':'do-not-serve'}
        p=self.root/'artifacts/lab2/r';save(p/'run.json',record);save(p/'worker.json',{'private':True})
        save(self.root/'artifacts/lab2/other/run.json',dict(record,config={'project_id':'other','dataset_id':'ec'}))
        records=self.m.records();self.assertEqual(len(records),1);self.assertNotIn('credentials',records[0]);self.assertEqual(records[0]['evidence'],[])

    def test_benchmark_home_serves_the_result_page_at_the_top(self):
        self.m.home_benchmark='0123456789abcdef'
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.m,0))
        port=server.server_port;server.RequestHandlerClass=handler(self.m,port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(path):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);c.request('GET',path)
            response=c.getresponse();result=response.status,response.read().decode();c.close();return result
        try:
            with patch('workshop.benchmark_report.page',return_value='<html>results</html>') as page:
                self.assertEqual(get('/'),(200,'<html>results</html>'))
                self.assertEqual(page.call_args[0][2],'0123456789abcdef')
            with patch('workshop.benchmark_report.page',return_value=None):
                self.assertEqual(get('/')[0],404)
            self.m.home_benchmark=None
            status,body=get('/')
            self.assertEqual(status,200);self.assertIn('run-form',body)
        finally:
            server.shutdown();server.server_close()

    def test_http_csrf_host_origin_no_get_mutation(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.m,0))
        port=server.server_port;server.RequestHandlerClass=handler(self.m,port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,body=None,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            c.request(method,path,json.dumps(body) if body is not None else None,headers or {})
            response=c.getresponse();result=response.status,response.read();c.close();return result
        try:
            self.assertEqual(request('GET','/api/run')[0],404)
            self.assertEqual(request('GET','/../config/workshop.local.json')[0],404)
            self.assertEqual(request('GET','/api/state',headers={'Host':'foreign.invalid'})[0],403)
            self.assertEqual(request('POST','/api/run',dict(self.body,csrf='wrong'))[0],403)
            self.assertEqual(request('POST','/api/run',self.body,{'Origin':'https://foreign.invalid'})[0],403)
            with patch.object(self.m, '_worker'):
                self.assertEqual(request('POST','/api/run',self.body)[0],202)
                self.assertEqual(request('POST','/api/run',self.body)[0],202)
            status,page=request('GET','/');self.assertEqual(status,200);self.assertNotIn(b'{{FALLBACK}}',page)
        finally:server.shutdown();server.server_close();thread.join()

class MissingDefinitionTests(unittest.TestCase):
    """#1 を終えていない参加者に、素の FileNotFoundError を見せない。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'config').mkdir(); (self.root/'participant').mkdir()
        shutil.copyfile(ROOT/'config/workshop.json', self.root/'config/workshop.json')
        (self.root/'config/workshop.local.json').write_text(json.dumps({'project_id':'participant-project'}))

    def message(self):
        from workshop.web import current_context
        with self.assertRaises(ValueError) as caught:
            current_context(root=self.root)
        self.assertNotIsInstance(caught.exception, FileNotFoundError)
        return str(caught.exception)

    def test_absent_file_offers_both_routes(self):
        text = self.message()
        # 事前イベント参加者と Summit 単独参加者で経路が違う。
        self.assertIn('#1 から参加', text)
        self.assertIn('Summit から参加', text)
        self.assertIn('Checkpoint', text)

    def test_absent_file_says_one_prepare_is_not_enough(self):
        # 初回の prepare だけで完成すると読めないようにする。
        text = self.message()
        self.assertIn('最初の prepare だけでは完成しません', text)

    def test_empty_file_offers_both_routes(self):
        (self.root/'participant/semantics.txt').write_text('   \n')
        text = self.message()
        self.assertIn('Summit から参加', text)

    def test_unedited_placeholders_point_at_the_catalog_edit(self):
        (self.root/'participant/semantics.txt').write_text('返金は [[返金の計上日]] で計上する')
        text = self.message()
        self.assertIn('Knowledge Catalog', text)
        self.assertIn('prepare', text)


if __name__ == '__main__':
    unittest.main()


class ServerLockMessageTests(unittest.TestCase):
    def test_second_server_explains_how_to_stop_the_first(self):
        import io, fcntl
        from contextlib import redirect_stderr
        from workshop import web
        with tempfile.TemporaryDirectory() as d, patch.object(web,'ROOT',Path(d)), \
             patch('sys.argv',['web','--port','8080']), patch.object(web.fcntl,'flock',side_effect=BlockingIOError(11,'busy')):
            err=io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(web.main(),2)
        self.assertIn('別の Web サーバーがすでに動いています',err.getvalue())

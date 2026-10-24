import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from workshop import benchmark as b
from workshop.benchmark_report import render, page
from workshop.settings import ROOT


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for folder in ('data','config','agent','workshop','participant'):(self.root/folder).mkdir()
        for name in ('data/public_cases.jsonl','data/evaluation-cases.json','data/pricing-2026-10-07.json','config/workshop.json','agent/root-instructions.md','requirements-adk.txt','workshop/root_agent.py','workshop/root_evidence.py'):
            shutil.copyfile(ROOT/name,self.root/name)
        (self.root/'config/workshop.local.json').write_text(json.dumps({'project_id':'participant-project'}))
        (self.root/'participant/semantics.txt').write_text('Completed definition')

    def plan(self, **kwargs):
        return b.plan(self.root,case_ids=kwargs.pop('case_ids',['Q01','Q08']),repeats=kwargs.pop('repeats',1),**kwargs)

    def save_trial(self,p,index=0,answer=None,state='completed',amount=1):
        t=p['trials'][index];v=next(v for v in p['variants'] if v['name']==t['variant']);folder=self.root/'artifacts/lab2'/t['run_id']
        a=copy.deepcopy(p['cases'][t['case_id']]['expected']) if answer is None else answer
        run={'run_id':t['run_id'],'question':t['question'],'config':p['context']['config'],'root_model_requested':v['model'],
             'options':{'evidence_cache_enabled':v['cache']=='on'},'source_sha256':{k:v for k,v in p['context']['files'].items() if k in ('workshop/root_agent.py','workshop/root_evidence.py','agent/root-instructions.md','requirements-adk.txt')},
             'state':state,'finished_at':'2026-09-06T01:00:00Z','answer':a,
             'routing':{'selected_route':'data_agent','events':[{'route':'data_agent','reason':'EVIDENCE_REQUEST'}]}}
        evidence=[] if a['status']=='INSUFFICIENT_DATA' else [{'sql':['SELECT 1'],'tables':[]}]
        b.atomic_save(folder/'run.json',run)
        for n,e in enumerate(evidence):b.atomic_save(folder/('evidence-'+str(n)+'.json'),e)
        jobs=[] if not evidence else [{'job_id':'one','query':'SELECT 1','state':'DONE','referenced_tables':[{'project_id':'participant-project','dataset_id':run['config']['dataset_id'],'table_id':'orders'}]}]
        measurement={'input_sha256':b.digest(run,evidence),'pricing_sha256':hashlib.sha256(json.dumps(p['pricing'],sort_keys=True).encode()).hexdigest(),'reference_total_usd':amount,'known_subtotal_usd':amount if amount is not None else .4,
           'components':{'bigquery_query':{'reference_usd':amount,'job_evidence':jobs,'attribution':'time_agent_sql_estimate'}}}
        b.atomic_save(folder/'economics.json',measurement)
        b.atomic_save(b.attempt_path(self.root/'artifacts/benchmarks'/p['batch_id'],t),{'state':state,'wall_seconds':2})
        return folder,run,evidence,measurement

    def test_plan_freezes_order_ids_and_metadata_without_sending_gold(self):
        p=self.plan(repeats=2,caches=('off','on'))
        self.assertEqual(len(p['trials']),8);self.assertEqual(len({t['run_id'] for t in p['trials']}),8)
        self.assertTrue(p['trials'][0]['variant'].endswith('-off'));self.assertTrue(p['trials'][4]['variant'].endswith('-on'))
        command=b.trial_command(self.root,p,p['trials'][0]);self.assertEqual(command[command.index('--question')+1],p['trials'][0]['question'])
        for hidden in ('Q01','standard_aggregation','verified_sql','net_sales_jpy','240000'):self.assertNotIn(hidden,command)
        self.assertEqual(p,b.load_plan(self.root,p['batch_id'])[1])

    def test_changed_context_or_plan_blocks_execution(self):
        p=self.plan();(self.root/'participant/semantics.txt').write_text('changed')
        with self.assertRaises(ValueError):b.verify_context(self.root,p)
        folder=self.root/'artifacts/benchmarks'/p['batch_id'];p['trials'][0]['question']='changed';b.atomic_save(folder/'plan.json',p)
        with self.assertRaises(ValueError):b.load_plan(self.root,p['batch_id'])

    def test_cloud_change_stops_before_trial_process(self):
        from workshop import provenance
        p=self.plan();p['cloud_snapshot']={'complete':True,'content':{'old':True},'sha256':provenance.sha({'old':True})}
        folder=self.root/'artifacts/benchmarks'/p['batch_id'];b.atomic_save(folder/'plan.json',p);(folder/'plan.sha256').write_text(b.sha(p))
        with patch.object(provenance,'capture',return_value={'complete':False}), patch.object(b.subprocess,'Popen') as process:
            with self.assertRaises(b.CloudError):b.execute(self.root,p['batch_id'])
            process.assert_not_called()

    def test_cache_sidecar_cannot_override_missing_proof(self):
        p=self.plan();_,run,_,m=self.save_trial(p)
        job=m['components']['bigquery_query']['job_evidence'][0];job.update(cache_hit=True,referenced_tables=[])
        b.atomic_save(self.root/'artifacts/lab2'/run['run_id']/'economics.json',m)
        sidecar={'plan_sha256':b.sha(p),'measurement_sha256':{run['run_id']:b.sha(m)},'current_snapshot':{},'estimates':{run['run_id']:{'one':{'status':'estimated','estimated_referenced_tables':[{'project_id':'participant-project','dataset_id':'ec_workshop','table_id':'orders'}]}}}}
        b.atomic_save(self.root/'artifacts/benchmarks'/p['batch_id']/'cache-provenance.json',sidecar)
        self.assertEqual(b.report(self.root,p['batch_id'])['trials'][0]['provenance']['status'],'unverified')

    def test_pending_is_neither_answered_nor_a_completed_failure(self):
        p=self.plan();r=b.report(self.root,p['batch_id'])
        self.assertEqual(r['trials'][0]['outcome'],'pending');self.assertFalse(r['groups'][0]['finished']);self.assertIsNone(r['groups'][0]['reference_usd_per_structured_success'])

    def test_expected_route_mismatch_does_not_fail_correct_answer(self):
        p=self.plan();self.save_trial(p);self.save_trial(p,1)
        r=b.report(self.root,p['batch_id']);g=r['groups'][0]
        self.assertEqual(g['physical_route_calls'],{'data_agent':2});self.assertEqual(g['structured_successes'],2);self.assertFalse(r['trials'][0]['expected_route_match'])
        self.assertEqual(g['reference_usd_per_structured_success'],1);self.assertIsNone(g['reference_usd_per_verified_success'])
        self.assertEqual(r['trials'][0]['provenance']['status'],'matched_estimate')

    def test_abstention_only_succeeds_for_unobservable_question(self):
        p=self.plan();a=copy.deepcopy(p['cases']['Q08']['expected']);a['period']=p['cases']['Q01']['expected']['period']
        self.save_trial(p,answer=a);self.save_trial(p,1)
        r=b.report(self.root,p['batch_id']);self.assertFalse(r['trials'][0]['structured_success']);self.assertTrue(r['trials'][1]['structured_success'])
        self.assertIn('question_class_outcome_mismatch',r['trials'][0]['errors'])

    def test_failure_cost_stays_in_numerator(self):
        p=self.plan();self.save_trial(p,state='failed',amount=2);self.save_trial(p,1,amount=1)
        g=b.report(self.root,p['batch_id'])['groups'][0]
        self.assertEqual(g['planned'],2);self.assertEqual(g['structured_successes'],1);self.assertEqual(g['reference_usd_per_structured_success'],3)

    def test_partial_cost_and_zero_success_never_produce_rank(self):
        p=self.plan();self.save_trial(p,state='failed',amount=None);self.save_trial(p,1,state='failed')
        r=b.report(self.root,p['batch_id']);g=r['groups'][0]
        self.assertEqual(g['known_subtotal_usd'],1.4);self.assertEqual(g['incomplete_cost_trials'],1)
        self.assertIsNone(g['reference_usd_per_structured_success']);self.assertIsNone(r['ranking'])

    def test_human_review_is_explicit_and_bound_to_evidence(self):
        p=self.plan(case_ids=['Q08']);folder,run,_,_=self.save_trial(p)
        r=b.review(self.root,p['batch_id'],run['run_id'],'pass','Reviewed refusal and future limitation')
        self.assertEqual(r['groups'][0]['reference_usd_per_verified_success'],1)
        run['answer']['customer_message_ja']+=' changed';b.atomic_save(folder/'run.json',run)
        r=b.report(self.root,p['batch_id']);self.assertEqual(r['trials'][0]['human_review'],'pending');self.assertIsNone(r['groups'][0]['reference_total_usd'])

    def test_provenance_uses_job_metadata_not_declared_source(self):
        p=self.plan();_,run,e,m=self.save_trial(p)
        self.assertEqual(b.evidence_source(run,e,m)['status'],'matched_estimate')
        m['components']['bigquery_query']['job_evidence'][0]['referenced_tables'][0]['table_id']='unrelated'
        self.assertEqual(b.evidence_source(run,e,m)['status'],'failed')
        self.assertEqual(run['answer']['data_sources'],['v_revenue_events'])

    def test_resume_never_resubmits_failed_trial(self):
        p=self.plan(case_ids=['Q01']);self.save_trial(p,state='failed')
        with patch('workshop.benchmark.subprocess.Popen') as pop:b.execute(self.root,p['batch_id']);pop.assert_not_called()

    def test_worker_failure_saved_before_next_resume(self):
        p=self.plan(case_ids=['Q01'])
        with patch('workshop.benchmark.subprocess.Popen',side_effect=OSError('no SDK')) as pop:
            b.execute(self.root,p['batch_id']);b.execute(self.root,p['batch_id']);self.assertEqual(pop.call_count,1)
        self.assertEqual(b.report(self.root,p['batch_id'])['trials'][0]['attempt_state'],'failed')

    def test_interrupted_attempt_is_not_silently_retried(self):
        p=self.plan(case_ids=['Q01']);folder=self.root/'artifacts/benchmarks'/p['batch_id']
        b.atomic_save(b.attempt_path(folder,p['trials'][0]),{'state':'running'})
        with patch('workshop.benchmark.subprocess.Popen') as pop:b.execute(self.root,p['batch_id']);pop.assert_not_called()
        self.assertEqual(b.report(self.root,p['batch_id'])['trials'][0]['attempt_state'],'interrupted')

    def test_extra_dimensions_allowed_but_required_value_still_checked(self):
        p=self.plan(case_ids=['Q03']);a=copy.deepcopy(p['cases']['Q03']['expected']);a['dimensions']['product_name']='extra'
        self.save_trial(p,answer=a);self.assertTrue(b.report(self.root,p['batch_id'])['trials'][0]['structured_success'])
        a['dimensions']['product_id']='wrong';self.save_trial(p,answer=a)
        self.assertFalse(b.report(self.root,p['batch_id'])['trials'][0]['structured_success'])

    def test_report_escapes_content_and_limits_project_and_path(self):
        p=self.plan();self.save_trial(p);r=b.report(self.root,p['batch_id']);r['trials'][0]['question']='<script>alert(1)</script>'
        output=render(r);self.assertNotIn('<script>',output);self.assertIn('&lt;script&gt;',output)
        self.assertIn('<caption>',output);self.assertIn('scope="row"',output)
        cfg=p['context']['config'];self.assertIsNotNone(page(self.root,cfg,p['batch_id']))
        self.assertIsNone(page(self.root,dict(cfg,project_id='different-project'),p['batch_id']))
        self.assertIsNone(page(self.root,cfg,'../'+p['batch_id']))

    def test_timeout_kills_worker_and_is_not_retried(self):
        import subprocess
        p=self.plan(case_ids=['Q01']);worker=MagicMock();worker.wait.side_effect=subprocess.TimeoutExpired('root',480)
        with patch('workshop.benchmark.subprocess.Popen',return_value=worker) as pop, patch('workshop.benchmark.kill_worker') as kill:
            b.execute(self.root,p['batch_id']);b.execute(self.root,p['batch_id'])
            kill.assert_called_once_with(worker);self.assertEqual(pop.call_count,1)
        self.assertEqual(b.report(self.root,p['batch_id'])['trials'][0]['attempt_state'],'timeout')

    def test_stale_price_conversion_is_not_used(self):
        p=self.plan(case_ids=['Q01']);folder,run,e,m=self.save_trial(p)
        m['pricing_sha256']='different';b.atomic_save(folder/'economics.json',m)
        g=b.report(self.root,p['batch_id'])['groups'][0]
        self.assertIsNone(g['reference_total_usd']);self.assertEqual(g['known_subtotal_usd'],0)

    def test_correct_future_abstention_rejects_extraneous_measures(self):
        p=self.plan(case_ids=['Q08']);a=copy.deepcopy(p['cases']['Q08']['expected'])
        a['measures']={'gross_profit_jpy':{'value':60000,'unit':'JPY'}};self.save_trial(p,answer=a)
        row=b.report(self.root,p['batch_id'])['trials'][0]
        self.assertFalse(row['structured_success']);self.assertIn('measure_keys_mismatch',row['errors'])
    def test_three_policies_are_frozen_and_only_router_displays_match(self):
        p=self.plan(routes=('data_agent','verified_sql','router'))
        self.assertEqual(len(p['trials']),6)
        r=b.report(self.root,p['batch_id'])
        self.assertEqual([g['show_route_match'] for g in r['groups']],[False,False,True])
        for t in p['trials']:
            command=b.trial_command(self.root,p,t)
            self.assertIn(command[command.index('--route')+1],('data_agent','verified_sql','router'))
            self.assertNotIn(t['case_id'],command)
        self.assertEqual(render(r).count('<th scope="row">期待経路との一致</th>'),1)
        p=self.plan();r=b.report(self.root,p['batch_id'])
        self.assertNotIn('<th scope="row">期待経路との一致</th>',render(r))

    def test_summit_preset_compares_fetching_twice_and_reusing(self):
        preset=b.PRESETS['summit']
        p=self.plan(case_ids=preset['cases'],repeats=preset['repeats'],pairs=preset['variants'])
        self.assertEqual(len(p['trials']),6)
        self.assertEqual([v['label'] for v in p['variants']],['Data Agent・毎回取得','Data Agent・再利用'])
        self.assertEqual([(v['cache'],v['route_policy']) for v in p['variants']],preset['variants'])
        self.assertEqual({t['case_id'] for t in p['trials']},{'Q01','Q04','Q08'})
        r=b.report(self.root,p['batch_id'])
        self.assertEqual([g['label'] for g in r['groups']],['Data Agent・毎回取得','Data Agent・再利用'])
        html=render(r)
        self.assertIn('<h2>Data Agent・毎回取得</h2>',html);self.assertIn('Q01 · Data Agent・再利用 · 1 回目',html)
        self.assertNotIn('<h2>gemini-3.8-flash-off</h2>',html)

    def test_explicit_pairs_keep_route_labels_for_the_challenge(self):
        pairs=[('off','data_agent'),('on','data_agent'),('on','verified_sql'),('on','router')]
        p=self.plan(case_ids=['Q01','Q04','Q08'],pairs=pairs)
        self.assertEqual(len(p['trials']),12)
        r=b.report(self.root,p['batch_id'])
        self.assertEqual([g['label'] for g in r['groups']],['Data Agent・毎回取得','Data Agent・再利用','決まった SQL','振り分け'])
        self.assertIn('Q01 · 振り分け · 1 回目',render(r))
        with self.assertRaises(ValueError):self.plan(pairs=[('on','data_agent'),('on','data_agent')])
        with self.assertRaises(ValueError):self.plan(pairs=[('maybe','data_agent')])

    def test_links_show_both_root_traces_and_the_reusing_data_agent_trace(self):
        import contextlib,io
        preset=b.PRESETS['summit']
        p=self.plan(case_ids=preset['cases'],repeats=preset['repeats'],pairs=preset['variants'],preset='summit')
        off=b.q01_trial(p,'off');on=b.q01_trial(p,'on')
        for trial,trace in ((off,'off-root'),(on,'on-root')):
            b.atomic_save(self.root/'artifacts/lab2'/trial['run_id']/'run.json',{'trace_console_url':'https://console.cloud.google.com/traces/explorer;traceId='+trace+'?project=participant-project'})
        out=io.StringIO()
        with patch.object(b,'data_agent_traces',return_value=['da']) as found,contextlib.redirect_stdout(out):
            b.print_links(self.root,{'project_id':'participant-project'},p,attempts=1)
        text=out.getvalue()
        self.assertIn('Root Agent の処理（Data Agent・毎回取得）：https://console.cloud.google.com/traces/explorer;traceId=off-root',text)
        self.assertIn('Root Agent の処理（Data Agent・再利用）：https://console.cloud.google.com/traces/explorer;traceId=on-root',text)
        self.assertLess(text.index('off-root'),text.index('on-root'))
        self.assertIn('Data Agent の処理（Data Agent・再利用）：',text)
        self.assertEqual(found.call_args[0][2]['trace_console_url'].split('traceId=')[1].split('?')[0],'on-root')

    def test_preset_plan_records_its_preset_and_resumes_until_every_question_is_sent(self):
        preset=b.PRESETS['summit']
        p=self.plan(case_ids=preset['cases'],repeats=preset['repeats'],pairs=preset['variants'],preset='summit')
        self.assertEqual(b.load_plan(self.root,p['batch_id'])[1]['preset'],'summit')
        self.assertTrue(b.unfinished(self.root,p))
        folder=self.root/'artifacts/benchmarks'/p['batch_id']
        for trial in p['trials']:b.atomic_save(b.attempt_path(folder,trial),{'state':'completed'})
        self.assertFalse(b.unfinished(self.root,p))
        b.atomic_save(b.attempt_path(folder,p['trials'][-1]),{'state':'running'})
        self.assertTrue(b.unfinished(self.root,p))
        b.atomic_save(b.attempt_path(folder,p['trials'][-1]),{'state':'interrupted'})
        self.assertFalse(b.unfinished(self.root,p))
        chosen=b.representative_trial(p)
        self.assertEqual(chosen['case_id'],'Q01');self.assertEqual(b.label_of(p,chosen['variant']),'Data Agent・再利用')

    def test_data_agent_trace_is_found_only_when_no_other_run_can_explain_it(self):
        cfg={'project_id':'participant-project','dataset_id':'ec'}
        agent='projects/participant-project/locations/us/dataAgents/ec-poc-view-x'
        run={'run_id':'a'*32,'config':cfg,'data_agent':agent,'started_at':'2026-10-12T04:00:00+00:00','finished_at':'2026-10-12T04:01:00+00:00'}
        b.atomic_save(self.root/'artifacts/lab2'/run['run_id']/'run.json',run)
        def trace(ident,start,end):
            return {'traceId':ident,'spans':[{'startTime':start,'endTime':end,'labels':{'gen_ai.agent.id':agent}}]}
        payload={'traces':[trace('inside','2026-10-12T04:00:10Z','2026-10-12T04:00:20Z'),trace('outside','2026-10-12T05:00:10Z','2026-10-12T05:00:20Z')]}
        with patch('workshop.cloud.Client') as client:
            client.return_value.request.return_value=payload
            self.assertEqual(b.data_agent_traces(self.root,cfg,run),['inside'])
            url=client.return_value.request.call_args[0][1]
            self.assertIn('participant-project/traces?',url)
        other=dict(run,run_id='b'*32);b.atomic_save(self.root/'artifacts/lab2'/other['run_id']/'run.json',other)
        with patch('workshop.cloud.Client') as client:
            client.return_value.request.return_value=payload
            self.assertEqual(b.data_agent_traces(self.root,cfg,run),[])
        self.assertEqual(b.data_agent_traces(self.root,cfg,{'run_id':'c'*32}),[])
        open_run={k:v for k,v in other.items() if k!='finished_at'}
        b.atomic_save(self.root/'artifacts/lab2'/other['run_id']/'run.json',open_run)
        with patch('workshop.cloud.Client') as client:
            client.return_value.request.return_value=payload
            self.assertEqual(b.data_agent_traces(self.root,cfg,run),[])

    def test_links_print_trace_urls_or_how_to_try_again(self):
        import contextlib,io
        p=self.plan()
        cfg={'project_id':'participant-project','dataset_id':'ec'}
        out=io.StringIO()
        with patch.object(b,'data_agent_traces',return_value=['abc']),contextlib.redirect_stdout(out):
            b.print_links(self.root,cfg,p,attempts=1)
        self.assertIn('Root Agent の処理（Data Agent・再利用）：記録がありません',out.getvalue())
        self.assertIn('Data Agent の処理（Data Agent・再利用）：https://console.cloud.google.com/traces/explorer;traceId=abc',out.getvalue())
        out=io.StringIO()
        with patch.object(b,'data_agent_traces',return_value=[]),patch.object(b.time,'sleep') as sleep,contextlib.redirect_stdout(out):
            b.print_links(self.root,cfg,p,attempts=2,wait=1)
        self.assertEqual(sleep.call_count,1)
        self.assertIn('workshop.benchmark links',out.getvalue())

    def test_label_appends_model_only_when_several_models_are_compared(self):
        self.assertEqual(b.variant_label('gemini-3.8-flash','off','data_agent'),'Data Agent・毎回取得')
        self.assertEqual(b.variant_label('gemini-3.8-flash','off','verified_sql'),'決まった SQL（毎回取得）')
        self.assertEqual(b.variant_label('gemini-2.5-flash','on','router',2),'振り分け（gemini-2.5-flash）')

    def test_legacy_plan_without_labels_still_shows_variant_names(self):
        p=self.plan()
        for v in p['variants']:v.pop('label')
        self.assertEqual(b.label_of(p,p['variants'][0]['name']),p['variants'][0]['name'])
        r=b.report(self.root,p['batch_id']);self.assertTrue(r['groups'][0]['label'])

    def test_group_shows_an_upper_bound_only_when_every_gap_is_bounded(self):
        p=self.plan(case_ids=['Q01','Q02'])
        self.save_trial(p,0,amount=0.25)
        folder,_,_,m=self.save_trial(p,1,amount=None)
        r=b.report(self.root,p['batch_id']);self.assertIsNone(r['groups'][0]['reference_total_upper_usd'])
        m['reference_total_upper_usd']=0.5;b.atomic_save(folder/'economics.json',m)
        r=b.report(self.root,p['batch_id']);g=r['groups'][0]
        self.assertIsNone(g['reference_total_usd']);self.assertAlmostEqual(g['reference_total_upper_usd'],0.75)
        self.assertIn('未算出（最大 0.750000 USD）',render(r))

    def test_latest_plan_is_the_newest_for_this_project_only(self):
        first=self.plan();second=self.plan()
        cfg=first['context']['config']
        self.assertEqual(b.latest_plan(self.root,cfg)['batch_id'],second['batch_id'])
        other=dict(cfg,project_id='someone-else')
        with self.assertRaises(ValueError):b.latest_plan(self.root,other)
        self.assertEqual(b.jst('2026-10-11T14:01:15.406794+00:00'),'10/11 23:01')
        self.assertEqual(b.jst(None),'日時不明')

    def test_legacy_or_self_declared_organizer_review_is_not_participant_success(self):
        p=self.plan(case_ids=['Q08']);_,run,_,_=self.save_trial(p)
        r=b.review(self.root,p['batch_id'],run['run_id'],'pass','Checked refusal')
        self.assertTrue(r['trials'][0]['verified_success'])
        folder=self.root/'artifacts/benchmarks'/p['batch_id'];path=next((folder/'reviews').glob('*.json'))
        record=b.read(path);self.assertEqual(record['reviewer_scope'],'participant');self.assertFalse(record['official_eligible'])
        record.pop('reviewer_scope');b.atomic_save(path,record)
        self.assertFalse(b.report(self.root,p['batch_id'])['trials'][0]['verified_success'])
        record['reviewer_scope']='organizer';b.atomic_save(path,record)
        result=b.report(self.root,p['batch_id']);self.assertFalse(result['trials'][0]['verified_success'])
        self.assertIsNone(result['ranking']);self.assertIsNone(result['trials'][0]['official_review'])

class ReportTraceLinkTests(unittest.TestCase):
    """反復評価レポートから代表実行の Trace へ辿れること。新しい推論は伴わない。"""

    BASE = {'batch_id':'0123456789abcdef','created_at':'2026-09-19T00:00:00Z','language':'ja',
            'groups':[],'limitations':['例'],'pricing_version':'2026-09-06','review_scope':'participant'}

    @staticmethod
    def trial(url):
        return {'run_id':'a'*32,'trace_console_url':url,'case_id':'Q01','variant':'v','repetition':1,
                'question':'問','attempt_state':'completed','outcome':'answered','structured_success':True,
                'errors':[],'provenance':{'status':'matched_estimate'},'human_review':'pending',
                'answer':{},'evidence':{},'routing':{},'reference_total_usd':None,'known_subtotal_usd':0}

    def render(self, url):
        from workshop.benchmark_report import render
        return render({**self.BASE, 'trials':[self.trial(url)]})

    def test_console_url_becomes_a_link(self):
        html = self.render('https://console.cloud.google.com/traces/list?project=x')
        self.assertIn('Cloud Trace で処理を開く', html)
        self.assertIn('新しい推論は行いません', html)

    def test_missing_url_does_not_promise_a_link_elsewhere(self):
        # 両画面は同じ run.json を読むため、画面を変えれば出るとは案内しない。
        # ページ上部のナビゲーションに「02 Observability」リンクがあるのは別件なので、
        # 案内文そのものを見る。
        html = self.render(None)
        self.assertNotIn('Cloud Trace で処理を開く ↗</a>', html)
        self.assertNotIn('「02 Observability」の保存記録を選び', html)
        self.assertIn('benchmark report', html)
        self.assertIn('Trace 未確認', html)

    def test_untrusted_url_is_not_linked(self):
        for url in ('javascript:alert(1)', 'http://example.com', 'https://example.com/traces'):
            with self.subTest(url=url):
                html = self.render(url)
                self.assertNotIn('href="'+url, html)
                self.assertIn('Trace 未確認', html)


if __name__ == '__main__':
    unittest.main()

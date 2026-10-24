import copy
import tempfile
import unittest
from pathlib import Path
from workshop.economics import (read, PRICE_PATH, reconcile, sql_key, root_cost, native_cost, digest, atomic_save, export_recorded)
from workshop.web import public_record


class EconomicsTests(unittest.TestCase):
    def setUp(self):
        self.prices = read(PRICE_PATH)
        self.run = {'run_id':'a'*32, 'started_at':'2026-09-06T05:00:00Z', 'finished_at':'2026-09-06T05:01:00Z',
            'state':'completed','data_agent':'projects/participant-project/locations/us/dataAgents/test-agent',
            'physical_call_attempts':1, 'root_model_requested':'gemini-2.5-flash',
            'config':{'bigquery_location':'US'}, 'root_model_usage':[{'event_id':'root-event','model_version':'gemini-2.5-flash',
                'usage':{'prompt_token_count':1000,'candidates_token_count':100,'thoughts_token_count':50,'total_token_count':1150,'traffic_type':'ON_DEMAND'}}]}
        labels = {'gen_ai.agent.id':self.run['data_agent'],'gcp.vertex.agent.event_id':'native-event',
            'gen_ai.usage.input_tokens':'2000','gen_ai.usage.output_tokens':'200','gen_ai.usage.reasoning.output_tokens':'50'}
        span = {'startTime':'2026-09-06T05:00:10Z','endTime':'2026-09-06T05:00:30Z','labels':labels}
        self.trace = {'traceId':'native-trace','spans':[span,copy.deepcopy(span)]}
        self.job = {'job_id':'job-one','creation_time':'2026-09-06T05:00:15Z','end_time':'2026-09-06T05:00:16Z',
            'labels':{'ca-bq-job':'true','data-agent-id':'test-agent'},'query':"SELECT 'A B'",'state':'DONE',
            'statement_type':'SELECT','total_bytes_processed':10,'total_bytes_billed':10485760,'cache_hit':False,
            'reservation_id':None,'edition':None}
        self.evidence = [{'sql':["SELECT  'A B'" ]}]

    def calculate(self, **changes):
        args = dict(run=self.run,evidence=self.evidence,jobs=[self.job],traces=[self.trace],runs=[self.run],prices=self.prices,mode='on-demand',completeness={'traces':True,'jobs':True})
        args.update(changes)
        return reconcile(**args)

    def test_failed_run_keeps_only_known_conversions_without_full_total(self):
        failed=dict(self.run,state='failed')
        r=self.calculate(run=failed,runs=[failed])
        self.assertIsNone(r['reference_total_usd'])
        self.assertGreater(r['known_subtotal_usd'],0)
        for c in r['components'].values():
            self.assertIsNone(c['reference_usd']);self.assertGreater(c['known_reference_usd'],0)
        r=self.calculate(run=failed,runs=[failed],traces=[])
        self.assertIsNone(r['components']['data_agent']['known_reference_usd'])

    def test_measured_amount_is_never_invoice(self):
        r=self.calculate()
        self.assertAlmostEqual(r['reference_total_usd'],0.000675+0.010+10485760/(1024**4)*6.25)
        self.assertIsNone(r['actual_billed_usd'])
        self.assertEqual(r['components']['data_agent']['attribution'],'time_agent_estimate')
        self.assertEqual(r['components']['data_agent']['usage']['output_tokens'],200)
        self.assertEqual(r['components']['data_agent']['usage']['events'],1)
        self.assertEqual(r['components']['data_agent']['trial_through'],'2026-12-31')

    def test_two_jobs_in_one_run_are_not_ambiguity(self):
        run=dict(self.run,physical_call_attempts=2)
        trace=copy.deepcopy(self.trace);trace['traceId']='second'
        for s in trace['spans']:s['labels']['gcp.vertex.agent.event_id']='second-event'
        r=self.calculate(run=run,runs=[run],jobs=[self.job,dict(self.job,job_id='two')],traces=[self.trace,trace])
        self.assertIsNotNone(r['reference_total_usd'])
        self.assertEqual(r['components']['bigquery_query']['usage']['jobs'],2)

    def test_extra_candidate_job_gives_an_upper_bound_but_no_total(self):
        extra=dict(self.job,job_id='extra',query='SELECT 2',creation_time='2026-09-06T05:00:20Z')
        r=self.calculate(jobs=[self.job,extra])
        bq=r['components']['bigquery_query']
        self.assertIsNone(r['reference_total_usd']);self.assertIsNone(bq['reference_usd'])
        self.assertEqual(bq['reasons'],['生成SQLにないジョブ候補または別名照合の競合あり'])
        self.assertAlmostEqual(bq['upper_bound_usd'],2*10485760/(1024**4)*6.25)
        self.assertAlmostEqual(r['reference_total_upper_usd'],0.000675+0.010+bq['upper_bound_usd'])

    def test_missing_job_or_other_doubt_gives_no_upper_bound(self):
        r=self.calculate(evidence=[{'sql':["SELECT  'A B'","SELECT 3"]}])
        self.assertIsNone(r['components']['bigquery_query']['upper_bound_usd']);self.assertIsNone(r['reference_total_upper_usd'])
        extra=dict(self.job,job_id='extra',query='SELECT 2')
        r=self.calculate(jobs=[self.job,extra],mode='unknown')
        self.assertIsNone(r['components']['bigquery_query']['upper_bound_usd']);self.assertIsNone(r['reference_total_upper_usd'])

    def test_model_price_is_not_used_after_its_end_date(self):
        def run_at(day):
            return dict(self.run,started_at=day+'T05:00:00Z',root_model_requested='gemini-3.8-flash',
                        root_model_usage=[dict(self.run['root_model_usage'][0],model_version='gemini-3.8-flash')])
        self.assertIsNotNone(root_cost(run_at('2026-12-31'),self.prices)['reference_usd'])
        late=root_cost(run_at('2027-01-01'),self.prices)
        self.assertIsNone(late['reference_usd']);self.assertIsNone(late['known_reference_usd'])
        self.assertIn('モデル単価の適用期限切れ（2026-12-31まで）',late['reasons'])

    def test_one_job_matching_two_runs_is_not_counted(self):
        other=dict(self.run,run_id='b'*32)
        r=self.calculate(runs=[self.run,other])
        self.assertIsNone(r['reference_total_usd'])
        for k in ['data_agent','bigquery_query']:
            self.assertEqual(r['components'][k]['attribution'],'ambiguous')
            self.assertIsNone(r['components'][k]['reference_usd'])

    def test_missing_or_truncated_telemetry_is_not_zero(self):
        for changes in [dict(traces=[]),dict(jobs=[]),dict(completeness={'jobs':False,'traces':True}),dict(completeness={'jobs':True,'traces':False})]:
            self.assertIsNone(self.calculate(**changes)['reference_total_usd'])

    def test_capacity_unknown_location_or_reservation_blocks_bytes_conversion(self):
        for changes in [dict(mode='unknown'),dict(mode='capacity'),dict(jobs=[dict(self.job,reservation_id='reservation')]),dict(run=dict(self.run,config={'bigquery_location':'EU'}))]:
            r=self.calculate(**changes)
            self.assertIsNone(r['reference_total_usd'])
            self.assertIsNotNone(r['components']['bigquery_query']['usage'])

    def test_conflicting_native_event_does_not_sum(self):
        t=copy.deepcopy(self.trace);t['spans'][1]['labels']['gen_ai.usage.output_tokens']='201'
        self.assertIsNone(self.calculate(traces=[t])['components']['data_agent']['reference_usd'])

    def test_missing_event_id_and_malformed_token_rejected(self):
        for key,value in [('gcp.vertex.agent.event_id',None),('gen_ai.usage.input_tokens','-1')]:
            t=copy.deepcopy(self.trace)
            for s in t['spans']:s['labels'][key]=value
            self.assertIsNone(self.calculate(traces=[t])['components']['data_agent']['reference_usd'])

    def test_sql_formatting_keeps_literal_and_token_boundaries(self):
        self.assertEqual(sql_key("SELECT\n 'A B', (x)"),sql_key("SELECT 'A B',( x )"))
        self.assertNotEqual(sql_key("SELECT 'A B'"),sql_key("SELECT 'A  B'"))
        self.assertNotEqual(sql_key("SELECT 'A B'"),sql_key("SELECT 'a b'"))
        self.assertNotEqual(sql_key('SELECT ab'),sql_key('SELECT a b'))
        self.assertIsNone(self.calculate(jobs=[dict(self.job,query="SELECT 'a b'")])['reference_total_usd'])

    def test_failure_script_or_unknown_usage_stays_incomplete(self):
        for j in [dict(self.job,error_result={'reason':'error'}),dict(self.job,statement_type='SCRIPT'),dict(self.job,parent_job_id='parent'),dict(self.job,total_bytes_billed=None)]:
            self.assertIsNone(self.calculate(jobs=[j])['reference_total_usd'])
        self.assertIsNone(self.calculate(run=dict(self.run,state='failed'))['reference_total_usd'])

    def test_explicit_cache_hit_is_zero_bq_not_zero_total(self):
        r=self.calculate(jobs=[dict(self.job,cache_hit=True,total_bytes_billed=None)])
        self.assertEqual(r['components']['bigquery_query']['reference_usd'],0)
        self.assertGreater(r['reference_total_usd'],0)

    def test_root_event_duplicates_conflicts_and_cache(self):
        run=copy.deepcopy(self.run);run['root_model_usage']*=2
        self.assertEqual(root_cost(run,self.prices)['usage']['input_tokens'],1000)
        run=copy.deepcopy(self.run);run['root_model_usage'][0]['usage']['cached_content_token_count']=100
        self.assertAlmostEqual(root_cost(run,self.prices)['reference_usd'],0.000648)
        run['root_model_usage'][0]['usage']['total_token_count']=1151
        self.assertIsNone(root_cost(run,self.prices)['reference_usd'])

    def test_unknown_model_and_nonstandard_traffic_are_not_priced(self):
        run=copy.deepcopy(self.run);run['root_model_requested']='unknown'
        self.assertIsNone(root_cost(run,self.prices)['reference_usd'])
        run=copy.deepcopy(self.run);run['root_model_usage'][0]['usage']['traffic_type']='PROVISIONED_THROUGHPUT'
        self.assertIsNone(root_cost(run,self.prices)['reference_usd'])

    def test_changed_original_invalidates_sidecar_in_web(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);atomic_save(folder/'evidence-1.json',self.evidence[0]);atomic_save(folder/'economics.json',self.calculate())
            self.assertIsNotNone(public_record(self.run,folder)['economics'])
            self.assertIsNone(public_record(dict(self.run,state='failed'),folder)['economics'])
            atomic_save(folder/'evidence-1.json',{'sql':['SELECT 2']})
            self.assertIsNone(public_record(self.run,folder)['economics'])

    def test_a_trace_without_usage_does_not_hide_behind_another_trace(self):
        run=dict(self.run,physical_call_attempts=2)
        t=copy.deepcopy(self.trace);t['traceId']='empty'
        for span in t['spans']:
            span['labels']={'gen_ai.agent.id':self.run['data_agent']}
        self.assertIsNone(self.calculate(run=run,runs=[run],traces=[self.trace,t])['reference_total_usd'])

    def test_export_keeps_numeric_summary_and_protects_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'artifacts/lab2'/self.run['run_id']
            run=dict(self.run,config={'project_id':'participant-project','bigquery_location':'US'},
                     options={'evidence_cache_enabled':True},elapsed_seconds=30,data_agent_calls=1,
                     private_identity='must-not-export')
            report=self.calculate(run=run,runs=[run]);report['components']['data_agent']['trace_ids']=['private-trace']
            atomic_save(folder/'run.json',run);atomic_save(folder/'economics.json',report)
            atomic_save(folder/'evidence-1.json',self.evidence[0]);output=root/'example.json'
            export_recorded(root,run['config'],[run['run_id']],output)
            text=output.read_text()
            for private in ('participant-project','must-not-export','private-trace',self.run['run_id'],'user_email'):
                self.assertNotIn(private,text)
            self.assertEqual(read(output)['records'][0]['components']['data_agent']['usage']['input_tokens'],2000)
            with self.assertRaises(FileExistsError):export_recorded(root,run['config'],[run['run_id']],output)

if __name__=='__main__':unittest.main()

import copy
import json
import re
import unittest
from unittest.mock import patch
from workshop import routes
from workshop.settings import ROOT,config
from workshop.checkpoint import standard_rules
from workshop.data import connect
from workshop.reference import all_answers
from workshop.cloud import CloudError
from workshop.economics import reconcile,read,PRICE_PATH,job_candidates
from workshop.benchmark import evidence_source


class RoutesTests(unittest.TestCase):
    def setUp(self):
        self.cfg={**config(),'project_id':'participant-project','bigquery_location':'US'}
        self.rules=standard_rules(ROOT)
        self.cases=[json.loads(x) for x in (ROOT/'data/public_cases.jsonl').read_text().splitlines()]

    def test_capability_boundary_both_languages_without_evaluation_metadata(self):
        for c in self.cases:
            for language in ('ja','en'):
                with self.subTest(case=c['case_id'],language=language):
                    sql=c['case_id'] not in ('Q04','Q08')
                    selected=routes.choose('router',c['question_'+language],self.cfg,self.rules,ROOT)
                    self.assertEqual(selected['selected_route'],'verified_sql' if sql else 'data_agent')
                    fixed=routes.choose('verified_sql',c['question_'+language],self.cfg,self.rules,ROOT)
                    self.assertEqual(fixed['selected_route'],'verified_sql' if sql else None)
                    self.assertNotIn('case_id',selected)

    def test_unsupported_words_dates_and_changed_definition_do_not_trigger_sql(self):
        for question in ('2026年8月の純売上はいくらですか。返品を除いて。','2026年9月の純売上はいくらですか。','2026年13月の粗利益はいくらですか。','2026年8月の粗利益はいくらですか。 DROP TABLE orders'):
            self.assertIsNone(routes.parse_question(question,self.cfg))
        value=routes.choose('router',self.cases[0]['question_ja'],self.cfg,self.rules+'\nChanged meaning',ROOT)
        self.assertEqual(value['selected_route'],'data_agent');self.assertEqual(value['decision_reason'],'UNSUPPORTED_DEFINITION')

    def test_templates_against_independent_fixture_totals_and_rankings(self):
        db=connect();answers={a['case_id']:a for a in all_answers()}
        try:
            for c in self.cases:
                request=routes.parse_question(c['question_ja'],self.cfg)
                if request is None:continue
                spec=routes.query_spec(request,self.cfg)
                query=re.sub(r'`[^`]+`','revenue_events',spec['query'])
                row=db.execute(query,{'start':request['period']['start'],'end':request['period']['end_exclusive']}).fetchone()
                self.assertEqual(row[request['metric']],answers[c['case_id']]['measures'][request['metric']]['value'])
                if request['group_by']:self.assertEqual(row['product_id'],answers[c['case_id']]['dimensions']['product_id'])
        finally:db.close()

    def test_sql_identifiers_and_job_ids_cannot_be_injected(self):
        request=routes.parse_question(self.cases[0]['question_ja'],self.cfg)
        with self.assertRaises(ValueError):routes.query_spec({**request,'metric':'x); DROP TABLE orders'},self.cfg)
        with self.assertRaises(ValueError):routes.query_spec(request,{**self.cfg,'project_id':'bad`project'})
        with self.assertRaises(ValueError):routes.job_url(self.cfg,'../other')

    def records(self):
        rid='a'*32;jid='ec_'+rid+'_1';request=routes.parse_question(self.cases[0]['question_ja'],self.cfg);spec=routes.query_spec(request,self.cfg)
        raw={'jobReference':{'jobId':jid,'projectId':self.cfg['project_id'],'location':'US'},'status':{'state':'DONE'},
             'configuration':{'query':spec,'labels':{'workshop-route':'verified_sql'}},
             'statistics':{'creationTime':str(int(routes.dt.datetime.fromisoformat('2026-09-06T05:00:10+00:00').timestamp()*1000)),'endTime':str(int(routes.dt.datetime.fromisoformat('2026-09-06T05:00:11+00:00').timestamp()*1000)),'query':{'statementType':'SELECT','cacheHit':False,'totalBytesProcessed':'814','totalBytesBilled':'41943040','referencedTables':[{'projectId':self.cfg['project_id'],'datasetId':self.cfg['dataset_id'],'tableId':'orders'}]}}}
        job=routes.job_evidence(raw)
        run={'run_id':rid,'implementation_version':'root-v3','state':'completed','config':self.cfg,'started_at':'2026-09-06T05:00:00Z','finished_at':'2026-09-06T05:01:00Z',
             'physical_call_attempts':0,'root_model_invoked':True,'root_model_requested':'gemini-2.5-flash',
             'root_model_usage':[{'event_id':'one','model_version':'gemini-2.5-flash','usage':{'prompt_token_count':10,'candidates_token_count':5,'total_token_count':15,'traffic_type':'ON_DEMAND'}}],
             'routing':{'policy':'verified_sql','selected_route':'verified_sql','events':[{'route':'verified_sql','job_id':jid}]},
             'direct_jobs':[{'job_id':jid,'query':spec['query'],'query_parameters':spec['queryParameters'],'location':'US'}]}
        evidence=[{'sql':[spec['query']],'query_parameters':spec['queryParameters']}]
        return raw,job,run,evidence

    def test_direct_job_matching_uses_id_parameters_project_and_time(self):
        _,job,run,evidence=self.records()
        self.assertEqual(job_candidates(job,[run]),[run['run_id']])
        for key,value in [('job_id','wrong'),('project_id','other-project'),('query_parameters',[]),('location','EU'),('creation_time','2026-09-05T00:00:00Z')]:
            with self.subTest(key=key):self.assertEqual(job_candidates({**job,key:value},[run]),[])
        cost=reconcile(run,evidence,[job],[],[run],read(PRICE_PATH),'on-demand',{'traces':False,'jobs':False})
        self.assertEqual(cost['components']['data_agent']['reference_usd'],0)
        self.assertEqual(cost['components']['bigquery_query']['attribution'],'direct_job_id')
        self.assertIsNotNone(cost['reference_total_usd'])
        self.assertEqual(evidence_source(run,evidence,cost)['status'],'matched_direct')
        job['referenced_tables']=[];job['cache_hit']=True
        cost=reconcile(run,evidence,[job],[],[run],read(PRICE_PATH),'on-demand',{'traces':False,'jobs':False})
        self.assertEqual(evidence_source(run,evidence,cost)['status'],'unverified')

    def test_missing_direct_job_or_failed_run_is_not_zero(self):
        _,job,run,evidence=self.records()
        for jobs,state in [([], 'completed'),([job],'failed')]:
            r={**run,'state':state};cost=reconcile(r,evidence,jobs,[],[r],read(PRICE_PATH),'on-demand',{'traces':False,'jobs':False})
            self.assertIsNone(cost['reference_total_usd'])
        cost=reconcile(run,evidence,[job],[],[run],read(PRICE_PATH),'capacity',{'traces':False,'jobs':False})
        self.assertIsNone(cost['components']['bigquery_query']['reference_usd'])

    def test_unsupported_completed_route_has_no_invocations_not_missing_usage(self):
        _,_,run,_=self.records();run.update(root_model_invoked=False,root_model_usage=[],direct_jobs=[])
        run['routing'].update(selected_route=None,events=[])
        cost=reconcile(run,[],[],[],[run],read(PRICE_PATH),'unknown',{'traces':False,'jobs':False})
        self.assertEqual(cost['reference_total_usd'],0)
        self.assertTrue(all(c['usage_kind']=='not_invoked' for c in cost['components'].values()))

    def test_sql_submission_is_once_and_preserves_natural_cache(self):
        raw,job,run,_=self.records();result={'jobComplete':True,'schema':{'fields':[{'name':'net_sales_jpy','type':'INTEGER'}]},'rows':[{'f':[{'v':'240000'}]}]}
        with patch.object(routes.Client,'request',side_effect=[raw,result]) as request:
            value=routes.execute_sql(self.cfg,routes.parse_question(self.cases[0]['question_ja'],self.cfg),job['job_id'])
            self.assertEqual(value['tables'][0]['rows'],[[240000]])
            self.assertEqual([c.args[0] for c in request.call_args_list],['POST','GET'])
            self.assertNotIn('useQueryCache',request.call_args_list[0].args[2]['configuration']['query'])
        with patch.object(routes.Client,'request',side_effect=CloudError('uncertain submission')) as request:
            with self.assertRaises(CloudError):routes.execute_sql(self.cfg,routes.parse_question(self.cases[0]['question_ja'],self.cfg),job['job_id'])
            self.assertEqual(request.call_count,1)

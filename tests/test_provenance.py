import copy
import unittest
from workshop import provenance as p
from workshop.benchmark import evidence_source


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'project_id':'participant-project','dataset_id':'ec_workshop','bigquery_location':'US'}
        self.refs=[{'project_id':self.cfg['project_id'],'dataset_id':self.cfg['dataset_id'],'table_id':'orders'}]
        content={'project_id':self.cfg['project_id'],'dataset_id':self.cfg['dataset_id'],'agent':{'name':'projects/participant-project/locations/us/dataAgents/agent-one'},'tables':{}}
        for table in ('orders','refunds','v_revenue_events'):
            content['tables']['participant-project.ec_workshop.'+table]={'lastModifiedTime':str(int(p.epoch('2026-09-06T04:00:00Z')*1000)),'view':{'query':'SELECT same_view'} if table=='v_revenue_events' else None}
        self.before={'complete':True,'captured_at':'2026-09-06T05:00:00Z','content':content,'sha256':p.sha(content)}
        self.after={**copy.deepcopy(self.before),'captured_at':'2026-09-06T05:03:00Z'}
        self.plan={'context':{'config':self.cfg},'trials':[{'run_id':'r1'},{'run_id':'r2'}],'cloud_snapshot':self.before}
        self.source={'job_id':'s1','project_id':'participant-project','query':'SELECT amount FROM v_revenue_events','creation_time':'2026-09-06T05:01:00Z','end_time':'2026-09-06T05:01:01Z','cache_hit':False,'state':'DONE','statement_type':'SELECT','referenced_tables':self.refs,'labels':{'data-agent-id':'agent-one','ca-bq-job':'true'},'principal_sha256':p.sha('participant')}
        self.target={**copy.deepcopy(self.source),'job_id':'t1','creation_time':'2026-09-06T05:02:00Z','end_time':'2026-09-06T05:02:01Z','cache_hit':True,'referenced_tables':[]}
        self.measurements={'r1':self.measure(self.source),'r2':self.measure(self.target)}

    def measure(self,job):return {'components':{'bigquery_query':{'attribution':'time_agent_sql_estimate','job_evidence':[job]}}}
    def result(self):return p.supplement(self.plan,self.measurements,self.after)['r2']['t1']

    def test_valid_estimate_preserves_empty_observed_references(self):
        r=self.result();self.assertEqual(r['status'],'estimated');self.assertEqual(r['estimated_referenced_tables'],self.refs)
        self.assertEqual(self.target['referenced_tables'],[]);self.assertEqual(r['candidates'][0]['source_job_id'],'s1')
        self.assertTrue(all(r['candidates'][0]['conditions'].values()))
        run={'config':self.cfg};e=[{'sql':[self.target['query']]}]
        self.assertEqual(evidence_source(run,e,self.measurements['r2'])['status'],'unverified')
        self.assertEqual(evidence_source(run,e,self.measurements['r2'],{'t1':r})['status'],'matched_estimate')

    def test_exact_sql_not_lexical_equivalence(self):
        self.source['query']=self.target['query'].replace('SELECT ','SELECT  ')
        self.assertEqual(self.result()['status'],'unverified')

    def test_other_plan_and_cache_chains_are_not_candidates(self):
        self.plan['trials']=[{'run_id':'r2'}];self.assertEqual(self.result()['status'],'unverified')
        self.plan['trials'].append({'run_id':'r1'});self.source['cache_hit']=True
        self.assertEqual(self.result()['status'],'unverified')

    def test_window_is_a_search_limit_not_cache_proof(self):
        self.source['creation_time']='2026-09-05T05:01:00Z';self.assertEqual(self.result()['status'],'unverified')
        self.target['cache_hit']=None;self.assertNotIn('r2',p.supplement(self.plan,self.measurements,self.after))

    def test_missing_snapshot_cannot_be_backfilled_for_old_plan(self):
        self.plan.pop('cloud_snapshot');self.assertEqual(self.result()['status'],'unverified')

    def test_changed_view_agent_or_table_blocks_estimate(self):
        for field in ('view','agent','table'):
            with self.subTest(field=field):
                snapshot=copy.deepcopy(self.after)
                if field=='agent':self.after['content']['agent']['name']='different'
                else:self.after['content']['tables']['participant-project.ec_workshop.'+('orders' if field=='table' else 'v_revenue_events')]['lastModifiedTime']='changed'
                self.after['sha256']=p.sha(self.after['content']);self.assertEqual(self.result()['status'],'unverified');self.after=snapshot

    def test_missing_and_changed_candidate_conditions_fail_closed(self):
        changes={'principal_sha256':None,'project_id':'other-project','labels':{},'state':'RUNNING','statement_type':'SCRIPT','parent_job_id':'parent','error_result':{'reason':'failed'},'cache_hit':None,'end_time':None,'referenced_tables':[],'creation_time':None}
        for key,value in changes.items():
            with self.subTest(key=key):
                old=self.source.get(key);had=key in self.source;self.source[key]=value
                self.assertEqual(self.result()['status'],'unverified')
                if had:self.source[key]=old
                else:del self.source[key]

    def test_candidate_finishing_after_target_is_not_accepted(self):
        self.source['end_time']='2026-09-06T05:02:30Z';self.assertEqual(self.result()['status'],'unverified')

    def test_multiple_agreeing_candidates_are_estimate_conflicts_are_not(self):
        other=copy.deepcopy(self.source);other.update(job_id='s2',creation_time='2026-09-06T05:01:10Z',end_time='2026-09-06T05:01:11Z')
        self.measurements['r1']['components']['bigquery_query']['job_evidence'].append(other)
        self.assertEqual(self.result()['status'],'estimated')
        other['referenced_tables']=[{**self.refs[0],'table_id':'refunds'}]
        self.assertEqual(self.result()['status'],'unverified')

    def test_unknown_candidate_is_not_ignored_in_favor_of_good_candidate(self):
        other=copy.deepcopy(self.source);other.update(job_id='s2',creation_time=None)
        self.measurements['r1']['components']['bigquery_query']['job_evidence'].append(other)
        self.assertEqual(self.result()['status'],'unverified')

    def test_table_or_view_updated_after_source_blocks_even_matching_snapshots(self):
        for name in ('orders','v_revenue_events'):
            with self.subTest(name=name):
                a=copy.deepcopy(self.before);b=copy.deepcopy(self.after)
                for snap in (self.before,self.after):
                    snap['content']['tables']['participant-project.ec_workshop.'+name]['lastModifiedTime']=str(int(p.epoch('2026-09-06T05:01:30Z')*1000));snap['sha256']=p.sha(snap['content'])
                self.assertEqual(self.result()['status'],'unverified');self.before=a;self.after=b;self.plan['cloud_snapshot']=self.before

    def test_ambiguous_run_link_and_disallowed_references_block(self):
        self.measurements['r1']['components']['bigquery_query']['ambiguous_job_ids']=['s1']
        self.assertEqual(self.result()['status'],'unverified')
        self.measurements['r1']['components']['bigquery_query'].pop('ambiguous_job_ids');self.source['referenced_tables']=[{**self.refs[0],'table_id':'private_table'}]
        self.assertEqual(self.result()['status'],'unverified')

if __name__=='__main__':unittest.main()

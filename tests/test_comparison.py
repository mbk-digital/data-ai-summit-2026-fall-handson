import copy
import unittest
from workshop import economics as e
from workshop.benchmark import common_timing,evidence_source


class ComparisonTests(unittest.TestCase):
    def test_alias_fallback_preserves_direct_and_lexical_levels(self):
        a='SELECT SUM(x) AS MONTH FROM `p.d.t`'
        self.assertEqual(e.compare_sql(a,a)['method'],'exact_text')
        self.assertEqual(e.compare_sql(a,a.replace('SUM(x)','SUM( x )'))['method'],'lexical')
        b=a.replace('AS MONTH','AS month')
        result=e.compare_sql(a,b)
        self.assertEqual(result['method'],'select_alias_case_estimate')
        self.assertEqual(result['changes'][0]['expected'],'MONTH')
        self.assertEqual(result['changes'][0]['actual'],'month')
        self.assertEqual(e.compare_sql(a,b,False)['method'],'unmatched')

    def test_sensitive_identifiers_literals_types_and_unsupported_syntax_not_folded(self):
        pairs=[('SELECT X AS total FROM TableA','SELECT X AS total FROM tablea'),
               ('SELECT f(X) AS total FROM t','SELECT F(X) AS total FROM t'),
               ("SELECT 'A' AS n FROM t","SELECT 'a' AS n FROM t"),
               ('SELECT `A` AS n FROM t','SELECT `a` AS n FROM t'),
               ('SELECT CAST(x AS MyEnum) AS n FROM t','SELECT CAST(x AS myenum) AS n FROM t'),
               ('SELECT AS MyType x FROM t','SELECT AS mytype x FROM t'),
               ('SELECT x AS N FROM t GROUP BY N','SELECT x AS n FROM t GROUP BY n'),
               ('WITH c AS (SELECT 1 AS X) SELECT X FROM c','WITH c AS (SELECT 1 AS x) SELECT X FROM c'),
               ('SELECT (SELECT 1 AS N) FROM t','SELECT (SELECT 1 AS n) FROM t'),
               ('SELECT x AS N /* note */ FROM t','SELECT x AS n /* note */ FROM t'),
               ('SELECT x AS `N` FROM t','SELECT x AS `n` FROM t'),
               ('SELECT x FROM t AS T','SELECT x FROM t AS t'),
               ('SELECT x AS N FROM t; SELECT 2','SELECT x AS n FROM t; SELECT 2')]
        for a,b in pairs:
            with self.subTest(a=a):self.assertEqual(e.compare_sql(a,b)['method'],'unmatched')

    def test_alias_candidates_are_not_chosen_when_ambiguous(self):
        expected=['SELECT 1 AS Aa','SELECT 1 AS aA']
        self.assertFalse(e.match_sql_queries(expected,['SELECT 1 AS aa'])['complete'])
        self.assertEqual(e.match_sql_queries(expected,['SELECT 1 AS aa'])['unmatched_actual'][0]['reason'],'ambiguous_alias_candidates')
        proof=e.match_sql_queries(expected,expected)
        self.assertTrue(proof['complete'])
        self.assertEqual([p['method'] for p in proof['matches']],['exact_text','exact_text'])
        self.assertFalse(e.match_sql_queries(expected,[expected[0]])['complete'])

    def test_alias_cost_is_estimate_and_reference_metadata_still_required(self):
        from tests.test_economics import EconomicsTests
        fixture=EconomicsTests();fixture.setUp()
        fixture.run['config'].update(project_id='participant-project',dataset_id='ec_workshop')
        fixture.evidence=[{'sql':['SELECT 1 AS MONTH']}]
        fixture.job.update(query='SELECT 1 AS month',referenced_tables=[{'project_id':'participant-project','dataset_id':'ec_workshop','table_id':'orders'}])
        result=fixture.calculate()
        self.assertIsNotNone(result['reference_total_usd'])
        self.assertEqual(result['components']['bigquery_query']['sql_matching']['matches'][0]['method'],'select_alias_case_estimate')
        proof=evidence_source(fixture.run,fixture.evidence,result)
        self.assertEqual(proof['status'],'matched_alias_estimate')
        fixture.job['referenced_tables']=[]
        self.assertEqual(evidence_source(fixture.run,fixture.evidence,fixture.calculate())['status'],'unverified')
        fixture.job['query']='SELECT 2 AS month'
        self.assertIsNone(fixture.calculate()['reference_total_usd'])

    def rows(self):
        rows=[]
        for name,times in [('a',[10,100,30]),('b',[20,1,40])]:
            for i,seconds in enumerate(times,1):
                rows.append({'case_id':'Q0'+str(i),'repetition':1,'variant':name,'attempt_state':'completed',
                    'errors':[],'elapsed_seconds':seconds,'outcome':'answered','structured_success':True})
        return rows,[{'name':'a'},{'name':'b'}]

    def test_common_timing_keeps_wrong_answers_and_valid_abstention_and_same_pairs(self):
        rows,variants=self.rows()
        rows[0].update(structured_success=False,errors=['wrong_value'])
        rows[4].update(outcome='unsupported',structured_success=False)
        rows[2].update(outcome='abstained')
        rows[5].update(outcome='abstained')
        result=common_timing(rows,variants)
        self.assertEqual(result['pairs'],[{'case_id':'Q01','repetition':1},{'case_id':'Q03','repetition':1}])
        self.assertEqual(result['groups']['a'],{'median_seconds':20,'trials':2})
        self.assertEqual(result['groups']['b'],{'median_seconds':30,'trials':2})
        self.assertEqual(result['excluded'][0]['case_id'],'Q02')
        rows[5].update(structured_success=False)
        self.assertEqual(common_timing(rows,variants)['groups']['a']['trials'],1)

    def test_common_timing_rejects_partial_missing_duration_or_identity(self):
        rows,variants=self.rows()
        for field,value in [('elapsed_seconds',None),('elapsed_seconds',float('nan')),('elapsed_seconds',True),('errors',['run_identity_or_frozen_config_mismatch']),('attempt_state','failed')]:
            sample=copy.deepcopy(rows);sample[0][field]=value
            self.assertEqual(common_timing(sample,variants)['groups']['b']['trials'],2)
        rows[0]['attempt_state']='pending'
        result=common_timing(rows,variants)
        self.assertEqual(result['status'],'pending');self.assertEqual(result['pairs'],[])
        self.assertIsNone(result['groups']['a']['median_seconds'])

    def test_repetitions_are_paired_without_selecting_fastest_run(self):
        rows,variants=self.rows();extra=copy.deepcopy(rows)
        for r in extra:r.update(repetition=2,elapsed_seconds=200)
        extra[-1].update(outcome='unsupported',structured_success=False)
        result=common_timing(rows+extra,variants)
        self.assertEqual(result['groups']['a']['trials'],5)
        self.assertIn({'case_id':'Q03','repetition':1},result['pairs'])
        self.assertNotIn({'case_id':'Q03','repetition':2},result['pairs'])
        self.assertEqual(common_timing(rows[:3],[variants[0]])['status'],'single_variant')

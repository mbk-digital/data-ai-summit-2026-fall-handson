import asyncio
import copy
import unittest
from workshop.root_evidence import compact_response, RequestEvidence, finalize, calculate_change, required_tool, strip_json_fence
from workshop.observability import summarize_traces


class RootEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.e = {'status':'SUCCESS','sql':['select ...'], 'tables':[{'headers':['category','previous','current'], 'rows':[['Example',40,-24]]}]}
        self.a = {'status':'OK','reason_code':'NONE','measure_items':[{'name':'gross_profit_delta_jpy','value':-64,'unit':'JPY'}], 'dimension_items':[{'name':'category','value':'Example'}], 'customer_message_ja':'説明'}
        self.cfg = {'definition_version':'test'}

    def test_strip_json_fence_only_removes_one_surrounding_fence(self):
        body='{\n  "status": "OK"\n}'
        self.assertEqual(strip_json_fence(body),body)
        self.assertEqual(strip_json_fence('```json\n'+body+'\n```'),body)
        self.assertEqual(strip_json_fence('  ```\n'+body+'\n```\n'),body)
        for text in ('```python\n'+body+'\n```','説明\n```json\n'+body+'\n```','```json\n{}\n```\n```json\n{}\n```','```json\n{'):
            self.assertEqual(strip_json_fence(text),text)

    def test_required_tools_prevent_skipping_evidence_and_comparison(self):
        self.assertEqual(required_tool([],[]),'analyze_ec')
        e={'status':'SUCCESS','tables':[{'headers':['category','previous_gross_profit_jpy','current_gross_profit_jpy']}]}
        self.assertEqual(required_tool([e],[]),'compute_change')
        self.assertIsNone(required_tool([e],[{'value':-64}]))
        self.assertIsNone(required_tool([{'status':'SUCCESS','tables':[]}],[]))

    def test_filters_progress(self):
        r = compact_response({'status':'SUCCESS','response':[{'text':{'textType':'THOUGHT','parts':['progress']}},{'text':{'textType':'FINAL_RESPONSE','parts':['answer']}}]})
        self.assertEqual(r['final_text'],['answer'])
        self.assertEqual(compact_response({'status':'SUCCESS','response':[]})['status'],'ERROR')

    def test_fault_and_cache_are_request_local(self):
        async def scenario(enabled):
            calls=[]
            async def fetch(key, consumer): calls.append(key); return copy.deepcopy(self.e)
            store=RequestEvidence(enabled,fetch)
            await store.both('question')
            self.assertEqual(len(calls),1 if enabled else 2)
            other=RequestEvidence(enabled,fetch)
            await other.both('question')
            self.assertEqual(len(calls),2 if enabled else 4)
        asyncio.run(scenario(False));asyncio.run(scenario(True))

    def test_failed_fetch_is_not_cached_or_counted_as_success(self):
        async def scenario():
            calls=[]
            async def fail(key, consumer): calls.append(key);return {'status':'ERROR'}
            s=RequestEvidence(True,fail);await s.both('x');await s.both('x')
            self.assertEqual(len(calls),2);self.assertFalse(s.cache)
        asyncio.run(scenario())

    def test_change_uses_table_cells_and_signed_direction(self):
        calc=calculate_change([self.e],0,0,'previous','current')
        self.assertEqual(calc['value'],-64)
        r=finalize(self.a,[self.e],self.cfg,[calc]);self.assertEqual(r['status'],'OK')
        self.assertEqual(r['measures']['gross_profit_delta_jpy']['value'],-64)

    def test_unrecorded_delta_rejected(self):
        self.e['tables'][0]['rows'][0].append(-64)
        self.assertEqual(finalize(self.a,[self.e],self.cfg)['status'],'ERROR')

    def test_invalid_numeric_coordinates(self):
        for index in (-1,True):
            with self.assertRaises(ValueError): calculate_change([self.e],index,0,'previous','current')
        self.e['tables'][0]['rows'][0][1]=True
        with self.assertRaises(ValueError): calculate_change([self.e],0,0,'previous','current')

    def test_insufficient_has_no_measures(self):
        self.a['status']='INSUFFICIENT_DATA';self.a['reason_code']='FUTURE_OUTCOME_NOT_OBSERVED'
        r=finalize(self.a,[self.e],self.cfg)
        self.assertFalse(r['measures']);self.assertEqual(r['status'],'INSUFFICIENT_DATA')

    def test_error_not_insufficient(self):
        self.a['status']='INSUFFICIENT_DATA'
        r=finalize(self.a,[{'status':'ERROR'}],self.cfg)
        self.assertEqual(r['status'],'ERROR');self.assertFalse(r['measures'])

    def test_missing_numeric_evidence_is_error(self):
        self.a['measure_items'][0]={'name':'net_sales_jpy','value':999,'unit':'JPY'}
        self.assertEqual(finalize(self.a,[self.e],self.cfg)['status'],'ERROR')

    def test_native_usage_deduplicates_nested_spans(self):
        s={'name':'call_llm','labels':{'gcp.vertex.agent.event_id':'e1','gen_ai.usage.input_tokens':'12','gen_ai.usage.output_tokens':'3'}}
        payload={'traces':[{'traceId':'t','spans':[{'labels':{'gen_ai.agent.id':'agent'}},s,dict(s,name='generate_content')]}]}
        r=summarize_traces(payload)[0]
        self.assertEqual(r['model_events'],1);self.assertEqual(r['tokens']['gen_ai.usage.input_tokens'],12)
        self.assertIsNone(r['tokens']['gen_ai.usage.reasoning.output_tokens'])
        payload['traces'][0]['spans'].append({'labels':dict(s['labels'],**{'gen_ai.usage.input_tokens':'13'})})
        self.assertIsNone(summarize_traces(payload)[0]['tokens']['gen_ai.usage.input_tokens'])

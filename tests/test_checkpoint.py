"""Guarded checkpoint tests: all cloud reads use an in-memory service."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from workshop import checkpoint as cp
from workshop.settings import ROOT,config
from workshop.catalog import as_document,concept


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('config/workshop.json','semantics/lab1-starter.txt','requirements-adk.txt'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
        self.put('config/workshop.local.json',{'project_id':'participant-project','bigquery_location':'US','data_agent_location':'us','catalog_location':'us','runtime_location':'us-central1'})
        self.cfg=config(True,self.root);self.rules=cp.standard_rules(self.root)
        self.put('participant/semantics.txt',self.rules);self.put('participant/lab2.json',{'evidence_cache_enabled':True})
        i=cp.Inspector(self.root,check_prerequisites=False)
        self.catpath=i.catalog.ledger_path;self.obspath=i.obs.path
        self.ident='ec-lab1-'+hashlib.sha256(self.cfg['dataset_id'].encode()).hexdigest()[:12]
        self.group=i.catalog.parent+'/entryGroups/'+self.ident;self.kind=i.catalog.parent+'/entryTypes/'+self.ident
        self.entry=self.group+'/entries/business-rules';self.doc=as_document(self.rules);fields,body=concept(self.doc)
        self.metadata={'displayName':'EC lab1 definitions','description':'workshop_cli:summit_ec_v1:catalog','labels':cp.LABELS}
        self.ctx=cp.make_contexts(self.cfg,self.rules,{'title':fields['title'],'body':body})['view']
        self.aid='ec-poc-view-'+cp.fingerprint(self.ctx);self.aname=i.agent.parent+'/dataAgents/'+self.aid;self.apath=i.agent.folder/(self.aid+'.resource.json')
        self.base=cp.dataset_path(i.c);self.obsname=i.obs.name;self.binding=self.obsname+'/settingBindings/workshop-project'
        self.cloud={self.group:{'name':self.group,**self.metadata},self.kind:{'name':self.kind,**self.metadata},
            self.entry:{'name':self.entry,'entryType':self.kind,'entrySource':{'displayName':fields['title'],'description':fields['description'],'location':'us'},'aspects':{'overview':{'data':{'content':self.doc,'contentType':'MARKDOWN'}}}},
            self.aname:{'name':self.aname,'description':'workshop_cli:summit_ec_v1:'+cp.fingerprint(self.ctx),'dataAnalyticsAgent':{'publishedContext':self.ctx}},
            self.base:{'location':'US','labels':cp.LABELS},self.base+'/tables/v_revenue_events':{'type':'VIEW','view':{'query':cp.expected_view_query(self.cfg)}},
            self.obsname:{'name':self.obsname,'conversationalAnalyticsSetting':{'metricsEnabled':True,'tracesEnabled':True}},self.binding:{'name':self.binding,'target':'projects/participant-project','product':'GEMINI_IN_BIGQUERY'}}
        for name in (self.group,self.kind,self.entry,self.aname,self.obsname,self.binding):
            self.cloud[name].update(createTime='2026-09-01T00:00:00Z',updateTime='2026-09-01T01:00:00Z')
        for t in cp.fixture():self.cloud[self.base+'/tables/'+t]={'type':'TABLE','schema':{'fields':cp.expected_table_fields(t)}}
        self.rows={t:sorted(v) for t,v in cp.fixture().items()};self.calls=[]
        def request(client,method,url,*args,**kwargs):
            self.calls.append((method,url));self.assertEqual(method,'GET','diagnosis must never mutate cloud')
            name=url.split('?',1)[0]
            for prefix in (cp.API,cp.BASE,i.agent.base):
                if name.startswith(prefix):name=name[len(prefix):];break
            if name.endswith('/gibqObservabilitySettings'):
                return {'gibqObservabilitySettings':[self.cloud[self.obsname]] if self.obsname in self.cloud else []}
            if name.startswith('https://cloudresourcemanager.googleapis.com'):return {'projectNumber':'12345'}
            if name==self.base+'/tables':return {'tables':[{'tableReference':{'tableId':k.rsplit('/',1)[1]}} for k in self.cloud if k.startswith(self.base+'/tables/')]}
            return copy.deepcopy(self.cloud.get(name))
        for target,new in ((cp.Client,'request'),):
            p=patch.object(target,new,request);p.start();self.addCleanup(p.stop)
        p=patch.object(cp.Catalog,'overview',lambda cat,e:next(iter(e['aspects'].items())));p.start();self.addCleanup(p.stop)
        p=patch.object(cp,'table_rows',lambda c,t:copy.deepcopy(self.rows[t]));p.start();self.addCleanup(p.stop)
        original=cp.diagnose
        p=patch.object(cp,'diagnose',lambda root=self.root,account=None,overrides=None,**kw:original(root,account,overrides,False));p.start();self.addCleanup(p.stop)

    def put(self,key,value):
        p=self.root/key;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(value if isinstance(value,str) else json.dumps(value))

    def scan(self):return cp.diagnose(self.root)

    def own(self):
        result=self.scan()
        for change in result['adoptions'].values():
            p=self.root/change['path'];v=cp.read(p)
            if change['entry']:v[change['entry']]=change['value']
            else:v=change['value']
            self.put(change['path'],v)
        self.put(str(self.obspath.relative_to(self.root)),{n:{'state':'present','snapshot':self.cloud[n]} for n in (self.obsname,self.binding)})
        return self.scan()

    def test_standard_definition_matches_lab1_instructions(self):
        instructions=(ROOT/'docs/labs/lab1.md').read_text()
        for text in ('refunded_date（返金日）','recovered_cost_jpy（実際に回収できた原価）'):
            self.assertIn(text,self.rules);self.assertIn(text,instructions)
        self.assertNotIn('[[',self.rules)

    def test_three_starting_states(self):
        lost=self.scan();self.assertEqual(lost['classification'],'recovery_needed');self.assertFalse(lost['blocked']);self.assertEqual(len(lost['adoptions']),5)
        existing=self.own();self.assertTrue(existing['ready']);self.assertEqual(existing['classification'],'continue_existing')
        shutil.rmtree(self.root/'artifacts');self.cloud={}
        first=self.scan();self.assertEqual(first['classification'],'first_setup');self.assertFalse(first['blocked']);self.assertFalse(first['ready'])

    def test_missing_definition_uses_trusted_catalog(self):
        (self.root/'participant/semantics.txt').unlink();r=self.scan()
        self.assertEqual(r['local_writes']['participant/semantics.txt'],self.doc)
        self.assertEqual(self.aname,next(i['resource_id'] for i in r['items'] if i['kind']=='data_agent'))

    def test_edited_incomplete_definition_is_not_overwritten(self):
        self.put('participant/semantics.txt','my changes [[unfinished]]');r=self.scan()
        self.assertTrue(r['blocked']);self.assertNotIn('participant/semantics.txt',r['local_writes'])

    def test_cloud_mismatch_or_partial_unowned_dataset_blocks(self):
        for modification in ('data','view','missing_tables'):
            with self.subTest(modification=modification):
                cloud=copy.deepcopy(self.cloud);rows=copy.deepcopy(self.rows)
                if modification=='data':self.rows['products']=[]
                elif modification=='view':self.cloud[self.base+'/tables/v_revenue_events']['view']['query']='SELECT 1'
                else:
                    for k in list(self.cloud):
                        if k.startswith(self.base+'/tables/'):del self.cloud[k]
                r=self.scan();self.assertTrue(r['blocked']);self.assertNotIn(self.base,r['adoptions'])
                self.cloud=cloud;self.rows=rows

    def test_read_error_is_not_classified_as_absence(self):
        with patch.object(cp.Catalog,'get',side_effect=cp.CloudError('403 unavailable')):
            r=self.scan();self.assertTrue(r['blocked']);self.assertEqual(r['classification'],'diagnosis_incomplete')

    def test_normalized_catalog_type_uses_uid_not_spelling(self):
        self.cloud[self.kind]['uid']='type-one'
        numeric='projects/12345/locations/us/entryTypes/'+self.ident
        self.cloud[numeric]={**self.cloud[self.kind],'name':numeric}
        self.cloud[self.entry]['entryType']=numeric
        r=self.scan();entry=next(i for i in r['items'] if i['kind']=='catalog_entry')
        self.assertTrue(entry['matches']);self.assertFalse(r['blocked'])
        self.cloud[numeric]['uid']='another-type'
        self.assertTrue(self.scan()['blocked'])

    def test_unowned_observability_is_used_but_never_adopted(self):
        r=self.scan();self.assertNotIn(self.obsname,r['adoptions'])
        self.assertEqual({i['action'] for i in r['items'] if i['kind']=='observability'},{'use_existing_unowned'})

    def test_blocked_plan_and_missing_approval_cannot_apply(self):
        p=cp.write_plan(self.root)
        with self.assertRaisesRegex(cp.CloudError,'全ID'):cp.apply(self.root,p['plan_id'])
        with self.assertRaisesRegex(cp.CloudError,'全ID'):cp.apply(self.root,p['plan_id'],list(p['adoptions'])[:-1])
        self.put('participant/semantics.txt','[[edited]]');p=cp.write_plan(self.root)
        with self.assertRaisesRegex(cp.CloudError,'未確認'):cp.apply(self.root,p['plan_id'])

    def test_changed_local_cloud_and_source_block_apply(self):
        self.own()
        for change in ('local','cloud','source'):
            with self.subTest(change=change):
                p=cp.write_plan(self.root)
                if change=='local':self.put('participant/lab2.json',{'evidence_cache_enabled':False})
                elif change=='cloud':self.cloud[self.entry]['updateTime']='changed'
                else:self.put('workshop/new_module.py','# changed source')
                with self.assertRaises(cp.CloudError):cp.apply(self.root,p['plan_id'])
                if change=='local':self.put('participant/lab2.json',{'evidence_cache_enabled':True})
                elif change=='cloud':self.cloud[self.entry].pop('updateTime')

    def test_backup_tampering_and_local_edits_block_restore(self):
        self.own();path=self.root/'backup.json';cp.backup(self.root,path)
        self.put('participant/semantics.txt','edited')
        with self.assertRaisesRegex(cp.CloudError,'現在の編集'):cp.restore_backup(self.root,path)
        self.put('participant/semantics.txt',self.rules)
        v=cp.read(path);v['payload']['files']['participant/semantics.txt']='tampered';path.write_text(json.dumps(v))
        with self.assertRaisesRegex(cp.CloudError,'内容・版'):cp.restore_backup(self.root,path)

    def test_restore_lost_ledgers_writes_only_verified_missing_files(self):
        self.own();path=self.root/'backup.json';cp.backup(self.root,path)
        original=cp.read(path)['payload']['files'];shutil.rmtree(self.root/'artifacts');shutil.rmtree(self.root/'participant')
        self.assertFalse(self.scan()['ready']);self.assertTrue(cp.restore_backup(self.root,path)['ready'])
        self.assertFalse(self.catpath.exists())
        cp.restore_backup(self.root,path,True)
        for key,value in original.items():self.assertEqual((self.root/key).read_text(),value)
        self.assertTrue(self.scan()['ready'])

    def test_extra_and_deleted_catalog_records_report_the_resource_id(self):
        self.own();records=cp.read(self.catpath);other=self.group+'/entries/previous-definition'
        records[other]={'state':'deleted','snapshot':{'name':other}};self.catpath.write_text(json.dumps(records))
        path=self.root/'backup.json';cp.backup(self.root,path);self.catpath.unlink()
        with self.assertRaisesRegex(cp.CloudError,'previous-definition'):cp.restore_backup(self.root,path)
        self.assertFalse(self.catpath.exists())

    def test_old_agent_is_checked_individually(self):
        self.own();name=self.aname[:-16]+'a'*16;value=copy.deepcopy(self.cloud[self.aname]);value['name']=name;self.cloud[name]=value
        self.put(str(self.apath.with_name(name.rsplit('/',1)[1]+'.resource.json').relative_to(self.root)),{'name':name,'state':'present','description':value['description'],'resource':value})
        path=self.root/'backup.json';cp.backup(self.root,path);cp.restore_backup(self.root,path)
        self.cloud[name]['description']='changed'
        with self.assertRaisesRegex(cp.CloudError,'a'*16):cp.restore_backup(self.root,path)

    def test_current_only_backup_explicitly_lists_excluded_old_ledgers(self):
        self.own();old=self.apath.with_name('ec-poc-defined-'+'b'*16+'.resource.json')
        old.write_text(json.dumps({'state':'deleted'}))
        path=self.root/'current-only.json';cp.backup(self.root,path,True)
        payload=cp.read(path)['payload'];key=str(old.relative_to(self.root))
        self.assertEqual(payload['excluded_agent_ledgers'],[key]);self.assertNotIn(key,payload['files'])
        self.assertTrue(old.exists());cp.restore_backup(self.root,path)

    def test_parent_symlink_and_interrupted_temp_file_are_preserved(self):
        outside=self.root/'outside';outside.mkdir();(self.root/'linked').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(cp.CloudError):cp.write_local(self.root,'linked/new','x',self.root/'before')
        self.assertFalse((outside/'new').exists())
        temporary=self.root/'participant/lab2.json.checkpoint.tmp';temporary.write_text('unfinished')
        with self.assertRaisesRegex(cp.CloudError,'別名へ退避'):cp.write_local(self.root,'participant/lab2.json','x',self.root/'before')
        self.assertEqual(temporary.read_text(),'unfinished');self.assertEqual(json.loads((self.root/'participant/lab2.json').read_text()),{'evidence_cache_enabled':True})

    def test_cloud_snapshot_reads_and_validates_metadata_without_mutation(self):
        from workshop.provenance import capture,same_snapshot
        for name,value in self.cloud.items():
            if name.startswith(self.base+'/tables/'):
                value.update(creationTime='1',lastModifiedTime='2',id=name)
        a=capture(self.root);b=capture(self.root)
        self.assertTrue(a['complete']);self.assertTrue(same_snapshot(a,b));self.assertEqual(len(a['content']['tables']),6)
        self.cloud[self.base+'/tables/v_revenue_events']['view']['query']='SELECT changed'
        self.assertFalse(capture(self.root)['complete'])

    def test_cost_permission_is_separate_from_execution_prerequisites(self):
        self.own()
        for access in ('granted','missing','unknown'):
            with self.subTest(access=access), patch.object(cp,'preflight',return_value={'can_attempt_stage':True}) as preflight, patch.object(cp.importlib.metadata,'version',return_value='2.8.0'), patch.object(cp,'cost_access',side_effect=cp.CloudError('unavailable') if access=='unknown' else None,return_value={'job_metadata_access':access,'missing_permissions':[] if access=='granted' else ['bigquery.jobs.listAll'],'actual_collection_verified':False}), patch.object(cp,'log_access',return_value={'standard_logs':'granted','data_access_logs':'granted','missing_permissions':[],'audit_config_verified':False,'actual_read_verified':False}):
                r=cp.Inspector(self.root).scan()
                self.assertTrue(r['ready']);self.assertFalse(r['blocked']);self.assertEqual(r['cost_collection']['job_metadata_access'],access)
                self.assertEqual(bool(r['warnings']),access!='granted');self.assertEqual(preflight.call_args.args[1],'lab2')
        with patch.object(cp,'preflight',return_value={'can_attempt_stage':False}), patch.object(cp.importlib.metadata,'version',return_value='2.8.0'), patch.object(cp,'cost_access',return_value={'job_metadata_access':'granted'}), patch.object(cp,'log_access',return_value={'standard_logs':'granted','data_access_logs':'granted','missing_permissions':[]}):
            self.assertTrue(cp.Inspector(self.root).scan()['blocked'])

    def test_log_permission_check_separates_standard_and_data_access(self):
        client=cp.Client(self.cfg)
        with patch.object(client,'request',return_value={'permissions':['logging.logEntries.list']}) as request:
            result=cp.log_access(client)
        self.assertEqual(request.call_args.args[2],{'permissions':['logging.logEntries.list','logging.privateLogEntries.list']})
        # 通常ログは見られるが Data Access ログは見られない、を区別できること。
        self.assertEqual(result['standard_logs'],'granted')
        self.assertEqual(result['data_access_logs'],'missing')
        self.assertEqual(result['missing_permissions'],['logging.privateLogEntries.list'])
        # 監査設定の有効化と実際の読み取りは、権限とは別に未確認のままにする。
        self.assertFalse(result['audit_config_verified'])
        self.assertFalse(result['actual_read_verified'])

    def test_log_permission_shortage_does_not_block_execution(self):
        self.own()
        with patch.object(cp,'preflight',return_value={'can_attempt_stage':True}), patch.object(cp.importlib.metadata,'version',return_value='2.8.0'), \
             patch.object(cp,'cost_access',return_value={'job_metadata_access':'granted','missing_permissions':[],'actual_collection_verified':False}), \
             patch.object(cp,'log_access',return_value={'standard_logs':'granted','data_access_logs':'missing','missing_permissions':['logging.privateLogEntries.list'],'audit_config_verified':False,'actual_read_verified':False}):
            r=cp.Inspector(self.root).scan()
        # ログ閲覧の不足は Root 実行の可否と別。開始は止めない。
        self.assertTrue(r['ready']);self.assertFalse(r['blocked'])
        self.assertEqual(r['log_access']['data_access_logs'],'missing')
        self.assertTrue(any('ログ閲覧' in w for w in r['warnings']))

    def test_cost_permission_check_requests_only_collection_permission(self):
        client=cp.Client(self.cfg)
        with patch.object(client,'request',return_value={'permissions':[]}) as request:
            self.assertEqual(cp.cost_access(client)['job_metadata_access'],'missing')
            self.assertEqual(request.call_args.args[2],{'permissions':['bigquery.jobs.listAll']})

    def test_missing_checkpoint_blocks_benchmark_before_any_trial(self):
        from workshop import benchmark
        with patch.object(benchmark,'config',return_value=self.cfg), patch.object(benchmark,'ROOT',self.root), patch.object(benchmark,'execute') as run, patch('sys.argv',['benchmark','run','--batch','0123456789abcdef']):
            self.assertEqual(benchmark.main(),2);run.assert_not_called()
        self.assertFalse((self.root/'artifacts/benchmarks').exists())

    def test_initial_apply_orders_components_and_creates_observability_folder(self):
        self.cloud={};(self.root/'participant/semantics.txt').unlink();(self.root/'participant/lab2.json').unlink()
        p=cp.write_plan(self.root);before=self.scan();after=copy.deepcopy(before);after.update(ready=True,blocked=False)
        order=[]
        fields,body=concept(as_document(cp.standard_rules(self.root)))
        with patch.object(cp,'diagnose',side_effect=[before,after]), patch.object(cp.subprocess,'run',side_effect=lambda command,**k:(self.assertEqual(command[-1],'lab2'),order.append('bigquery'))), patch.object(cp.Catalog,'sync',side_effect=lambda *a:order.append('catalog') or {'title':fields['title'],'body':body}), patch.object(cp.AgentClient,'ensure',side_effect=lambda *a:order.append('agent')), patch.object(cp.Observability,'enable',side_effect=lambda *a,**k:order.append('observability')):
            self.assertTrue(cp.apply(self.root,p['plan_id'])['ready'])
        self.assertEqual(order,['bigquery','catalog','agent','observability'])
        self.assertTrue((self.root/'artifacts/observability/.lock').exists())
        self.assertEqual((self.root/'participant/semantics.txt').read_text(),cp.standard_rules(self.root))

    def test_exact_adoption_approval_only_writes_verified_ledgers(self):
        p=cp.write_plan(self.root)
        with patch.object(cp.Catalog,'sync',side_effect=cp.CloudError('stop before remote mutation')):
            with self.assertRaisesRegex(cp.CloudError,'stop before'):cp.apply(self.root,p['plan_id'],list(p['adoptions']))
        self.assertTrue(self.catpath.exists());self.assertTrue(self.apath.exists())
        self.assertFalse(self.obspath.exists());self.assertTrue(self.scan()['ready'])

    def test_apply_records_failure_and_preserves_before_files(self):
        # 業務定義が #1 の雛形のまま → Catalog の保存定義で戻す。途中で失敗しても元のファイルは退避されている。
        self.own();starter=(self.root/'semantics/lab1-starter.txt').read_text();self.put('participant/semantics.txt',starter)
        p=cp.write_plan(self.root);self.assertEqual(p['local_writes'],{'participant/semantics.txt':self.doc})
        with patch.object(cp.Catalog,'sync',side_effect=cp.CloudError('controlled interruption')):
            with self.assertRaises(cp.CloudError):cp.apply(self.root,p['plan_id'])
        folder=self.root/'artifacts/checkpoints'/p['plan_id'];journal=cp.read(folder/'apply.json')
        self.assertEqual(journal['state'],'interrupted');self.assertEqual(journal['completed'],['verified_ledger_adoption','participant_files','bigquery'])
        self.assertEqual((folder/'before/participant/semantics.txt').read_text(),starter)
        self.assertEqual((self.root/'participant/semantics.txt').read_text(),self.doc)
        self.assertEqual(cp.read(self.root/'participant/lab2.json'),{'evidence_cache_enabled':True})

    def test_metadata_drift_warns_without_changing_ledgers(self):
        self.own();before=self.obspath.read_bytes()
        for name in (self.group,self.kind,self.entry,self.aname,self.obsname,self.binding):
            self.cloud[name]['updateTime']='2026-09-02T00:00:00Z'
        result=self.scan()
        self.assertTrue(result['ready']);self.assertEqual(len(result['metadata_changes']),6)
        self.assertTrue(result['warnings']);self.assertEqual(before,self.obspath.read_bytes())

    def test_content_identity_and_unknown_changes_still_block(self):
        self.own()
        for name,key,value in ((self.aname,'createTime','another-creation'),(self.obsname,'labels',{'changed':'yes'}),(self.group,'extra',True)):
            with self.subTest(name=name,key=key):
                before=copy.deepcopy(self.cloud[name]);self.cloud[name][key]=value
                self.assertTrue(self.scan()['blocked']);self.cloud[name]=before

    def test_metadata_backup_restores_original_and_logs_delta(self):
        self.own();path=self.root/'backup.json';cp.backup(self.root,path)
        original=self.apath.read_bytes();self.cloud[self.aname]['updateTime']='2026-09-02T00:00:00Z'
        self.apath.unlink()
        result=cp.restore_backup(self.root,path,True)
        self.assertTrue(result['ready']);self.assertTrue(result['metadata_changes'])
        self.assertEqual(original,self.apath.read_bytes())
        journal=next((self.root/'artifacts/checkpoints').glob('restore-*/restored.json'))
        self.assertTrue(cp.read(journal)['metadata_changes'])

    def test_refresh_is_explicit_and_backs_up_exact_originals(self):
        from workshop import ledger_refresh as refresh
        self.own();original=self.obspath.read_bytes()
        self.cloud[self.obsname]['updateTime']='2026-09-02T00:00:00Z'
        value=refresh.plan(self.root)
        self.assertFalse(value['blocked']);self.assertEqual(len(value['changes']),1)
        self.assertEqual(original,self.obspath.read_bytes())
        journal=refresh.apply(self.root,value['plan_id']);self.assertEqual(journal['state'],'completed')
        self.assertEqual(cp.read(self.obspath)[self.obsname]['snapshot'],self.cloud[self.obsname])
        folder=self.root/'artifacts/ledger-refresh'/value['plan_id']
        self.assertEqual(original,(folder/'before'/self.obspath.relative_to(self.root)).read_bytes())
        with self.assertRaises(cp.CloudError):refresh.apply(self.root,value['plan_id'])

    def add_api_defaults(self,bqaa=False,change_request=None):
        self.cloud[self.group]['changeRequestConfig']={} if change_request is None else change_request
        self.cloud[self.obsname]['conversationalAnalyticsSetting']['bqaaEnabled']=bqaa

    def test_api_defaults_on_old_ledgers_still_start_and_refresh_explicitly(self):
        from workshop import ledger_refresh as refresh
        self.own();ledgers=(self.catpath.read_bytes(),self.obspath.read_bytes())
        self.add_api_defaults()
        result=self.scan()
        self.assertEqual(result['classification'],'continue_existing');self.assertTrue(result['ready'])
        changed={x['resource_id']:x['changes'] for x in result['metadata_changes']}
        self.assertIn('changeRequestConfig',changed[self.group])
        self.assertIn('conversationalAnalyticsSetting.bqaaEnabled',changed[self.obsname])
        self.assertEqual(ledgers,(self.catpath.read_bytes(),self.obspath.read_bytes()))
        with patch('builtins.print'):self.assertTrue(cp.require_ready(self.root)['ready'])  # #3 の推論前の診断
        value=refresh.plan(self.root);self.assertFalse(value['blocked'])
        self.assertEqual({c['resource_id'] for c in value['changes']},{self.group,self.obsname})
        self.assertEqual(refresh.apply(self.root,value['plan_id'])['state'],'completed')
        self.assertEqual(cp.read(self.catpath)[self.group]['snapshot'],self.cloud[self.group])
        self.assertEqual(cp.read(self.obspath)[self.obsname]['snapshot'],self.cloud[self.obsname])
        result=self.scan();self.assertTrue(result['ready']);self.assertEqual(result['metadata_changes'],[])

    def test_api_defaults_in_a_new_project_scan_are_ready(self):
        self.add_api_defaults()
        self.assertTrue(self.own()['ready'])

    def test_enabled_or_non_default_api_fields_still_block(self):
        self.own()
        for bqaa,change_request in ((True,None),(False,{'requireApproval':True})):
            with self.subTest(bqaa=bqaa,change_request=change_request):
                group,setting=copy.deepcopy(self.cloud[self.group]),copy.deepcopy(self.cloud[self.obsname])
                self.add_api_defaults(bqaa,change_request)
                self.assertTrue(self.scan()['blocked'])
                self.cloud[self.group],self.cloud[self.obsname]=group,setting

    def test_refresh_does_not_adopt_missing_ledgers(self):
        from workshop import ledger_refresh as refresh
        value=refresh.plan(self.root);self.assertEqual(value['changes'],[])
        self.assertFalse(self.catpath.exists());self.assertFalse(self.apath.exists())

    def test_refresh_blocks_content_changes_and_stale_plans(self):
        from workshop import ledger_refresh as refresh
        self.own();original=self.obspath.read_bytes()
        self.cloud[self.obsname]['labels']={'new':'setting'}
        value=refresh.plan(self.root);self.assertTrue(value['blocked'])
        with self.assertRaises(cp.CloudError):refresh.apply(self.root,value['plan_id'])
        del self.cloud[self.obsname]['labels']
        self.cloud[self.obsname]['updateTime']='2026-09-02T00:00:00Z';value=refresh.plan(self.root)
        self.cloud[self.obsname]['updateTime']='2026-09-03T00:00:00Z'
        with self.assertRaisesRegex(cp.CloudError,'計画後'):refresh.apply(self.root,value['plan_id'])
        self.assertEqual(original,self.obspath.read_bytes())

    def test_refresh_detects_local_change_and_plan_tampering(self):
        from workshop import ledger_refresh as refresh
        self.own();self.cloud[self.obsname]['updateTime']='2026-09-02T00:00:00Z'
        value=refresh.plan(self.root);self.obspath.write_text(self.obspath.read_text()+' ')
        with self.assertRaises(cp.CloudError):refresh.apply(self.root,value['plan_id'])
        value=refresh.plan(self.root);path=self.root/'artifacts/ledger-refresh'/value['plan_id']/'plan.json'
        tampered=cp.read(path);tampered['changes']=[];cp.atomic_save(path,tampered)
        with self.assertRaisesRegex(cp.CloudError,'計画の内容'):refresh.apply(self.root,value['plan_id'])

    def test_agent_reuse_preserves_ledger_and_rejects_missing_record(self):
        self.own();original=self.apath.read_bytes();self.cloud[self.aname]['updateTime']='2026-09-02T00:00:00Z'
        agent=cp.AgentClient(self.cfg);agent.folder=self.apath.parent
        self.assertEqual(agent.ensure('view',self.ctx),self.aname)
        self.assertIs(agent.last_created,False)
        self.assertEqual(original,self.apath.read_bytes());self.apath.unlink()
        with self.assertRaises(cp.CloudError):agent.ensure('view',self.ctx)

    def test_cloud_snapshot_ignores_only_agent_update_metadata(self):
        from workshop.provenance import capture,same_snapshot
        for name,value in self.cloud.items():
            if '/tables/' in name:value.update(creationTime='1',lastModifiedTime='2')
        before=capture(self.root);self.assertTrue(before['complete'])
        self.cloud[self.aname]['updateTime']='2026-09-02T00:00:00Z'
        after=capture(self.root);self.assertTrue(same_snapshot(before,after))
        self.assertNotEqual(before['agent_metadata'],after['agent_metadata'])
        self.cloud[self.base+'/tables/v_revenue_events']['lastModifiedTime']='3'
        self.assertFalse(same_snapshot(before,capture(self.root)))
        self.cloud[self.base+'/tables/v_revenue_events']['lastModifiedTime']='2'
        self.cloud[self.aname]['createTime']='2026-09-02T00:00:00Z'
        self.assertFalse(same_snapshot(before,capture(self.root)))

    def test_observability_reuse_tolerates_metadata_but_delete_stays_strict(self):
        self.own();obs=cp.Inspector(self.root,check_prerequisites=False).obs;obs.ledger=cp.read(self.obspath)
        self.cloud[self.obsname]['updateTime']='2026-09-02T00:00:00Z'
        self.assertEqual(obs.ensure(self.obsname,obs.collection,{},{}),self.cloud[self.obsname])
        with self.assertRaisesRegex(cp.CloudError,'変更済み'):obs.cleanup(True)

    def test_refresh_interruption_keeps_backup_and_completion_journal(self):
        from workshop import ledger_refresh as refresh
        self.own();original=self.obspath.read_bytes()
        for name in (self.obsname,self.aname):self.cloud[name]['updateTime']='2026-09-02T00:00:00Z'
        value=refresh.plan(self.root);write=cp.write_local;calls=[]
        def interrupted(*args):
            calls.append(args[1])
            if len(calls)==2:raise OSError('controlled interruption')
            return write(*args)
        with patch.object(cp,'write_local',side_effect=interrupted):
            with self.assertRaises(OSError):refresh.apply(self.root,value['plan_id'])
        folder=self.root/'artifacts/ledger-refresh'/value['plan_id'];journal=cp.read(folder/'apply.json')
        self.assertEqual(journal['state'],'interrupted');self.assertEqual(len(journal['completed']),1)
        self.assertEqual(original,(folder/'before'/self.obspath.relative_to(self.root)).read_bytes())
        second=refresh.plan(self.root);self.assertEqual(len(second['changes']),1)
        self.assertEqual(refresh.apply(self.root,second['plan_id'])['state'],'completed')

    def test_past_agent_metadata_is_restorable_but_identity_change_blocks(self):
        self.own();old=self.apath.with_name('ec-poc-defined-'+'b'*16+'.resource.json')
        name=self.aname.rsplit('/',1)[0]+'/'+old.name.removesuffix('.resource.json')
        resource={**copy.deepcopy(self.cloud[self.aname]),'name':name}
        self.cloud[name]=copy.deepcopy(resource)
        self.put(str(old.relative_to(self.root)),{'name':name,'state':'present','description':resource['description'],'resource':resource})
        path=self.root/'backup.json';cp.backup(self.root,path)
        self.cloud[name]['updateTime']='2026-09-02T00:00:00Z'
        result=cp.restore_backup(self.root,path);self.assertTrue(result['ready'])
        self.assertIn(name,[x['resource_id'] for x in result['metadata_changes']])
        self.cloud[name]['createTime']='2026-09-03T00:00:00Z'
        with self.assertRaises(cp.CloudError):cp.restore_backup(self.root,path)

    # --- lab2.json は開始の条件にしない ---

    def test_lab2_options_do_not_gate_start_and_are_never_rewritten(self):
        self.own();path=self.root/'participant/lab2.json'
        for value in ({'evidence_cache_enabled':True},{'evidence_cache_enabled':False},None):
            with self.subTest(value=value):
                if value is None:path.unlink()
                else:self.put('participant/lab2.json',value)
                before=path.read_bytes() if path.exists() else None
                r=self.scan();self.assertTrue(r['ready']);self.assertNotIn('participant/lab2.json',r['local_writes'])
                self.assertEqual(before,path.read_bytes() if path.exists() else None)
        for text in ('null','not json','{"evidence_cache_enabled":"yes"}','{"evidence_cache_enabled":true,"other":1}'):
            with self.subTest(text=text):
                self.put('participant/lab2.json',text);r=self.scan()
                self.assertTrue(r['blocked']);self.assertEqual(next(i for i in r['items'] if i['resource_id']=='participant/lab2.json')['action'],'blocked')

    def test_lab2_option_change_is_still_detected_and_backed_up(self):
        self.own();self.put('participant/lab2.json',{'evidence_cache_enabled':False})
        path=self.root/'backup.json';cp.backup(self.root,path)
        self.assertEqual(json.loads(cp.read(path)['payload']['files']['participant/lab2.json']),{'evidence_cache_enabled':False})
        first=self.scan();self.put('participant/lab2.json',{'evidence_cache_enabled':True})
        self.assertNotEqual(first['signature'],self.scan()['signature'])

    def test_pre_event_backup_with_reuse_off_restores_and_starts_unchanged(self):
        self.own();self.put('participant/lab2.json',{'evidence_cache_enabled':False})
        path=self.root/'backup.json';cp.backup(self.root,path)
        shutil.rmtree(self.root/'artifacts');shutil.rmtree(self.root/'participant')
        cp.restore_backup(self.root,path,True)
        original=(self.root/'participant/lab2.json').read_bytes()
        self.assertEqual(json.loads(original),{'evidence_cache_enabled':False})
        code,out=self.start(assume_yes=True);self.assertEqual(code,0);self.assertIn('開始できます',out)
        self.assertEqual(original,(self.root/'participant/lab2.json').read_bytes());self.assertEqual(self.plans(),[])

    # --- start ---

    def start(self,**kw):
        out=io.StringIO()
        kw.setdefault('interactive',False)
        with contextlib.redirect_stdout(out):code=cp.start(self.root,**kw)
        return code,out.getvalue()

    def plans(self):
        folder=self.root/'artifacts/checkpoints'
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and len(p.name)==16) if folder.is_dir() else []

    def first_setup(self):
        self.cloud={};fields,body=concept(self.doc);calls=[]
        mocks=[patch.object(cp.subprocess,'run',side_effect=lambda command,**k:calls.append('bigquery')),
               patch.object(cp.Catalog,'sync',side_effect=lambda *a:calls.append('catalog') or {'title':fields['title'],'body':body}),
               patch.object(cp.AgentClient,'ensure',side_effect=lambda *a:calls.append('agent')),
               patch.object(cp.Observability,'enable',side_effect=lambda *a,**k:calls.append('observability'))]
        for m in mocks:m.start();self.addCleanup(m.stop)
        return calls

    def test_start_when_ready_changes_nothing_and_links_the_entry(self):
        self.own();before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code,out=self.start();self.assertEqual(code,0)
        self.assertIn('開始できます',out);self.assertIn('dataplex/dp-entries/'+self.entry,out);self.assertNotIn('dp-search',out)
        self.assertEqual(out.count('OK'),4);self.assertNotIn('ledger_refresh',out)
        self.assertEqual(self.plans(),[]);self.assertTrue(all(m=='GET' for m,_ in self.calls))
        after={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='diagnosis.json'}
        self.assertEqual(before,after)

    def test_start_hides_metadata_warning(self):
        self.own();self.cloud[self.obsname]['updateTime']='2026-09-02T00:00:00Z'
        code,out=self.start();self.assertEqual(code,0);self.assertNotIn('更新日時',out)
        self.assertTrue(cp.read(self.root/'artifacts/checkpoints/diagnosis.json')['metadata_changes'])

    def test_start_asks_before_creating(self):
        calls=self.first_setup()
        code,out=self.start(interactive=False);self.assertEqual(code,2);self.assertIn('何も変更していません',out)
        code,out=self.start(interactive=True,ask=lambda prompt:'n');self.assertEqual(code,1);self.assertIn('何も変更していません',out)
        code,out=self.start(interactive=True,ask=lambda prompt:(_ for _ in ()).throw(EOFError()));self.assertEqual(code,1)
        self.assertEqual(calls,[]);self.assertEqual(self.plans(),[])
        self.assertIn('これから作ります',out)
        code,out=self.start(interactive=True,ask=lambda prompt:'y')
        self.assertEqual(calls,['bigquery','catalog','agent','observability']);self.assertEqual(len(self.plans()),1)
        self.assertIn('分析データ ……… 完了',out);self.assertIn('もう一度確かめています',out)
        # 疑似クラウドは作成しても変わらないので、最後の診断は未完了のまま。開始できるとは表示しない。
        self.assertEqual(code,2);self.assertIn('まだ開始できません',out);self.assertNotIn('開始できます。',out)

    def test_start_reports_done_only_for_what_it_creates(self):
        # 事前準備の後は分析データと実行の準備が OK。作るものだけに「完了」を出す。
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            report=cp.progress({'definition','agent'})
            for step in ('participant_files','bigquery','catalog','data_agent','observability'):report(step)
        self.assertNotIn('分析データ',out.getvalue());self.assertNotIn('実行の準備',out.getvalue())
        self.assertIn('業務定義 ……… 完了',out.getvalue());self.assertIn('分析エージェント ……… 完了',out.getvalue())
        self.assertIn('もう一度確かめています',out.getvalue())

    def test_start_links_the_agent_catalog(self):
        self.own();code,out=self.start();self.assertEqual(code,0)
        self.assertIn('agents_hub;caPath=%2Fbq2%2Fagents-catalog?project=',out)

    def test_start_yes_skips_only_the_prompt(self):
        calls=self.first_setup()
        code,out=self.start(assume_yes=True,ask=lambda prompt:self.fail('must not ask'))
        self.assertEqual(calls,['bigquery','catalog','agent','observability'])
        plan=cp.load_plan(self.root,self.plans()[0])[1];diagnosis=cp.read(self.root/'artifacts/checkpoints'/plan['plan_id']/'after.json')
        self.assertEqual(plan['signature'],self.scan()['signature']);self.assertTrue(diagnosis)

    def test_start_stops_on_adopt_blocked_or_errors_even_with_yes(self):
        cases={'adopt':lambda:None,'blocked':lambda:(self.own(),self.put('participant/semantics.txt','[[edited]]')),
               'errors':lambda:self.own()}
        for name,prepare in cases.items():
            with self.subTest(name=name):
                prepare();errors=patch.object(cp.Catalog,'get',side_effect=cp.CloudError('403 unavailable')) if name=='errors' else contextlib.nullcontext()
                with errors, patch.object(cp,'apply') as applied:
                    code,out=self.start(assume_yes=True)
                self.assertEqual(code,2);applied.assert_not_called();self.assertEqual(self.plans(),[])
                self.assertIn('何も変更していません。講師に声をかけてください',out)
                self.assertIn({'adopt':'要相談','blocked':'要相談','errors':'確認できませんでした'}[name],out)

    def test_start_stops_before_apply_when_state_changes_during_prompt(self):
        for change in ('local','cloud'):
            with self.subTest(change=change):
                shutil.rmtree(self.root/'artifacts',ignore_errors=True);calls=self.first_setup()
                def answer(prompt):
                    if change=='local':self.put('participant/lab2.json',{'evidence_cache_enabled':False})
                    else:self.cloud[self.base]={'location':'US','labels':cp.LABELS}
                    return 'y'
                code,out=self.start(interactive=True,ask=answer)
                self.assertEqual(code,2);self.assertEqual(calls,[]);self.assertIn('作成を始める前に止まりました。何も変更していません',out)
                self.assertFalse((self.root/'artifacts/checkpoints'/self.plans()[0]/'apply.json').exists())
                self.put('participant/lab2.json',{'evidence_cache_enabled':True})

    def test_start_reports_partial_failure_without_claiming_no_change(self):
        calls=self.first_setup()
        with patch.object(cp.Catalog,'sync',side_effect=cp.CloudError('controlled interruption')):
            code,out=self.start(assume_yes=True)
        self.assertEqual(code,2);self.assertEqual(calls,['bigquery'])
        self.assertIn('作成の途中で止まりました',out);self.assertNotIn('何も変更していません',out)
        self.assertIn('終わったもの：参加者ファイル、分析データ',out);self.assertIn('止まったところ：業務定義（エラー：controlled interruption）',out)

    def test_start_groups_take_the_worst_state(self):
        def result(items,errors=()):return {'items':items,'errors':list(errors)}
        def item(kind,action,name='x'):return {'kind':kind,'action':action,'resource_id':name}
        rows={r[0]:r[3] for r in cp.summarize(result([item('dataset','none'),item('table','create'),item('view','blocked'),
            item('catalog_entry','update_owned_definition'),item('data_agent','none'),item('observability','use_existing_unowned')],
            [{'resource_id':'ADK','error':'missing'}]))}
        self.assertTrue(rows['data'].startswith('要相談'));self.assertTrue(rows['definition'].startswith('これから直します'))
        self.assertEqual(rows['agent'],'OK');self.assertTrue(rows['setup'].startswith('確認できませんでした'))
        rows={r[0]:r[3] for r in cp.summarize(result([item('file','complete_definition','participant/semantics.txt')],
            [{'resource_id':self.base+'/tables/orders/rows','error':'x'}]))}
        self.assertTrue(rows['definition'].startswith('これから作ります（標準の業務定義'));self.assertTrue(rows['setup'].startswith('確認できませんでした'))
        self.assertTrue(rows['agent'].startswith('確かめていません'))

    # --- 片付け後の同じ Project（削除済みの Agent と重ならない ID） ---

    def deleted_original(self):
        self.own();rec=cp.read(self.apath);rec.update(state='deleted',deleted_at='2026-10-31T00:00:00Z')
        self.put(str(self.apath.relative_to(self.root)),rec)
        self.cloud[self.aname].update(updateTime='2026-10-31T00:00:00Z',deleteTime='2026-10-31T00:00:00Z',purgeTime='2026-11-30T00:00:00Z')

    def numbered(self,suffix='-r2',ledger=True,ctx=None):
        name=self.aname+suffix;resource={k:v for k,v in copy.deepcopy(self.cloud[self.aname]).items() if k not in ('deleteTime','purgeTime')}
        resource.update(name=name,createTime='2026-11-01T00:00:00Z',updateTime='2026-11-01T00:00:00Z')
        if ctx is not None:resource['dataAnalyticsAgent']={'publishedContext':ctx}
        self.cloud[name]=resource
        if ledger:self.put(str(self.apath.with_name(self.aid+suffix+'.resource.json').relative_to(self.root)),{'name':name,'description':resource['description'],'state':'present','resource':resource})
        return name

    def agent_item(self,result):
        return next(i for i in result['items'] if i['kind']=='data_agent')

    def test_diagnosis_after_cleanup_plans_the_next_id_without_writing(self):
        self.deleted_original();before=self.apath.read_bytes()
        result=self.scan();item=self.agent_item(result)
        self.assertEqual((item['resource_id'],item['action']),(self.aname+'-r2','create'))
        self.assertIn(self.aname,result['cloud_snapshot']);self.assertEqual(result['adoptions'],{})
        self.assertEqual(before,self.apath.read_bytes());self.assertFalse(self.apath.with_name(self.aid+'-r2.resource.json').exists())

    def test_numbered_agent_is_ready_backed_up_restored_and_captured(self):
        from workshop.provenance import capture
        self.deleted_original();name=self.numbered()
        result=self.scan();self.assertTrue(result['ready']);self.assertEqual(self.agent_item(result)['resource_id'],name)
        only=self.root/'current-only.json';cp.backup(self.root,only,True);payload=cp.read(only)['payload']
        r2=str(self.apath.with_name(self.aid+'-r2.resource.json').relative_to(self.root));base=str(self.apath.relative_to(self.root))
        self.assertIn(r2,payload['files']);self.assertEqual(payload['excluded_agent_ledgers'],[base])
        for key,value in self.cloud.items():
            if '/tables/' in key:value.update(creationTime='1',lastModifiedTime='2')
        self.assertEqual(capture(self.root)['content']['agent']['name'],name)
        full=self.root/'backup.json';cp.backup(self.root,full);files=cp.read(full)['payload']['files']
        self.assertIn(base,files);self.assertIn(r2,files)
        shutil.rmtree(self.root/'artifacts');shutil.rmtree(self.root/'participant')
        self.assertTrue(cp.restore_backup(self.root,full,True)['ready'])
        for key,value in files.items():self.assertEqual((self.root/key).read_text(),value)
        self.assertTrue(self.scan()['ready'])

    def test_restore_refuses_a_deleted_ledger_whose_agent_is_usable(self):
        self.deleted_original();self.numbered();path=self.root/'backup.json';cp.backup(self.root,path)
        shutil.rmtree(self.root/'artifacts')
        for key in ('deleteTime','purgeTime'):self.cloud[self.aname].pop(key)
        with self.assertRaises(cp.CloudError):cp.restore_backup(self.root,path)
        self.assertFalse(self.apath.exists())

    def test_interrupted_creation_blocks_diagnosis_without_adoption(self):
        self.deleted_original();name=self.numbered(ledger=False)
        self.put(str(self.apath.with_name(self.aid+'-r2.resource.json').relative_to(self.root)),{'name':name,'state':'creation_requested'})
        result=self.scan();item=self.agent_item(result)
        self.assertEqual((item['resource_id'],item['action']),(name,'blocked'));self.assertIn('作成の途中',item['next'])
        self.assertEqual(result['adoptions'],{})

    def test_lost_ledger_offers_only_one_verified_candidate(self):
        self.deleted_original();name=self.numbered()
        for p in self.apath.parent.glob('*.resource.json'):p.unlink()
        result=self.scan();self.assertEqual(self.agent_item(result)['action'],'adopt');self.assertEqual(list(result['adoptions']),[name])
        self.numbered('-r3',ledger=False);result=self.scan()
        self.assertEqual(self.agent_item(result)['action'],'blocked');self.assertEqual(result['adoptions'],{})

    def test_lost_ledger_marker_alone_is_not_a_candidate(self):
        self.deleted_original();other=copy.deepcopy(self.ctx);other['systemInstruction']+='\nchanged'
        self.numbered(ledger=False,ctx=other);self.apath.unlink()
        result=self.scan();self.assertEqual(self.agent_item(result)['action'],'blocked');self.assertEqual(result['adoptions'],{})

if __name__=='__main__':unittest.main()

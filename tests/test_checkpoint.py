"""Guarded checkpoint tests: all cloud reads use an in-memory service."""
import copy
import hashlib
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
        with patch.object(cp,'diagnose',side_effect=[before,after]), patch.object(cp.subprocess,'run',side_effect=lambda command,**k:(self.assertEqual(command[-1],'lab2'),order.append('bigquery'))), patch.object(cp.Catalog,'sync',side_effect=lambda *a:order.append('catalog') or {'title':fields['title'],'body':body}), patch.object(cp.AgentClient,'ensure',side_effect=lambda *a:order.append('agent')), patch.object(cp.Observability,'enable',side_effect=lambda:order.append('observability')):
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
        self.own();self.put('participant/lab2.json',{'evidence_cache_enabled':False});p=cp.write_plan(self.root)
        with patch.object(cp.Catalog,'sync',side_effect=cp.CloudError('controlled interruption')):
            with self.assertRaises(cp.CloudError):cp.apply(self.root,p['plan_id'])
        folder=self.root/'artifacts/checkpoints'/p['plan_id']
        self.assertEqual(cp.read(folder/'apply.json')['state'],'interrupted')
        self.assertEqual(cp.read(folder/'before/participant/lab2.json'),{'evidence_cache_enabled':False})
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

if __name__=='__main__':unittest.main()

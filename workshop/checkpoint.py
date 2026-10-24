"""Read-only Summit diagnosis and explicit, guarded checkpoint application."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .resource_state import compare as compare_resource, equivalent, without_api_defaults
from .settings import ROOT,config
from .cloud import Client,CloudError,LABELS,dataset_path,table_rows,preflight,timestamp,validate_cloud_config
from .bigquery import expected_view_query,expected_table_fields
from .data import fixture
from .catalog import Catalog,concept,as_document
from .semantics_poc import API
from .data_agent import AgentClient,fingerprint,validate_published
from .lab1 import make_contexts
from .observability import BASE,Observability,binding_matches,settings_enabled
from .economics import atomic_save

VERSION='summit-checkpoint-v1'


def sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def standard_rules(root):
    return (root/'semantics/lab1-starter.txt').read_text().replace('[[返金の計上日]]','refunded_date（返金日）').replace('[[返品で戻す原価]]','recovered_cost_jpy（実際に回収できた原価）')


def complete(value):
    return bool(value.strip()) and '[[' not in value and ']]' not in value


def safe_path(root,key):
    path=root/key
    if not path.is_relative_to(root) or '..' in Path(key).parts:
        raise CloudError('許可範囲外のパス: '+key)
    if any(p.is_symlink() for p in (path,*path.parents) if p.is_relative_to(root)):
        raise CloudError('シンボリックリンクを使用しません: '+key)
    return path


def source_snapshot(root):
    paths=[*root.glob('workshop/*.py'),*root.glob('semantics/**/*'),*root.glob('data/*'),*root.glob('agent/*'),root/'requirements-adk.txt',root/'uv.lock']
    return {str(p.relative_to(root)):local_hash(p) for p in sorted(paths) if p.is_file()}


def local_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def log_access(client):
    """監査ログの閲覧権限。通常ログと Data Access ログを分けて確認する。

    Root の実行可否とは別の判定。ここが不足しても実習は開始できる。
    Data Access ログは `_Default` バケットでは Private Logs Viewer が必要で、
    通常ログの閲覧権限とは別に付与される。
    """
    standard, private = 'logging.logEntries.list', 'logging.privateLogEntries.list'
    value = client.request('POST','https://cloudresourcemanager.googleapis.com/v1/projects/'+client.project+':testIamPermissions',
                           {'permissions':[standard, private]})
    granted = set(value.get('permissions', []))
    return {'standard_logs':'granted' if standard in granted else 'missing',
            'data_access_logs':'granted' if private in granted else 'missing',
            'missing_permissions':[p for p in (standard, private) if p not in granted],
            'audit_config_verified':False, 'actual_read_verified':False}


def cost_access(client):
    permission='bigquery.jobs.listAll'
    value=client.request('POST','https://cloudresourcemanager.googleapis.com/v1/projects/'+client.project+':testIamPermissions',{'permissions':[permission]})
    granted=permission in value.get('permissions',[])
    return {'job_metadata_access':'granted' if granted else 'missing','missing_permissions':[] if granted else [permission],'actual_collection_verified':False}


class Inspector:
    def __init__(self,root=ROOT,cfg=None,account=None,overrides=None,check_prerequisites=True):
        self.root=Path(root);self.cfg=cfg or config(require_project=True,root=root);validate_cloud_config(self.cfg)
        self.account=account;self.c=Client(self.cfg,account)
        self.catalog=Catalog(self.cfg,account,root);self.catalog.c=self.c
        self.agent=AgentClient(self.cfg,account);self.agent.folder=self.root/'artifacts/data-agent'/self.c.project/self.cfg['dataset_id']/self.cfg['data_agent_location']
        self.obs=Observability(self.cfg,account);self.obs.c=self.c;self.obs.folder=self.root/'artifacts/observability'/self.c.project;self.obs.path=self.obs.folder/'resources.json'
        self.overrides=overrides or {};self.check_prerequisites=check_prerequisites
        self.items=[];self.snapshots={};self.ledger_paths=set();self.adoptions={};self.local_writes={};self.errors=[];self.metadata_changes=[]

    def local(self,path):
        key=str(path.relative_to(self.root));safe_path(self.root,key);self.ledger_paths.add(key)
        return self.overrides[key] if key in self.overrides else path.read_text() if path.is_file() else None

    def ledger(self,path):
        value=self.local(path)
        return json.loads(value) if value is not None else {}

    def resource_matches(self, expected, actual):
        comparison=compare_resource(expected,actual)
        if comparison['metadata_changes']:
            self.metadata_changes.append({'resource_id':actual['name'],'changes':comparison['metadata_changes']})
        return comparison['equivalent']

    def item(self,name,kind,actual,match,ledger,action='none',note=''):
        self.items.append({'resource_id':name,'kind':kind,'exists':actual is not None,'matches':match,'ledger':ledger,'action':action,'next':note})
        self.snapshots[name]=actual

    def get(self,name,callback):
        try:return callback()
        except (CloudError,ValueError,OSError) as exc:
            self.errors.append({'resource_id':name,'error':str(exc),'next':'認証・API・権限・配置を確認してdiagnoseを再実行'})
            return None

    def adopt(self,resource,path,entry=None,value=None):
        key=str(path.relative_to(self.root))
        self.adoptions[resource]={'path':key,'entry':entry,'value':value}

    def scan(self):
        cfg=self.cfg;cat=self.catalog
        ident='ec-lab1-'+hashlib.sha256(cfg['dataset_id'].encode()).hexdigest()[:12]
        names=[cat.parent+'/entryGroups/'+ident,cat.parent+'/entryTypes/'+ident]
        metadata={'displayName':'EC lab1 definitions','description':'workshop_cli:summit_ec_v1:catalog','labels':LABELS}
        actuals={name:self.get(name,lambda n=name:cat.get(n)) for name in names}
        trusted=True;ledger=self.ledger(cat.ledger_path)
        for name in names:
            actual=actuals[name]
            content={k:v for k,v in without_api_defaults(actual or {}).items() if k not in ('name','uid','createTime','updateTime','etag') and not (k=='authorization' and v=={})}
            match=content==metadata if actual is not None else None
            recorded=ledger.get(name,{})
            owned=recorded.get('state')=='present' and self.resource_matches(recorded.get('snapshot'),actual) and recorded.get('payload')==metadata
            state='matched' if owned else 'changed' if recorded else 'missing'
            action='create' if actual is None and not recorded else 'none' if owned and match else 'adopt' if match and not recorded else 'blocked'
            self.item(name,'catalog_dependency',actual,match,state,action,'台帳バックアップを復元。なければ検証済み候補のIDを確認' if action=='adopt' else '')
            if action=='adopt':self.adopt(name,cat.ledger_path,name,{'state':'present','snapshot':actual,'payload':metadata})
            trusted=trusted and bool(match)
        name=names[0]+'/entries/business-rules';entry=self.get(name,lambda:cat.get(name));document=None
        if entry is not None and trusted:
            try:
                _,aspect=cat.overview(entry);candidate=aspect.get('data',{}).get('content','')
                if complete(candidate):concept(candidate);document=candidate
            except (CloudError,ValueError) as exc:self.errors.append({'resource_id':name,'error':str(exc)})
        path=self.root/'participant/semantics.txt';local=self.local(path);starter=(self.root/'semantics/lab1-starter.txt').read_text()
        if local is None or local==starter:
            rules=document or standard_rules(self.root);self.local_writes['participant/semantics.txt']=rules
            self.item('participant/semantics.txt','file',local,False,'not_applicable','restore_definition' if document else 'complete_definition','Catalogの保存定義を復元' if document else '標準完成定義を作成。元ファイルは退避')
        elif not complete(local):
            rules=local;self.item('participant/semantics.txt','file',local,False,'not_applicable','blocked','編集途中の定義を保持。#1を完成させるかバックアップを復元')
        else:
            rules=local;self.item('participant/semantics.txt','file',local,True,'not_applicable')
        self.rules=rules
        if not complete(rules):return self.finish()
        try:fields,body=concept(as_document(rules))
        except ValueError as exc:self.errors.append({'resource_id':'participant/semantics.txt','error':str(exc)});return self.finish()
        desired=as_document(rules)
        payload={'entryType':(actuals[names[1]] or {'name':names[1]})['name'],'entrySource':{'displayName':fields['title'],'description':fields.get('description','')},'aspects':{'dataplex-types.global.overview':{'data':{'content':desired,'contentType':'MARKDOWN'}}}}
        entry_match=False
        if entry is not None:
            try:
                key,aspect=cat.overview(entry)
                source=dict(entry.get('entrySource',{}));source.pop('location',None)
                type_match=entry.get('entryType')==payload['entryType']
                if not type_match:
                    entry_type=entry.get('entryType','')
                    actual_type=self.get(entry_type,lambda:cat.get(entry_type)) if entry_type else None
                    expected_type=actuals[names[1]] or {}
                    type_match=bool(actual_type and actual_type.get('uid') and actual_type.get('uid')==expected_type.get('uid'))
                entry_match=(type_match and source==payload['entrySource'] and set(entry.get('aspects',{}))=={key} and aspect.get('data')==payload['aspects']['dataplex-types.global.overview']['data'])
            except CloudError as exc:self.errors.append({'resource_id':name,'error':str(exc)})
        rec=ledger.get(name,{});owned=rec.get('state')=='present' and self.resource_matches(rec.get('snapshot'),entry)
        state='matched' if owned else 'changed' if rec else 'missing'
        action='create' if entry is None and not rec else 'none' if owned and entry_match else 'update_owned_definition' if owned else 'adopt' if entry_match and trusted and not rec else 'blocked'
        self.item(name,'catalog_entry',entry,entry_match if entry else None,state,action,'既存定義が異なる場合はバックアップを確認。自動上書きしない')
        if action=='adopt':self.adopt(name,cat.ledger_path,name,{'state':'present','snapshot':entry,'payload':payload})
        self.context=make_contexts(cfg,rules,{'title':fields['title'],'body':body})['view']
        aid='ec-poc-view-'+fingerprint(self.context);aname=self.agent.parent+'/dataAgents/'+aid
        agent=self.get(aname,lambda:self.agent.request('GET',self.agent.base+aname,missing_ok=True))
        marker='workshop_cli:summit_ec_v1:'+fingerprint(self.context);match=False
        if agent is not None:
            try:validate_published(agent,marker,self.context,aname);match=True
            except CloudError:pass
        apath=self.agent.folder/(aid+'.resource.json');rec=self.ledger(apath)
        owned=rec.get('name')==aname and rec.get('state')=='present' and rec.get('description')==marker and match and self.resource_matches(rec.get('resource'),agent)
        action='create' if agent is None and not rec else 'none' if owned else 'adopt' if match and not rec else 'blocked'
        self.item(aname,'data_agent',agent,match if agent else None,'matched' if owned else 'changed' if rec else 'missing',action)
        if action=='adopt':self.adopt(aname,apath,value={'name':aname,'description':marker,'state':'present','resource':agent,'checked_at':timestamp()})
        self.scan_bigquery()
        self.scan_observability()
        path=self.root/'participant/lab2.json';value=self.local(path)
        try:options=json.loads(value) if value is not None else None
        except ValueError:options='invalid'
        if options is not None and (not isinstance(options,dict) or set(options)!={'evidence_cache_enabled'} or type(options['evidence_cache_enabled']) is not bool):
            self.item('participant/lab2.json','file',value,False,'not_applicable','blocked','未知の設定を保持。内容を確認')
        else:
            enabled=options=={'evidence_cache_enabled':True}
            self.item('participant/lab2.json','file',value,enabled,'not_applicable','none' if enabled else 'enable_reuse','適用時だけ再利用を有効化。元ファイルは退避')
            if not enabled:self.local_writes['participant/lab2.json']=json.dumps({'evidence_cache_enabled':True},indent=2)+'\n'
        return self.finish()

    def scan_bigquery(self):
        c=self.c;base=dataset_path(c);dataset=self.get(base,lambda:c.request('GET',base,missing_ok=True));manifest=self.ledger(self.root/'artifacts/cloud-resources.json')
        owned_manifest=not (set(manifest.get('tables',[]))-set(fixture()) or set(manifest.get('views',[]))-{'v_revenue_events'}) and manifest.get('project_id')==c.project and manifest.get('dataset_id')==c.cfg['dataset_id'] and manifest.get('location','').lower()==c.location.lower()
        good=bool(dataset and dataset.get('location','').lower()==c.location.lower() and all(dataset.get('labels',{}).get(k)==v for k,v in LABELS.items()))
        action='create' if dataset is None and not manifest else 'none' if good and owned_manifest else 'adopt' if good and not manifest else 'blocked'
        self.item(base,'dataset',dataset,good if dataset else None,'matched' if owned_manifest else 'changed' if manifest else 'missing',action)
        matches=True;present=[]
        if dataset:
            listing=self.get(base+'/tables',lambda:c.request('GET',base+'/tables?maxResults=1000')) or {}
            unexpected={x['tableReference']['tableId'] for x in listing.get('tables',[])}-set(fixture())-{'v_revenue_events'}
            if unexpected or listing.get('nextPageToken'):self.errors.append({'resource_id':base,'error':'教材外のテーブルまたは不完全な一覧。変更せず個別確認'})
        for table,rows in fixture().items():
            name=base+'/tables/'+table;actual=self.get(name,lambda n=name:c.request('GET',n,missing_ok=True)) if dataset else None
            got=self.get(name+'/rows',lambda t=table:table_rows(c,t)) if actual else None
            schema=bool(actual and actual.get('type')=='TABLE' and actual.get('schema',{}).get('fields')==expected_table_fields(table) and not any(actual.get(k) for k in ('labels','description','friendlyName','expirationTime','timePartitioning','rangePartitioning','clustering','encryptionConfiguration')))
            match=schema and got==sorted(rows)
            seedable=schema and got==[] and owned_manifest
            recorded=owned_manifest and table in manifest.get('tables',[])
            action='create' if actual is None else 'seed_empty_owned_table' if seedable else 'none' if match and recorded else 'adopt_with_dataset' if match and good and not manifest else 'blocked'
            self.item(name,'table',{'metadata':{k:actual.get(k) for k in ('id','type','schema','lastModifiedTime','view','labels','description')} ,'rows':got} if actual else None,match,'matched' if recorded else 'missing',action,'合成データ全行・スキーマを照合。異なるデータは保持')
            matches=matches and match
            if actual:present.append(table)
        name=base+'/tables/v_revenue_events';view=self.get(name,lambda:c.request('GET',name,missing_ok=True)) if dataset else None
        match=bool(view and view.get('type')=='VIEW' and view.get('view',{}).get('query','').strip().rstrip(';').strip()==expected_view_query(c.cfg))
        recorded=owned_manifest and 'v_revenue_events' in manifest.get('views',[])
        action='create' if view is None else 'none' if match and recorded else 'adopt_with_dataset' if match and good and not manifest else 'blocked'
        self.item(name,'view', {k:view.get(k) for k in ('id','type','view','schema','lastModifiedTime')} if view else None,match if view else None,'matched' if recorded else 'missing',action)
        if good and not manifest and matches and match:
            self.adopt(base,self.root/'artifacts/cloud-resources.json',value={'project_id':c.project,'dataset_id':c.cfg['dataset_id'],'location':c.location,'managed_labels':LABELS,'tables':present,'views':['v_revenue_events']})
        elif good and not manifest:
            self.errors.append({'resource_id':base,'error':'台帳再構築にはDataset・全5テーブル・Viewの一致が必要'})

    def scan_observability(self):
        obs=self.obs;ledger=self.ledger(obs.path)
        listing=self.get(obs.collection,lambda:self.c.request('GET',BASE+obs.collection)) or {}
        if listing.get('nextPageToken') or listing.get('unreachable') or any(x.get('name')!=obs.name for x in listing.get('gibqObservabilitySettings',[])):
            self.errors.append({'resource_id':obs.collection,'error':'教材外の観測設定、または一覧が不完全。管理者の確認が必要'})
        setting=self.get(obs.name,lambda:obs.get(obs.name));binding_name=obs.name+'/settingBindings/workshop-project'
        binding=self.get(binding_name,lambda:obs.get(binding_name)) if setting else None
        project=self.get('project_metadata',lambda:self.c.request('GET','https://cloudresourcemanager.googleapis.com/v1/projects/'+self.c.project)) or {}
        expected_target={'projects/'+self.c.project,'projects/'+str(project.get('projectNumber',''))}
        matches=[settings_enabled(setting),binding_matches(binding,expected_target)]
        for name,actual,match in zip((obs.name,binding_name),(setting,binding),matches):
            rec=ledger.get(name,{});owned=rec.get('state')=='present' and self.resource_matches(rec.get('snapshot'),actual)
            action='create' if actual is None and not rec else 'none' if match and owned else 'use_existing_unowned' if match and not rec else 'blocked'
            self.item(name,'observability',actual,match if actual else None,'matched' if owned else 'changed' if rec else 'missing',action,'既存の有効設定は利用だけ。台帳なしでは教材の所有物として採用・削除しない' if action=='use_existing_unowned' else '')
        if setting and not binding and not ledger:self.errors.append({'resource_id':obs.name,'error':'台帳なしの既存観測設定にbindingを追加しない。バックアップを復元'})

    def finish(self):
        pre=None;dependencies=True;warnings=(['内容と作成時の識別情報は一致、更新日時等だけに差があります。開始は可能です。台帳更新は uv run --locked python3 -m workshop.ledger_refresh plan で差分を確認してください。'] if self.metadata_changes else []);cost={'job_metadata_access':'unknown','missing_permissions':[],'actual_collection_verified':False};logs={'standard_logs':'unknown','data_access_logs':'unknown','missing_permissions':[],'audit_config_verified':False,'actual_read_verified':False}
        if self.check_prerequisites:
            try:pre=preflight(self.c,'lab2')
            except (CloudError,ValueError,OSError) as exc:self.errors.append({'resource_id':'prerequisites','error':str(exc)})
            try:dependencies=importlib.metadata.version('google-adk')=='2.8.0'
            except importlib.metadata.PackageNotFoundError:dependencies=False
            if not dependencies:self.errors.append({'resource_id':'ADK','error':'事前課題の仮想環境を有効化し、requirements-adk.txtを導入してください'})
            try:cost=cost_access(self.c)
            except (CloudError,ValueError,OSError) as exc:cost['error']=str(exc)
            if cost['job_metadata_access']!='granted':warnings.append('実行開始と費用照合は別の判定です。BigQueryジョブ情報の閲覧権限が未準備・未確認のため、費用照合でBigQuery分と全体費用が未算出になる可能性があります。管理者にbigquery.jobs.listAllを相談してください。')
            try:logs=log_access(self.c)
            except (CloudError,ValueError,OSError) as exc:logs['error']=str(exc)
            if logs['data_access_logs']!='granted':warnings.append('実行開始とログ閲覧は別の判定です。監査ログの閲覧権限が未準備・未確認でも実習は開始できます。Data Accessログの閲覧にはPrivate Logs Viewerが必要です。')
            if pre and not pre['can_attempt_stage']:self.errors.append({'resource_id':'prerequisites','error':'必要API・権限・課金状態を事前診断で確認'})
        locals_={key:hashlib.sha256(self.overrides[key].encode()).hexdigest() if key in self.overrides else local_hash(self.root/key) for key in sorted(self.ledger_paths|{'config/workshop.json','config/workshop.local.json'})}
        # Applying a reviewed plan still requires the exact fresh snapshot, including metadata.
        fingerprint_items=[{k:v for k,v in i.items() if k!='next'} for i in self.items]
        signature=sha({'config':self.cfg,'items':fingerprint_items,'snapshots':self.snapshots,'local':locals_,'rules':getattr(self,'rules',None)})
        cloud=[i for i in self.items if i['kind']!='file'];missing_ledger=any(i['exists'] and i['ledger']=='missing' and i['action']!='use_existing_unowned' for i in cloud)
        blocked=bool(self.errors or any(i['action']=='blocked' for i in self.items))
        state='diagnosis_incomplete' if self.errors else 'recovery_needed' if missing_ledger or blocked else 'first_setup' if not any(i['exists'] for i in cloud) else 'continue_existing'
        return {'version':VERSION,'project_id':self.c.project,'dataset_id':self.cfg['dataset_id'],'checked_at':timestamp(),'classification':state,'items':self.items,'errors':self.errors,'prerequisites':pre,'cost_collection':cost,'log_access':logs,'warnings':warnings,'metadata_changes':self.metadata_changes,'dependencies_verified':dependencies,'signature':signature,'cloud_snapshot':self.snapshots,'local_snapshot':locals_,'local_writes':self.local_writes,'adoptions':self.adoptions,'blocked':blocked,'ready':not blocked and all(i['action'] in ('none','use_existing_unowned') for i in self.items),'inference_verified':False}


def diagnose(root=ROOT,account=None,overrides=None,check_prerequisites=True):
    return Inspector(root,account=account,overrides=overrides,check_prerequisites=check_prerequisites).scan()


def write_plan(root,account=None):
    value=diagnose(root,account);ident=uuid.uuid4().hex[:16]
    value.update(plan_id=ident,config=config(require_project=True,root=root),source_snapshot=source_snapshot(root))
    folder=root/'artifacts/checkpoints'/ident;folder.mkdir(parents=True,exist_ok=False)
    atomic_save(folder/'plan.json',value);(folder/'plan.sha256').write_text(sha(value))
    return value


def load_plan(root,ident):
    if not re.fullmatch('[a-f0-9]{16}',ident):raise ValueError('Invalid checkpoint plan ID')
    folder=root/'artifacts/checkpoints'/ident;value=read(folder/'plan.json')
    if sha(value)!=(folder/'plan.sha256').read_text():raise ValueError('Checkpoint計画が変更されています')
    return folder,value


def path_allowed(root,cfg,key):
    fixed={'config/workshop.local.json','participant/semantics.txt','participant/lab2.json','artifacts/cloud-resources.json'}
    prefixes=['artifacts/catalog/'+cfg['project_id']+'/'+cfg['dataset_id']+'/'+cfg['catalog_location']+'/resources.json','artifacts/observability/'+cfg['project_id']+'/resources.json']
    agent='artifacts/data-agent/'+cfg['project_id']+'/'+cfg['dataset_id']+'/'+cfg['data_agent_location']+'/'
    return key in fixed or key in prefixes or (key.startswith(agent) and re.fullmatch(r'ec-poc-(raw|defined|view)-[a-f0-9]{16}\.(resource|config)\.json',key[len(agent):]) is not None)


def write_local(root,key,text,backup_dir):
    path=safe_path(root,key)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        target=backup_dir/key;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(path.read_bytes())
    temporary=path.with_name(path.name+'.checkpoint.tmp')
    try:
        with temporary.open('x') as stream:stream.write(text)
    except FileExistsError as exc:
        raise CloudError('中断時の一時ファイルが残っています: '+str(temporary)+ '。元ファイルと内容を比較し、別名へ退避してから新しい計画を作成してください。自動削除はしません。') from exc
    temporary.replace(path)


def apply(root,ident,accept_resources=(),account=None):
    folder,plan=load_plan(root,ident)
    if plan['blocked']:raise CloudError('診断で未確認・不一致があります。解消して新しい計画を作成してください')
    if set(accept_resources)!=set(plan['adoptions']):raise CloudError('台帳再構築候補の全IDを確認し、--accept-resourceをIDごとに指定してください。バックアップ復元を優先します。')
    cfg=config(require_project=True,root=root)
    if cfg!=plan['config']:raise CloudError('対象Projectまたは設定が変わりました')
    if source_snapshot(root)!=plan['source_snapshot']:raise CloudError('教材の版が変わりました。新しいCheckpoint計画を作成してください')
    with ExitStack() as stack:
        # Refuse concurrent Root and checkpoint commands. Component commands retain their own guards.
        for key in ('artifacts/.root-execution.lock','artifacts/checkpoints/.lock'):
            p=safe_path(root,key);p.parent.mkdir(parents=True,exist_ok=True);f=stack.enter_context(p.open('a'));fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=diagnose(root,account)
        if current['blocked'] or current['signature']!=plan['signature']:raise CloudError('計画後にクラウドまたはローカル状態が変わりました。再診断してください')
        journal={'plan_id':ident,'state':'applying','completed':[]};atomic_save(folder/'apply.json',journal)
        try:
            updates={}
            for resource,change in plan['adoptions'].items():
                key=change['path']
                if not path_allowed(root,cfg,key):raise CloudError('許可範囲外の台帳パス')
                if change['entry']:
                    updates.setdefault(key,read(root/key))[change['entry']]=change['value']
                else:updates[key]=change['value']
            for key,value in updates.items():write_local(root,key,json.dumps(value,ensure_ascii=False,indent=2)+'\n',folder/'before')
            journal['completed'].append('verified_ledger_adoption');atomic_save(folder/'apply.json',journal)
            for key,value in plan['local_writes'].items():
                if key not in ('participant/semantics.txt','participant/lab2.json'):raise CloudError('許可範囲外の参加者ファイル')
                write_local(root,key,value,folder/'before')
            journal['completed'].append('participant_files');atomic_save(folder/'apply.json',journal)
            bq_actions={i['action'] for i in plan['items'] if i['kind'] in ('dataset','table','view')}
            if bq_actions-{'none','adopt','adopt_with_dataset'}:
                command=[sys.executable,'-m','workshop.cloud','bootstrap-bigquery','--stage','lab2']
                if account:command+=['--account',account]
                subprocess.run(command,cwd=root,check=True)
            journal['completed'].append('bigquery');atomic_save(folder/'apply.json',journal)
            cat=Catalog(cfg,account,root);receipt=cat.sync(as_document((root/'participant/semantics.txt').read_text()))
            journal['completed'].append('catalog');atomic_save(folder/'apply.json',journal)
            agent=AgentClient(cfg,account);agent.folder=root/'artifacts/data-agent'/cfg['project_id']/cfg['dataset_id']/cfg['data_agent_location']
            agent.ensure('view',make_contexts(cfg,(root/'participant/semantics.txt').read_text(),receipt)['view'])
            journal['completed'].append('data_agent');atomic_save(folder/'apply.json',journal)
            if any(i['kind']=='observability' and i['action']=='create' for i in plan['items']):
                obs=Observability(cfg,account);obs.folder=root/'artifacts/observability'/cfg['project_id'];obs.path=obs.folder/'resources.json';obs.ledger=read(obs.path)
                (root/'artifacts/observability').mkdir(parents=True,exist_ok=True)
                with (root/'artifacts/observability/.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);obs.enable()
            journal['completed'].append('observability');atomic_save(folder/'apply.json',journal)
            final=diagnose(root,account);atomic_save(folder/'after.json',final)
            journal['state']='ready' if final['ready'] else 'needs_attention'
        except Exception as exc:
            journal.update(state='interrupted',error=str(exc));raise
        finally:atomic_save(folder/'apply.json',journal)
    return final


def backup(root,output,current_agent_only=False):
    cfg=config(require_project=True,root=root);files={};excluded=[];current_id=None
    if current_agent_only:
        rules=(root/'participant/semantics.txt').read_text()
        if not complete(rules):raise CloudError('現行Agentを特定するには完成した定義ファイルが必要です')
        fields,body=concept(as_document(rules))
        current_id='ec-poc-view-'+fingerprint(make_contexts(cfg,rules,{'title':fields['title'],'body':body})['view'])
    candidates=[root/k for k in ('config/workshop.local.json','participant/semantics.txt','participant/lab2.json','artifacts/cloud-resources.json')]
    candidates+=list((root/'artifacts/catalog'/cfg['project_id']/cfg['dataset_id']).glob('*/resources.json'))
    candidates+=list((root/'artifacts/data-agent'/cfg['project_id']/cfg['dataset_id']).glob('*/*.resource.json'))
    candidates+=[root/'artifacts/observability'/cfg['project_id']/'resources.json']
    for p in candidates:
        key=str(p.relative_to(root))
        if p.is_file() and path_allowed(root,cfg,key):
            if current_id and p.name.endswith('.resource.json') and p.name!=current_id+'.resource.json':
                excluded.append(key);continue
            safe_path(root,key)
            files[key]=p.read_text()
    payload={'version':VERSION,'config':cfg,'files':files,'excluded_agent_ledgers':excluded,'scope':'Checkpoint only; Graph, other PoCs and answer records require the lab1 artifacts backup'}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as f:json.dump({'payload':payload,'sha256':sha(payload)},f,ensure_ascii=False,indent=2)
    return len(files)


def restore_backup(root,path,execute=False,account=None):
    envelope=read(path);payload=envelope.get('payload',{});cfg=config(require_project=True,root=root)
    if sha(payload)!=envelope.get('sha256') or payload.get('version')!=VERSION or payload.get('config')!=cfg:raise CloudError('バックアップの内容・版・対象設定が一致しません')
    files=payload.get('files',{})
    if not isinstance(files,dict):raise CloudError('バックアップのファイル一覧が不正です')
    for key,value in files.items():
        if not path_allowed(root,cfg,key) or not isinstance(value,str):raise CloudError('バックアップに許可範囲外の内容')
        safe_path(root,key)
        if (root/key).exists() and (root/key).read_text()!=value:raise CloudError('現在の編集または台帳を保護して停止: '+key)
    result=diagnose(root,account,overrides=files)
    if result['blocked']:raise CloudError('バックアップと実リソースの一致を確認できません。復元しません')
    # Every restored resource ledger must be checked, not just the current view agent.
    checked={str(Inspector(root,account=account).catalog.ledger_path.relative_to(root)),str(Inspector(root,account=account).obs.path.relative_to(root)),'artifacts/cloud-resources.json'}
    restore_snapshots={}
    for key,text in files.items():
        if key.endswith('.resource.json'):
            rec=json.loads(text);agent=AgentClient(cfg,account);name=rec.get('name','')
            if name!=agent.parent+'/dataAgents/'+Path(key).name.removesuffix('.resource.json'):raise CloudError('バックアップAgentのIDが対象範囲と一致しません: '+name)
            actual=agent.request('GET',agent.base+name,missing_ok=True)
            restore_snapshots[name]=actual
            expected=rec.get('resource',{})
            from .data_agent import published_context
            delta=compare_resource(expected,actual)['metadata_changes']
            if delta:result['metadata_changes'].append({'resource_id':name,'changes':delta})
            if actual is None or rec.get('state')!='present' or not equivalent(expected,actual) or published_context(actual)!=published_context(expected) or actual.get('description')!=rec.get('description'):raise CloudError('バックアップAgentが実リソースと一致しません（状態='+str(rec.get('state'))+'）: '+name)
        elif key in checked and key!='artifacts/cloud-resources.json':
            records=json.loads(text)
            for name,rec in records.items():
                actual=result['cloud_snapshot'].get(name)
                if actual is None or rec.get('state')!='present' or not equivalent(rec.get('snapshot'),actual):raise CloudError('バックアップ台帳に未確認・変更済みリソースがあります（状態='+str(rec.get('state'))+'）: '+name+'。原本を保持して個別確認してください。')
    result['signature']=sha({'diagnosis':result['signature'],'restored_agents':restore_snapshots})
    if result.get('metadata_changes') and not result.get('warnings'):
        result['warnings']=['バックアップ原本をそのまま復元します。メタデータ差分はmetadata_changesに記録。台帳最新化はworkshop.ledger_refreshで別途実行してください。']
    if execute:
        with ExitStack() as stack:
            for key in ('artifacts/.root-execution.lock','artifacts/checkpoints/.lock'):
                p=safe_path(root,key);p.parent.mkdir(parents=True,exist_ok=True)
                f=stack.enter_context(p.open('a'));fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            again=restore_backup(root,path,False,account)
            if again['signature']!=result['signature']:raise CloudError('復元検査中にクラウドが変更されました')
            target=root/'artifacts/checkpoints'/('restore-'+uuid.uuid4().hex)
            record={'source_sha256':envelope['sha256'],'files':[],'state':'restoring','checked_at':timestamp(),'metadata_changes':result.get('metadata_changes',[])}
            try:
                for key,value in files.items():
                    dest=safe_path(root,key)
                    if dest.exists():
                        if dest.read_text()!=value:raise CloudError('復元中にローカルファイルが変更されました: '+key)
                    else:write_local(root,key,value,target/'before')
                    record['files'].append(key)
                record['state']='restored'
            except Exception as exc:record.update(state='interrupted',error=str(exc));raise
            finally:atomic_save(target/'restored.json',record)
    return result


def require_ready(root=ROOT,account=None):
    result=diagnose(root,account)
    atomic_save(root/'artifacts/checkpoints/diagnosis.json',result)
    for warning in result.get('warnings',[]):print('警告: '+warning,flush=True)
    if not result['ready']:
        details=[i['resource_id']+': '+i['action'] for i in result['items'] if i['action'] not in ('none','use_existing_unowned')]
        details += [e['resource_id']+': '+e['error'] for e in result['errors']]
        raise CloudError('評価の推論前診断で停止しました。試行は開始していません。uv run --locked python3 -m workshop.checkpoint plan で不足・復旧手順を確認してください。\n'+'\n'.join(details))
    return result


def show(result):
    print('対象Project: '+result['project_id']+' / '+result['dataset_id'])
    print('状態: '+result['classification']+' / 開始可能: '+str(result['ready']))
    if result.get('plan_id'):print('Checkpoint計画: '+result['plan_id'])
    for i in result['items']:print(i['kind']+' | '+i['resource_id']+' | 存在='+str(i['exists'])+' 一致='+str(i['matches'])+' 台帳='+i['ledger']+' | '+i['action']+' '+i['next'])
    for e in result['errors']:print('未確認: '+str(e))
    print('費用照合用ジョブ閲覧権限: '+result.get('cost_collection',{}).get('job_metadata_access','unknown')+'（実際の取得成功は未確認）')
    log = result.get('log_access', {})
    print('監査ログ閲覧権限: 通常ログ='+log.get('standard_logs','unknown')+' / Data Accessログ='+log.get('data_access_logs','unknown')
          +'（監査設定の有効化と実際の読み取りは未確認。いずれもRoot実行の可否とは別）')
    for warning in result.get('warnings',[]):print('警告: '+warning)
    print('推論は未実行。観測の反映・モデル利用・Quotaは実習時に確認します。')
    if result['ready']:
        project=result['project_id']
        print('次はGoogle Cloud Consoleで完成状態を確認します:')
        print('BigQuery: https://console.cloud.google.com/bigquery?project='+project)
        print('Knowledge Catalog: https://console.cloud.google.com/dataplex/dp-search?project='+project)
        print('BigQuery Agents: https://console.cloud.google.com/bigquery/agents_hub?project='+project)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['diagnose','plan','apply','backup','restore-backup']);p.add_argument('--account');p.add_argument('--plan');p.add_argument('--accept-resource',action='append',default=[]);p.add_argument('--file',type=Path);p.add_argument('--execute',action='store_true');p.add_argument('--current-agent-only',action='store_true',help='backupのみ。現行のView Agentに絞り、除外した過去台帳の一覧を保存');a=p.parse_args()
    try:
        cfg=config(require_project=True)
        print('対象Project: '+cfg['project_id']+' / '+cfg['dataset_id'],flush=True)
        if a.command=='backup':
            if not a.file:p.error('--file is required')
            count=backup(ROOT,a.file,a.current_agent_only)
            print('バックアップを保存しました：'+str(a.file)+'（'+str(count)+' ファイル）')
            if a.current_agent_only:print('過去Agentの台帳は対象外です。元フォルダと全件バックアップを保持してください。')
            return 0
        if a.command=='restore-backup':
            if not a.file:p.error('--file is required')
            result=restore_backup(ROOT,a.file,a.execute,a.account)
        elif a.command=='apply':
            if not a.plan:p.error('--plan is required')
            result=apply(ROOT,a.plan,a.accept_resource,a.account)
        elif a.command=='plan':result=write_plan(ROOT,a.account)
        else:
            result=diagnose(ROOT,a.account);atomic_save(ROOT/'artifacts/checkpoints/diagnosis.json',result)
        show(result);return 2 if result['blocked'] else 0
    except (CloudError,ValueError,OSError,subprocess.CalledProcessError) as exc:print(str(exc),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())

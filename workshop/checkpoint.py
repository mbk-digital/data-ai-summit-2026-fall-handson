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
import unicodedata
from urllib.parse import quote,urlencode
import uuid

from .resource_state import compare as compare_resource, equivalent, without_api_defaults
from .settings import ROOT,config
from .cloud import Client,CloudError,LABELS,dataset_path,table_rows,preflight,timestamp,validate_cloud_config
from .bigquery import expected_view_query,expected_table_fields
from .data import fixture
from .catalog import Catalog,concept,as_document
from .semantics_poc import API
from .data_agent import AGENT_ID,AgentClient,candidate_ids,deletion_state,fingerprint,validate_published
from .lab1 import CATALOG_URL,make_contexts
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
    def __init__(self,root=ROOT,cfg=None,account=None,overrides=None,check_prerequisites=True,quiet=False):
        self.root=Path(root);self.cfg=cfg or config(require_project=True,root=root);validate_cloud_config(self.cfg)
        self.account=account;self.c=Client(self.cfg,account)
        self.catalog=Catalog(self.cfg,account,root);self.catalog.c=self.c
        self.agent=AgentClient(self.cfg,account);self.agent.folder=self.root/'artifacts/data-agent'/self.c.project/self.cfg['dataset_id']/self.cfg['data_agent_location']
        self.obs=Observability(self.cfg,account);self.obs.c=self.c;self.obs.folder=self.root/'artifacts/observability'/self.c.project;self.obs.path=self.obs.folder/'resources.json'
        self.overrides=overrides or {};self.check_prerequisites=check_prerequisites;self.quiet=quiet
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
        # 削除済みの Agent と重ならない ID を、prepare と同じ規則で選ぶ（読むだけ）。
        base=self.agent.parent+'/dataAgents/ec-poc-view-'+fingerprint(self.context)
        d=self.get(base,lambda:self.agent.resolve('view',self.context,ledger=self.local,get=lambda n:self.agent.request('GET',self.agent.base+n,missing_ok=True)))
        if d is None:self.item(base,'data_agent',None,None,'missing','blocked')
        else:
            aname,agent,rec=d['name'],d['actual'],d['record'] or {}
            for name,actual in d['probed'].items():
                if name!=aname:self.snapshots[name]=actual
            if agent is not None and d['action']=='reuse':self.resource_matches(rec.get('resource'),agent)  # metadata warning only
            match=False
            if deletion_state(agent)=='usable':
                try:validate_published(agent,d['marker'],self.context,aname);match=True
                except CloudError:pass
            action='none' if d['action']=='reuse' else 'create' if d['action']=='create' else 'adopt' if d.get('adoptable') else 'blocked'
            self.item(aname,'data_agent',agent,match if agent else None,'matched' if action=='none' else 'changed' if d['record'] is not None else 'missing',action,d['message'] if action=='blocked' else '')
            if action=='adopt':self.adopt(aname,d['path'],value={'name':aname,'description':d['marker'],'state':'present','resource':agent,'checked_at':timestamp()})
        self.scan_bigquery()
        self.scan_observability()
        # lab2.json は root_agent をコマンドで直接動かすときの既定値。#3 の評価は計画ごとに --cache を渡すので、
        # true・false・ファイルなしのどれでも開始できる。書き換えない。形式が壊れていると root_agent が読めないので止める。
        # 中身が null だけのファイルは「ファイルなし」と区別する（ファイルがあるかどうかで判定する）。
        path=self.root/'participant/lab2.json';value=self.local(path)
        if value is None:self.item('participant/lab2.json','file',None,None,'not_applicable')
        else:
            try:options=json.loads(value)
            except ValueError:options=None
            valid=isinstance(options,dict) and set(options)=={'evidence_cache_enabled'} and type(options['evidence_cache_enabled']) is bool
            self.item('participant/lab2.json','file',value,valid,'not_applicable','none' if valid else 'blocked','' if valid else '未知の設定を保持。内容を確認')
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
            try:pre=preflight(self.c,'lab2',quiet=self.quiet)
            except (CloudError,ValueError,OSError) as exc:self.errors.append({'resource_id':'prerequisites','error':str(exc)})
            try:dependencies=importlib.metadata.version('google-adk')=='2.8.0'
            except importlib.metadata.PackageNotFoundError:dependencies=False
            if not dependencies:self.errors.append({'resource_id':'ADK','error':'共通の事前準備の uv sync --locked をやり直してください（google-adk 2.8.0 が必要です）'})
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


def diagnose(root=ROOT,account=None,overrides=None,check_prerequisites=True,quiet=False):
    return Inspector(root,account=account,overrides=overrides,check_prerequisites=check_prerequisites,quiet=quiet).scan()


def write_plan(root,account=None,result=None,before=None):
    # start は、確認を求めた診断の結果と、その診断の前に控えた設定・教材の版を計画として保存する。
    value=dict(result) if result is not None else diagnose(root,account);ident=uuid.uuid4().hex[:16]
    value.update(plan_id=ident,**(before or {'config':config(require_project=True,root=root),'source_snapshot':source_snapshot(root)}))
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
    return key in fixed or key in prefixes or (key.startswith(agent) and re.fullmatch(AGENT_ID.pattern+r'\.(resource|config)\.json',key[len(agent):]) is not None)


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


def apply(root,ident,accept_resources=(),account=None,quiet=False,progress=None):
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
        current=diagnose(root,account,quiet=quiet)
        if current['blocked'] or current['signature']!=plan['signature']:raise CloudError('計画後にクラウドまたはローカル状態が変わりました。再診断してください')
        journal={'plan_id':ident,'state':'applying','completed':[]};atomic_save(folder/'apply.json',journal)
        def done(step):
            journal['completed'].append(step);atomic_save(folder/'apply.json',journal)
            if progress:progress(step)
        try:
            updates={}
            for resource,change in plan['adoptions'].items():
                key=change['path']
                if not path_allowed(root,cfg,key):raise CloudError('許可範囲外の台帳パス')
                if change['entry']:
                    updates.setdefault(key,read(root/key))[change['entry']]=change['value']
                else:updates[key]=change['value']
            for key,value in updates.items():write_local(root,key,json.dumps(value,ensure_ascii=False,indent=2)+'\n',folder/'before')
            done('verified_ledger_adoption')
            for key,value in plan['local_writes'].items():
                if key!='participant/semantics.txt':raise CloudError('許可範囲外の参加者ファイル')
                write_local(root,key,value,folder/'before')
            done('participant_files')
            bq_actions={i['action'] for i in plan['items'] if i['kind'] in ('dataset','table','view')}
            if bq_actions-{'none','adopt','adopt_with_dataset'}:
                command=[sys.executable,'-m','workshop.cloud','bootstrap-bigquery','--stage','lab2']
                if account:command+=['--account',account]
                try:subprocess.run(command,cwd=root,check=True,capture_output=quiet,text=quiet)
                except subprocess.CalledProcessError as exc:
                    if not quiet:raise
                    lines=(exc.stderr or exc.stdout or '').strip().splitlines()
                    raise CloudError('BigQueryの準備で停止しました'+('：'+lines[-1] if lines else '')) from exc
            done('bigquery')
            cat=Catalog(cfg,account,root);receipt=cat.sync(as_document((root/'participant/semantics.txt').read_text()))
            done('catalog')
            agent=AgentClient(cfg,account);agent.folder=root/'artifacts/data-agent'/cfg['project_id']/cfg['dataset_id']/cfg['data_agent_location']
            agent.ensure('view',make_contexts(cfg,(root/'participant/semantics.txt').read_text(),receipt)['view'])
            done('data_agent')
            if any(i['kind']=='observability' and i['action']=='create' for i in plan['items']):
                obs=Observability(cfg,account);obs.folder=root/'artifacts/observability'/cfg['project_id'];obs.path=obs.folder/'resources.json';obs.ledger=read(obs.path)
                (root/'artifacts/observability').mkdir(parents=True,exist_ok=True)
                with (root/'artifacts/observability/.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);obs.enable(quiet)
            done('observability')
            final=diagnose(root,account,quiet=quiet);atomic_save(folder/'after.json',final)
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
        ids=candidate_ids('view',fingerprint(make_contexts(cfg,rules,{'title':fields['title'],'body':body})['view']))
        folder=root/'artifacts/data-agent'/cfg['project_id']/cfg['dataset_id']/cfg['data_agent_location']
        # 現行 Agent は、削除済みでない台帳がある候補。なければ元の ID（今までと同じ）。
        active=[i for i in ids if (folder/(i+'.resource.json')).is_file() and json.loads((folder/(i+'.resource.json')).read_text()).get('state')!='deleted']
        if len(active)>1:raise CloudError('現行Agentの台帳が複数あります。Checkpointで診断してください: '+', '.join(active))
        current_id=(active or ids)[0]
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
            if rec.get('state')=='deleted':
                # 片付けた Agent の台帳は、実物がない（404）か、削除済み（deleteTime あり）のときだけ戻す。
                if deletion_state(actual) not in ('absent','deleted'):raise CloudError('バックアップでは削除済みのAgentが、削除済みと確認できません。復元しません: '+name)
                continue
            expected=rec.get('resource',{})
            from .data_agent import published_context
            delta=compare_resource(expected,actual)['metadata_changes']
            if delta:result['metadata_changes'].append({'resource_id':name,'changes':delta})
            if deletion_state(actual)!='usable' or rec.get('state')!='present' or not equivalent(expected,actual) or published_context(actual)!=published_context(expected) or actual.get('description')!=rec.get('description'):raise CloudError('バックアップAgentが実リソースと一致しません（状態='+str(rec.get('state'))+'）: '+name)
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
    # #3 の質問を送る前の確認。参加者には 1 行だけ。詳しい結果と警告は diagnosis.json に残す。
    result=diagnose(root,account,quiet=True)
    atomic_save(root/'artifacts/checkpoints/diagnosis.json',result)
    if not result['ready']:
        show_start(result)
        details=[i['resource_id']+': '+i['action'] for i in result['items'] if i['action'] not in ('none','use_existing_unowned')]
        details += [e['resource_id']+': '+e['error'] for e in result['errors']]
        raise CloudError('\n開始確認で止まりました。質問は送っていません。uv run --locked python3 -m workshop.checkpoint start で開始確認をやり直し、止まる場合は講師に声をかけてください。\n詳しい結果：artifacts/checkpoints/diagnosis.json\n'+'\n'.join(details))
    print('開始確認：OK',flush=True)
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
        print('Knowledge Catalog: '+console_links(result)[1][1])
        print('BigQuery Agents: https://console.cloud.google.com/bigquery/agents_hub?project='+project)


# start：参加者向けの入口。診断を 4 つのまとまりで見せ、作るものがあれば確認してから既存の plan・apply で適用する。
OK_ACTIONS=('none','use_existing_unowned')
CREATE_NOTES={'create':'','complete_definition':'（標準の業務定義を使います）','restore_definition':'（Knowledge Catalog に保存された業務定義を使います）',
              'update_owned_definition':'（Knowledge Catalog を手元の業務定義に合わせます）','seed_empty_owned_table':'（空のテーブルにデータを入れます）'}
GROUPS=(('data','分析データ（BigQuery）'),('definition','業務定義（Knowledge Catalog）'),('agent','分析エージェント「EC分析 ③」'),('setup','実行の準備（API・権限・観測）'))
KIND_GROUPS={'dataset':'data','table':'data','view':'data','catalog_dependency':'definition','catalog_entry':'definition','data_agent':'agent','observability':'setup'}
LEVELS=('OK','これから作ります','要相談','確認できませんでした')
APPLY_LABELS={'bigquery':'分析データ','catalog':'業務定義','data_agent':'分析エージェント','observability':'実行の準備（観測）'}
APPLY_GROUPS={'bigquery':'data','catalog':'definition','data_agent':'agent','observability':'setup'}
# エージェント カタログを開く URL（2026-11-05 に Console で確認。画面の中で使われている形で、公開の仕様ではない）。
AGENT_CATALOG_URL='https://console.cloud.google.com/bigquery/agents_hub;caPath=%2Fbq2%2Fagents-catalog?project='
APPLY_STEPS=(('participant_files','参加者ファイル'),('bigquery','分析データ'),('catalog','業務定義'),('data_agent','分析エージェント'),('observability','観測の設定'))


def item_group(i):
    if i['kind']=='file':return 'definition' if i['resource_id']=='participant/semantics.txt' else 'setup'
    return KIND_GROUPS.get(i['kind'],'setup')


def item_level(i):
    action=i['action']
    if action in OK_ACTIONS:return 0,''
    if action in CREATE_NOTES:return 1,CREATE_NOTES[action]
    if action in ('adopt','adopt_with_dataset'):return 2,'（教材の記録がありません。講師が確かめます）'
    if i['resource_id']=='participant/semantics.txt':return 2,'（participant/semantics.txt が編集の途中です）'
    return 2,'（教材の記録や内容と、実際のものが合いません）'


def error_level(e):
    if e['resource_id']=='ADK':return 3,'（ADK が入っていません）'
    if e['resource_id']=='prerequisites':return 3,'（API・権限・課金のどれかを確かめられません）'
    return 3,'（確かめる途中でエラーが出ました）'


def summarize(result):
    """まとまりごとに、中の項目のいちばん悪い状態を返す。"""
    found={key:[] for key,_ in GROUPS};owners={i['resource_id']:item_group(i) for i in result['items']}
    for i in result['items']:found[item_group(i)].append((*item_level(i),i['action']))
    for e in result['errors']:
        name=e['resource_id'];key=owners.get(name) or next((owners[k] for k in sorted(owners,key=len,reverse=True) if name.startswith(k+'/')),'setup')
        found[key].append((*error_level(e),None))
    rows=[]
    for key,label in GROUPS:
        if not found[key]:rows.append((key,label,None,'確かめていません（ほかの項目を先に解決します）'));continue
        level=max(x[0] for x in found[key]);notes=[x for x in found[key] if x[0]==level]
        status=LEVELS[level]
        if level==1 and not any(x[2] in ('create','complete_definition','restore_definition') for x in notes):status='これから直します'
        rows.append((key,label,level,status+next((x[1] for x in notes if x[1]),'')))
    return rows


def created_groups(result):
    """作る・直すものがあるまとまり。参加者ファイルは業務定義のものだけ数える。"""
    return {item_group(i) for i in result['items'] if item_level(i)[0]==1 and (i['kind']!='file' or item_group(i)=='definition')}


def progress(groups):
    """作成中の表示。最初から OK だったまとまりには「完了」を出さない（作り直したように読めるため）。"""
    def report(step):
        if step in APPLY_LABELS and APPLY_GROUPS[step] in groups:print('  '+APPLY_LABELS[step]+' ……… 完了',flush=True)
        if step=='observability':print('もう一度確かめています…',flush=True)
    return report


def creatable(result):
    return not result['blocked'] and not result['adoptions'] and all(i['action'] in OK_ACTIONS or i['action'] in CREATE_NOTES for i in result['items'])


def pad(text,width):
    return text+' '*max(2,width-sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text))


def console_links(result):
    project=result['project_id'];entry=next((i['resource_id'] for i in result['items'] if i['kind']=='catalog_entry'),None)
    entry=((result.get('cloud_snapshot') or {}).get(entry) or {}).get('name') or entry
    catalog=CATALOG_URL+quote(entry,safe='/')+'?'+urlencode({'project':project}) if entry else 'https://console.cloud.google.com/dataplex/dp-search?project='+project
    return [('分析データ','https://console.cloud.google.com/bigquery?project='+project),('業務定義',catalog),('分析エージェント',AGENT_CATALOG_URL+project)]


def show_start(result):
    print()
    for _,label,_,status in summarize(result):print('  '+pad(label,36)+status)


def show_ready(result):
    print();print('開始できます。次の 3 つを Console で開き、同じ Project で中身を確かめます。')
    for n,(label,url) in enumerate(console_links(result),1):print('  '+str(n)+'. '+label+'：'+url)
    if result.get('cost_collection',{}).get('job_metadata_access')!='granted':
        print();print('注意：BigQuery のジョブ情報を読む権限が確認できませんでした。実習は始められますが、#3 で BigQuery 分の費用が「未算出」になる場合があります。')


def show_stopped():
    print();print('開始できません。何も変更していません。講師に声をかけてください。')
    print('伝えること：この画面の内容と Project ID。詳しい結果は artifacts/checkpoints/diagnosis.json に保存しました。')


def show_interrupted(root,ident,exc):
    folder=root/'artifacts/checkpoints'/ident;journal=read(folder/'apply.json')
    print()
    if not journal:
        # apply は、状態の照合が通ってから記録を作る。記録がなければ、まだ何も変えていない。
        print('作成を始める前に止まりました。何も変更していません。')
        print('理由：'+(str(exc) or type(exc).__name__))
        print('もう一度同じコマンドを実行してください。同じ表示になる場合は、講師に声をかけてください。')
        return
    completed=journal.get('completed',[])
    finished=[label for step,label in APPLY_STEPS if step in completed]
    rest=[label for step,label in APPLY_STEPS if step not in completed]
    print('作成の途中で止まりました。一部はすでに作成・変更されている可能性があります。')
    print('  終わったもの：'+('、'.join(finished) or 'なし'))
    print('  止まったところ：'+(rest[0] if rest else '最後の確認')+'（エラー：'+(journal.get('error') or str(exc) or type(exc).__name__)+'）')
    if (folder/'before').is_dir():print('変更する前の参加者ファイルは artifacts/checkpoints/'+ident+'/before/ に退避してあります。')
    print('講師に声をかけてください。教材フォルダと artifacts/ は消さないでください。')


def start(root=ROOT,account=None,assume_yes=False,ask=input,interactive=None):
    """--yes は入力の確認を省くだけ。止まる条件と、適用直前の再診断・署名の照合は省かない。"""
    cfg=config(require_project=True,root=root)
    print('開始確認：Project '+cfg['project_id'],flush=True)
    print('確かめています（1 分ほどかかります）…',flush=True)
    # 計画には、診断の前に控えた設定と教材の版を入れる。確認を待つ間の変更は apply が検出して止める。
    before={'config':cfg,'source_snapshot':source_snapshot(root)}
    result=diagnose(root,account,quiet=True);atomic_save(root/'artifacts/checkpoints/diagnosis.json',result)
    show_start(result)
    if result['ready']:show_ready(result);return 0
    if not creatable(result):show_stopped();return 2
    verbs=[v for v in ('これから作ります','これから直します') if any(r[3].startswith(v) for r in summarize(result))]
    print();print('この Project（'+cfg['project_id']+'）に、「'+'」「'.join(verbs)+'」のものを用意します。')
    print('変更する参加者のファイルは、先に退避します。BigQuery の利用料金が少しかかります。')
    if not assume_yes:
        if not (sys.stdin.isatty() if interactive is None else interactive):
            print('入力を受け付けられないため、ここで止めます。何も変更していません。Cloud Shell のターミナルから実行してください。');return 2
        try:answer=ask('続けますか？ [y/N]: ')
        except EOFError:answer=''
        if answer.strip().lower() not in ('y','yes'):print('中止しました。何も変更していません。');return 1
    plan=write_plan(root,account,result,before)
    print();print('作成しています（2〜3 分かかります）…',flush=True)
    try:final=apply(root,plan['plan_id'],(),account,quiet=True,progress=progress(created_groups(result)))
    except (Exception,KeyboardInterrupt) as exc:show_interrupted(root,plan['plan_id'],exc);return 2
    atomic_save(root/'artifacts/checkpoints/diagnosis.json',final)
    show_start(final)
    if final['ready']:show_ready(final);return 0
    print();print('作成は終わりましたが、まだ開始できません。講師に声をかけてください。')
    print('伝えること：この画面の内容と Project ID。詳しい結果は artifacts/checkpoints/'+plan['plan_id']+'/after.json に保存しました。')
    return 2


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['start','diagnose','plan','apply','backup','restore-backup']);p.add_argument('--yes',action='store_true',help='startのみ。確認の入力を省く（止まる条件と適用直前の照合は省かない）');p.add_argument('--account');p.add_argument('--plan');p.add_argument('--accept-resource',action='append',default=[]);p.add_argument('--file',type=Path);p.add_argument('--execute',action='store_true');p.add_argument('--current-agent-only',action='store_true',help='backupのみ。現行のView Agentに絞り、除外した過去台帳の一覧を保存');a=p.parse_args()
    try:
        if a.command=='start':return start(ROOT,a.account,a.yes)
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

"""Review then explicitly refresh metadata-only ledger differences, including listed API default fields (GET-only cloud access)."""
import argparse
import copy
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import re
import sys
import uuid

from . import checkpoint as cp
from .settings import ROOT, config
from .resource_state import compare, RESOURCE
from .data_agent import AgentClient
from .cloud import CloudError, timestamp
from .catalog import Catalog
from .observability import Observability
from .economics import atomic_save

VERSION = 'ledger-refresh-v1'


def inventory(root, account=None):
    cfg=config(True,root);cat=Catalog(cfg,account,root);agent=AgentClient(cfg,account);obs=Observability(cfg,account)
    agent.folder=root/'artifacts/data-agent'/cfg['project_id']/cfg['dataset_id']/cfg['data_agent_location']
    obs.path=root/'artifacts/observability'/cfg['project_id']/'resources.json'
    paths=[(cat.ledger_path,'catalog'),(obs.path,'observability')]
    paths += [(p,'agent') for p in sorted(agent.folder.glob('*.resource.json'))]
    files={};changes=[];errors=[];checked=[]
    for path,kind in paths:
        key=str(path.relative_to(root));cp.safe_path(root,key)
        if not cp.path_allowed(root,cfg,key):raise CloudError('許可範囲外の台帳パス: '+key)
        if not path.is_file():continue  # Never adopt unrecorded cloud resources.
        text=path.read_text();files[key]=text;value=json.loads(text)
        records={value.get('name',''):value} if kind=='agent' else value
        if not isinstance(records,dict):raise CloudError('台帳形式が不正: '+key)
        for name,record in records.items():
            if record.get('state')=='deleted':continue
            field='resource' if kind=='agent' else 'snapshot';before=record.get(field)
            allowed=(name==agent.parent+'/dataAgents/'+path.name.removesuffix('.resource.json') if kind=='agent' else
                     name.startswith(cat.parent+'/entryGroups/') or name.startswith(cat.parent+'/entryTypes/') if kind=='catalog' else
                     name in (obs.name,obs.name+'/settingBindings/workshop-project'))
            if not allowed or not RESOURCE.fullmatch(name) or record.get('state')!='present' or not isinstance(before,dict) or before.get('name')!=name:
                errors.append({'resource_id':name,'error':'対象範囲・台帳の状態・識別情報を確認できません'});continue
            try:
                actual=(agent.request('GET',agent.base+name,missing_ok=True) if kind=='agent' else cat.get(name) if kind=='catalog' else obs.get(name))
                comparison=compare(before,actual)
                if not comparison['equivalent']:
                    errors.append({'resource_id':name,'error':'内容・作成時の識別情報の不一致、またはリソース不在。最新化しません'});continue
                checked.append({'resource_id':name,'resource':actual})
                if comparison['metadata_changes']:
                    changes.append({'path':key,'entry':None if kind=='agent' else name,'field':field,'resource_id':name,
                                    'before':before,'after':actual,'metadata_changes':comparison['metadata_changes']})
            except (CloudError,ValueError,OSError) as exc:errors.append({'resource_id':name,'error':str(exc)})
    return {'version':VERSION,'config':cfg,'files':files,'changes':changes,'checked':checked,'errors':errors,'blocked':bool(errors)}


def plan(root=ROOT,account=None):
    value=inventory(root,account);value.update(plan_id=uuid.uuid4().hex[:16],checked_at=timestamp(),source_snapshot=cp.source_snapshot(root))
    folder=root/'artifacts/ledger-refresh'/value['plan_id'];folder.mkdir(parents=True,exist_ok=False)
    atomic_save(folder/'plan.json',value);(folder/'plan.sha256').write_text(cp.sha(value))
    return value


def apply(root,ident,account=None):
    if not re.fullmatch('[a-f0-9]{16}',ident):raise CloudError('不正な計画ID')
    folder=cp.safe_path(root,'artifacts/ledger-refresh/'+ident);value=cp.read(folder/'plan.json')
    if cp.sha(value)!=(folder/'plan.sha256').read_text() or value.get('version')!=VERSION:raise CloudError('計画の内容・版が変更されています')
    if value['blocked']:raise CloudError('未確認・不一致がある計画は適用できません')
    if (folder/'apply.json').exists():raise CloudError('この計画は実行済みまたは中断済みです。記録を確認して新しい計画を作成してください')
    if value['config']!=config(True,root) or value['source_snapshot']!=cp.source_snapshot(root):raise CloudError('設定または教材の版が変わりました')
    cat=Catalog(value['config'],account,root)
    with ExitStack() as stack:
        keys=['artifacts/.root-execution.lock','artifacts/checkpoints/.lock','artifacts/lab1/.run.lock',
              'artifacts/semantics-poc/.lock',str(cat.folder.relative_to(root)/'.lock'),'artifacts/observability/.lock']
        for key in keys:
            path=cp.safe_path(root,key);path.parent.mkdir(parents=True,exist_ok=True)
            stream=stack.enter_context(path.open('a'));fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fresh=inventory(root,account)
        # Exact current responses, including metadata: a reviewed plan is not a waiver for later changes.
        expected={k:value[k] for k in fresh}
        if fresh!=expected:raise CloudError('計画後にクラウドまたは台帳が変わりました。新しい計画を作成してください')
        updated={}
        for change in fresh['changes']:
            key=change['path'];body=updated.setdefault(key,json.loads(fresh['files'][key]))
            record=body if change['entry'] is None else body[change['entry']]
            record[change['field']]=copy.deepcopy(change['after'])
        journal={'plan_id':ident,'state':'applying','started_at':timestamp(),'completed':[],
                 'changes':[{'resource_id':c['resource_id'],'metadata_changes':c['metadata_changes']} for c in fresh['changes']]}
        atomic_save(folder/'apply.json',journal)
        try:
            for key,body in updated.items():
                if cp.safe_path(root,key).read_text()!=fresh['files'][key]:raise CloudError('適用中に台帳が変更されました: '+key)
                cp.write_local(root,key,json.dumps(body,ensure_ascii=False,indent=2)+'\n',folder/'before')
                journal['completed'].append(key);atomic_save(folder/'apply.json',journal)
            journal['state']='completed'
        except Exception as exc:
            journal.update(state='interrupted',error=str(exc));raise
        finally:
            journal['finished_at']=timestamp();atomic_save(folder/'apply.json',journal)
    return journal


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['plan','apply']);parser.add_argument('--plan');parser.add_argument('--account');args=parser.parse_args()
    try:
        cfg=config(True,ROOT);print('対象Project: '+cfg['project_id']+' / '+cfg['dataset_id'],flush=True)
        if args.command=='apply':
            if not args.plan:parser.error('--plan が必要です')
            result=apply(ROOT,args.plan,args.account);print('台帳更新: '+result['state']+' / '+str(len(result['completed']))+'ファイル');return 0
        result=plan(ROOT,args.account);print('台帳更新計画: '+result['plan_id'])
        for change in result['changes']:print(change['resource_id']+' | '+json.dumps(change['metadata_changes'],ensure_ascii=False))
        for error in result['errors']:print('停止: '+json.dumps(error,ensure_ascii=False))
        print('クラウドは読み取りのみ。差分確認後: uv run --locked python3 -m workshop.ledger_refresh apply --plan '+result['plan_id'])
        return 2 if result['blocked'] else 0
    except (CloudError,ValueError,OSError) as exc:print(str(exc),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())

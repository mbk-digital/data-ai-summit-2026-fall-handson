"""Cloud snapshots and conservative, same-plan cache provenance estimates."""
import copy
import hashlib
import json
from .resource_state import stable
from .settings import config
from .cloud import Client,CloudError,dataset_path,timestamp
from .data import FIELDS
from .bigquery import expected_table_fields,expected_view_query
from .data_agent import AgentClient,fingerprint,validate_published,published_context
from .catalog import as_document,concept
from .lab1 import make_contexts
from .economics import epoch


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def capture(root,account=None):
    cfg=config(True,root);client=Client(cfg,account);items={};errors=[]
    for name in [*FIELDS,'v_revenue_events']:
        try:
            resource=client.request('GET',dataset_path(client)+'/tables/'+name,missing_ok=True)
            if resource is None:raise CloudError('resource absent')
            if name=='v_revenue_events':
                if resource.get('type')!='VIEW' or resource.get('view',{}).get('query','').strip().rstrip(';').strip()!=expected_view_query(cfg):raise CloudError('View differs from workshop definition')
            elif resource.get('type')!='TABLE' or resource.get('schema',{}).get('fields')!=expected_table_fields(name):raise CloudError('Table schema differs from workshop definition')
            if not resource.get('lastModifiedTime') or not resource.get('creationTime'):raise CloudError('Missing table timestamps')
            items[cfg['project_id']+'.'+cfg['dataset_id']+'.'+name]={k:resource.get(k) for k in ('id','type','schema','view','creationTime','lastModifiedTime','etag')}
        except (CloudError,ValueError,OSError) as exc:errors.append({'resource':name,'error':str(exc)})
    agent=None;metadata={}
    try:
        rules=(root/'participant/semantics.txt').read_text()
        if '[[' in rules or ']]' in rules:raise ValueError('Complete the workshop definition first')
        fields,body=concept(as_document(rules));ctx=make_contexts(cfg,rules,{'title':fields['title'],'body':body})['view']
        client=AgentClient(cfg,account);name=client.parent+'/dataAgents/ec-poc-view-'+fingerprint(ctx)
        resource=client.request('GET',client.base+name,missing_ok=True)
        if resource is None:raise CloudError('Data Agent absent')
        validate_published(resource,'workshop_cli:summit_ec_v1:'+fingerprint(ctx),ctx,name)
        agent={'name':name,'published_context':published_context(resource),'resource_sha256':sha(stable(resource))}
        metadata={k:resource[k] for k in ('updateTime','etag') if k in resource}
    except (CloudError,ValueError,OSError) as exc:errors.append({'resource':'data_agent','error':str(exc)})
    content={'project_id':cfg['project_id'],'dataset_id':cfg['dataset_id'],'location':cfg['bigquery_location'],'tables':items,'agent':agent}
    return {'version':'cloud-provenance-v2','agent_metadata':metadata,'captured_at':timestamp(),'complete':not errors,'errors':errors,'content':content,'sha256':sha(content)}


def same_snapshot(before,after):
    return bool(before and after and before.get('version')==after.get('version') and before.get('complete') and after.get('complete') and before.get('sha256')==sha(before.get('content'))==after.get('sha256')==sha(after.get('content')))


def seconds(value):
    try:return epoch(value) if value else None
    except (ValueError,TypeError,OverflowError):return None


def references(values,cfg):
    if not isinstance(values,list) or not values:return None
    result=[]
    for t in values:
        if not isinstance(t,dict) or t.get('project_id')!=cfg['project_id'] or t.get('dataset_id')!=cfg['dataset_id'] or t.get('table_id') not in set(FIELDS)|{'v_revenue_events'}:return None
        result.append((t['project_id'],t['dataset_id'],t['table_id']))
    return sorted(set(result))


def supplement(plan,measurements,current):
    """Never replace observed referenced_tables or infer an actual cache-parent job."""
    cfg=plan['context']['config'];before=plan.get('cloud_snapshot');stable=same_snapshot(before,current)
    initial=seconds((before or {}).get('captured_at'));collected=seconds((current or {}).get('captured_at'))
    pool=[];ids={t['run_id'] for t in plan['trials']}
    for run_id,measurement in measurements.items():
        if run_id not in ids:continue
        component=measurement.get('components',{}).get('bigquery_query',{})
        linked=component.get('attribution')=='time_agent_sql_estimate' and not component.get('ambiguous_job_ids')
        for job in component.get('job_evidence',[]):pool.append({'run_id':run_id,'job':job,'linked':linked})
    results={}
    for target in pool:
        job=target['job']
        if job.get('cache_hit') is not True or job.get('referenced_tables'):continue
        t=seconds(job.get('creation_time'));end=seconds(job.get('end_time'));candidates=[]
        for source in pool:
            old=source['job'];start=seconds(old.get('creation_time'));finish=seconds(old.get('end_time'))
            if old.get('job_id')==job.get('job_id') or old.get('cache_hit') is True:continue
            if not job.get('query') or old.get('query')!=job['query']:continue
            if t is None or (start is not None and not 0<t-start<=86400):continue
            refs=references(old.get('referenced_tables'),cfg);labels=job.get('labels') or {};oldlabels=old.get('labels') or {}
            conditions={'same_plan':True,'exact_query':True,'within_search_window':start is not None and 0<t-start<=86400,
                'same_project':old.get('project_id')==job.get('project_id')==cfg['project_id'],
                'same_agent':bool(labels.get('data-agent-id')) and labels.get('data-agent-id')==oldlabels.get('data-agent-id') and labels.get('ca-bq-job')==oldlabels.get('ca-bq-job')=='true' and labels.get('data-agent-id')==((before or {}).get('content',{}).get('agent') or {}).get('name','').rsplit('/',1)[-1],
                'same_principal':bool(job.get('principal_sha256')) and job.get('principal_sha256')==old.get('principal_sha256'),
                'source_noncache_success':old.get('cache_hit') is False and old.get('state')=='DONE' and not old.get('error_result') and old.get('statement_type')=='SELECT' and not old.get('parent_job_id'),
                'target_success':job.get('state')=='DONE' and not job.get('error_result') and job.get('statement_type')=='SELECT' and not job.get('parent_job_id'),
                'unambiguous_run_links':bool(source['linked'] and target['linked']),
                'ordered_times':initial is not None and start is not None and finish is not None and end is not None and collected is not None and initial<=start<=finish<=t<=end<=collected,
                'cloud_snapshot_unchanged':stable,'references_in_scope':bool(refs)}
            unchanged=bool(refs and stable and start is not None)
            if unchanged:
                for ref in refs:
                    meta=current['content']['tables'].get('.'.join(ref),{})
                    try:unchanged=unchanged and float(meta['lastModifiedTime'])/1000<=start
                    except (KeyError,ValueError,TypeError):unchanged=False
                # Explicitly check the logical view even when jobs report only underlying tables.
                view=current['content']['tables'].get(cfg['project_id']+'.'+cfg['dataset_id']+'.v_revenue_events',{})
                try:unchanged=unchanged and float(view['lastModifiedTime'])/1000<=start
                except (KeyError,ValueError,TypeError):unchanged=False
            conditions['table_and_view_unchanged_before_source']=unchanged
            candidates.append({'source_run_id':source['run_id'],'source_job_id':old.get('job_id'),'conditions':conditions,'referenced_tables':old.get('referenced_tables') or []})
        refsets={sha(references(c['referenced_tables'],cfg)) for c in candidates}
        valid=bool(candidates) and all(all(c['conditions'].values()) for c in candidates) and len(refsets)==1
        reasons=[]
        if not stable:reasons.append('評価計画作成時と照合時のクラウド状態が未確認または不一致')
        if not candidates:reasons.append('同じ計画内に完全一致SQL・24時間以内の非キャッシュ候補なし')
        if candidates and not valid:reasons.append('候補条件の不足・不一致、または参照テーブルの競合')
        if valid:reasons.append('同条件の過去ジョブから取得元を推定。実際のキャッシュ元ジョブを特定したものではない')
        results.setdefault(target['run_id'],{})[job['job_id']]={'status':'estimated' if valid else 'unverified','method':'same_plan_past_job_estimate','target_job_id':job['job_id'],
            'estimated_referenced_tables':copy.deepcopy(candidates[0]['referenced_tables']) if valid else [],'candidates':candidates,'reasons':reasons,'snapshot_sha256':current.get('sha256') if current else None}
    return results

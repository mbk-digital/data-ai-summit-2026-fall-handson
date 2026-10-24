"""Frozen public evaluation plans and resumable, serial Data Agent trials."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
import uuid

from .settings import ROOT, config
from .cloud import timestamp, CloudError
from .data import FIELDS
from .data_agent import fingerprint
from .reference import all_answers
from .evaluation import check_answer
from .economics import atomic_save, read, digest, collect as collect_costs

VERSION = 'benchmark-v1'
ID = re.compile(r'^[a-f0-9]{16}$')
DEFAULT_MODEL = 'gemini-3.8-flash'
# Summit basic task: the same agent asking Data Agent twice or reusing one result.
# The fixed-SQL and router routes stay available through --route for the Challenge.
PRESETS = {'summit': {'cases': ['Q01', 'Q04', 'Q08'], 'repeats': 1,
                      'variants': [('off', 'data_agent'), ('on', 'data_agent')]}}
ROUTE_LABELS = {'data_agent': 'Data Agent', 'verified_sql': '決まった SQL', 'router': '振り分け'}


def variant_label(model, cache, route, models=1):
    base = ('Data Agent・毎回取得' if cache == 'off' else 'Data Agent・再利用') if route == 'data_agent' else ROUTE_LABELS[route] + ('（毎回取得）' if cache == 'off' else '')
    return base + ('（' + model + '）' if models > 1 else '')


def label_of(value, name):
    variant = next((v for v in value.get('variants', []) if v['name'] == name), {})
    return variant.get('label') or name


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def frozen_context(root):
    cfg=config(require_project=True,root=root)
    rules=(root/'participant/semantics.txt').read_text(encoding='utf-8')
    if not rules.strip() or '[[' in rules or ']]' in rules:raise ValueError('先に#1の定義を完成させてください')
    files=list((root/'workshop').glob('*.py'))+[root/'agent/root-instructions.md',root/'requirements-adk.txt',root/'uv.lock']+list((root/'data').glob('*'))
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files) if p.is_file()}
    return {'config':cfg,'rules_sha256':hashlib.sha256(rules.encode()).hexdigest(),'files':hashes,
            'root_context':fingerprint({'config':cfg,'rules':rules})}


def plan(root, case_ids=None, repeats=1, caches=('on',), models=(DEFAULT_MODEL,), language='ja',cloud_snapshot=None,routes=('data_agent',),pairs=None,preset=None):
    if not 1<=repeats<=10:raise ValueError('繰り返しは1〜10回')
    if not caches or len(set(caches))!=len(caches) or set(caches)-{'on','off'}:raise ValueError('Invalid cache settings')
    if not models or len(set(models))!=len(models) or any(not re.fullmatch('[a-zA-Z0-9._-]+',m) for m in models):raise ValueError('Invalid models')
    if language not in ('ja','en'):raise ValueError('Invalid language')
    from .routes import POLICIES
    if not routes or len(set(routes))!=len(routes) or set(routes)-set(POLICIES):raise ValueError('Invalid routes')
    if pairs is not None and (not pairs or len(set(pairs))!=len(pairs) or any(c not in ('on','off') or r not in POLICIES for c,r in pairs)):raise ValueError('Invalid variant pairs')
    context=frozen_context(root)
    cases=[json.loads(line) for line in (root/'data/public_cases.jsonl').read_text().splitlines() if line.strip()]
    known={c['case_id'] for c in cases}
    if case_ids and (len(case_ids)!=len(set(case_ids)) or set(case_ids)-known):raise ValueError('問題IDが不正または重複しています')
    selected=[c for c in cases if not case_ids or c['case_id'] in case_ids]
    expected={a['case_id']:a for a in all_answers()}
    metadata=read(root/'data/evaluation-cases.json')
    prices=read(root/'data/pricing-2026-10-07.json')
    combos=list(pairs) if pairs is not None else [(cache,route) for cache in caches for route in routes]
    configs=[{'name':m+'-'+cache+('' if route=='data_agent' else '-'+route),'label':variant_label(m,cache,route,len(models)),'model':m,'cache':cache,'route_policy':route} for m in models for cache,route in combos]
    trials=[]
    for rep in range(repeats):
        # Rotate order between repetitions; preserve the exact order in the plan.
        order=configs[rep%len(configs):]+configs[:rep%len(configs)]
        for case in selected:
            for variant in order:
                trials.append({'run_id':uuid.uuid4().hex,'case_id':case['case_id'],'repetition':rep+1,
                    'question':case['question_'+language],'variant':variant['name']})
    if len(trials)>160:raise ValueError('初期Runnerは1計画160試行まで')
    ident=uuid.uuid4().hex[:16];folder=root/'artifacts/benchmarks'/ident
    folder.mkdir(parents=True,exist_ok=False)
    value={'version':VERSION,'batch_id':ident,'created_at':timestamp(),'preset':preset,'context':context,'language':language,
           'repeats':repeats,'variants':configs,'trials':trials,'pricing':prices,
           'cases':{c['case_id']:{'expected':expected[c['case_id']],**metadata['cases'][c['case_id']]} for c in selected},
           'evaluation_version':metadata['version'],'retry_policy':'no_automatic_trial_retry',
           'cloud_snapshot':cloud_snapshot,'cloud_data_snapshot':'cloud_metadata_and_agent_captured' if cloud_snapshot else 'local_fixture_only_no_cache_supplement'}
    atomic_save(folder/'plan.json',value);(folder/'plan.sha256').write_text(sha(value)+'\n')
    return value


def latest_plan(root, cfg):
    found=[]
    for path in (root/'artifacts/benchmarks').glob('*/plan.json'):
        if not ID.fullmatch(path.parent.name):continue
        value=read(path);c=value.get('context',{}).get('config',{})
        if c.get('project_id')==cfg.get('project_id') and c.get('dataset_id')==cfg.get('dataset_id'):found.append(value)
    if not found:raise ValueError('評価計画がありません。先に plan を実行してください')
    return max(found,key=lambda v:v.get('created_at',''))


def jst(stamp):
    try:return dt.datetime.fromisoformat(stamp).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime('%m/%d %H:%M')
    except (TypeError,ValueError):return '日時不明'


def unfinished(root, value):
    """True while some planned question is unsent or left running by a killed Runner (run resumes it)."""
    folder=root/'artifacts/benchmarks'/value['batch_id']
    return any(not attempt_path(folder,t).exists() or read(attempt_path(folder,t)).get('state')=='running' for t in value['trials'])


def q01_trial(value, cache):
    """Q01 answered by the Data Agent route with the given evidence cache, if planned."""
    for t in value['trials']:
        v=next((x for x in value['variants'] if x['name']==t['variant']),{})
        if t['case_id']=='Q01' and v.get('cache')==cache and v.get('route_policy','data_agent')=='data_agent':return t
    return None


def representative_trial(value):
    """The run whose Data Agent trace step 3 reads: one request, so one request's usage."""
    return q01_trial(value,'on') or q01_trial(value,'off') or value['trials'][0]


def data_agent_traces(root, cfg, run, account=None):
    """Data Agent traces that only this run can explain, matched as the cost check does."""
    from .cloud import Client
    from .economics import epoch, load_runs, trace_candidates
    from urllib.parse import urlencode
    if not run.get('started_at') or not run.get('finished_at'):return []
    since=dt.datetime.fromtimestamp(epoch(run['started_at'])-2,dt.timezone.utc).isoformat()
    until=dt.datetime.fromtimestamp(epoch(run['finished_at'])+2,dt.timezone.utc).isoformat()
    payload=Client(cfg,account).request('GET','https://cloudtrace.googleapis.com/v1/projects/'+cfg['project_id']+'/traces?'
                                        +urlencode({'startTime':since,'endTime':until,'view':'COMPLETE','pageSize':1000}))
    # A run without an end time may still own any later trace, as in the cost check.
    runs=[{**r,'finished_at':r.get('finished_at') or timestamp()} for r in load_runs(root,cfg)]
    return [t['traceId'] for t in payload.get('traces',[]) if trace_candidates(t,runs)==[run['run_id']]]


def print_links(root, cfg, value, account=None, attempts=4, wait=30):
    from .cloud import modern_trace_url, trace_console_url
    def run_of(trial):
        path=root/'artifacts/lab2'/trial['run_id']/'run.json'
        return read(path) if path.exists() else {}
    print('\n次の手順で開くリンク（Q01 の記録）',flush=True)
    # Step 3 compares the two Root traces side by side: two Data Agent requests versus one.
    for trial in [t for t in (q01_trial(value,'off'),q01_trial(value,'on')) if t] or [representative_trial(value)]:
        print('  Root Agent の処理（'+label_of(value,trial['variant'])+'）：'+(modern_trace_url(run_of(trial).get('trace_console_url')) or '記録がありません'),flush=True)
    trial=representative_trial(value);run=run_of(trial)
    ids=[]
    for i in range(attempts):
        try:ids=data_agent_traces(root,cfg,run,account)
        except (CloudError,OSError,ValueError):ids=[]
        if ids or i==attempts-1:break
        print('  Data Agent の処理の記録が届くのを待っています…',flush=True);time.sleep(wait)
    name='Data Agent の処理（'+label_of(value,trial['variant'])+'）'
    for n,ident in enumerate(ids,1):
        print('  '+name+('（'+str(n)+'）' if len(ids)>1 else '')+'：'+trace_console_url(cfg['project_id'],ident),flush=True)
    if not ids:
        print('  '+name+'：記録がまだ届いていません。1〜2 分後に、次のコマンドで表示できます。',flush=True)
        print('  uv run --locked python3 -m workshop.benchmark links',flush=True)


def load_plan(root, ident):
    if not ID.fullmatch(ident):raise ValueError('Invalid batch ID')
    folder=root/'artifacts/benchmarks'/ident;value=read(folder/'plan.json')
    if value.get('batch_id')!=ident or sha(value)!=(folder/'plan.sha256').read_text().strip():raise ValueError('評価計画が変更されています')
    return folder,value


def verify_context(root, value):
    if frozen_context(root)!=value['context']:raise ValueError('計画後に設定・定義・コード・価格表が変わりました。既存記録を保持し、新しい計画を作ってください')


def trial_command(root, value, trial, account=None):
    variant=next(v for v in value['variants'] if v['name']==trial['variant'])
    command=[sys.executable,'-m','workshop.root_agent','--question',trial['question'],
             '--model',variant['model'],'--cache',variant['cache'],'--run-id',trial['run_id'],
             '--expected-context',value['context']['root_context'],'--route',variant.get('route_policy','data_agent')]
    if account:command+=['--account',account]
    return command


def attempt_path(folder, trial):
    return folder/'attempts'/trial['run_id']/'attempt.json'


def kill_worker(process):
    if process.poll() is not None:return
    try:
        os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=3)
    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    except ProcessLookupError:pass


def execute(root, ident, account=None, timeout=480):
    folder,value=load_plan(root,ident);verify_context(root,value)
    if value.get('cloud_snapshot'):
        from .provenance import capture,same_snapshot
        snapshot=capture(root,account);atomic_save(folder/('cloud-before-run-'+uuid.uuid4().hex+'.json'),snapshot)
        if not same_snapshot(value['cloud_snapshot'],snapshot):raise CloudError('クラウド状態が評価計画と異なります。推論を開始せず、新しい計画を作成してください')
    lockpath=root/'artifacts/benchmarks/.runner.lock';lockpath.parent.mkdir(parents=True,exist_ok=True)
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # A previous killed Runner can leave its subprocess alive. Never resubmit it.
        root_lock=root/'artifacts/.root-execution.lock'
        with root_lock.open('a') as active:
            fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for index,trial in enumerate(value['trials'],1):
            verify_context(root,value)
            path=attempt_path(folder,trial)
            if path.exists():
                old=read(path)
                if old['state']=='running':
                    runpath=root/'artifacts/lab2'/trial['run_id']/'run.json'
                    run=read(runpath) if runpath.exists() else {}
                    # Root lock was free at entry: any saved result is now immutable.
                    old.update(state='recovered' if run.get('finished_at') else 'interrupted',finished_at=timestamp())
                    atomic_save(path,old)
                continue
            if (root/'artifacts/lab2'/trial['run_id']).exists():raise ValueError('予定IDに台帳外の記録があるため停止します')
            record={'run_id':trial['run_id'],'state':'running','started_at':timestamp(),'command_scope':'question_and_normal_runtime_options_only'}
            atomic_save(path,record)
            start=time.monotonic();process=None
            print(str(index)+'/'+str(len(value['trials']))+' '+trial['case_id']+' '+label_of(value,trial['variant'])+' に質問しています（実行 ID '+trial['run_id']+'）',flush=True)
            try:
                with (path.parent/'worker.log').open('w') as log:
                    process=subprocess.Popen(trial_command(root,value,trial,account),cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    code=process.wait(timeout=timeout)
                record.update(state='completed' if code==0 else 'failed',exit_code=code)
            except subprocess.TimeoutExpired:
                kill_worker(process);record.update(state='timeout',error='8分の上限に到達。自動再送しません')
            except KeyboardInterrupt:
                if process:kill_worker(process)
                record.update(state='interrupted',error='利用者が中断');raise
            except OSError as exc:record.update(state='failed',error=str(exc))
            finally:
                record.update(finished_at=timestamp(),wall_seconds=round(time.monotonic()-start,3));atomic_save(path,record)
    return value


def outcome(run):
    if not run or run.get('state')!='completed':return 'execution_failed'
    return {'OK':'answered','INSUFFICIENT_DATA':'abstained','UNSUPPORTED':'unsupported'}.get(run.get('answer',{}).get('status'),'execution_failed')


def evidence_source(run, evidence, measurement,cache_estimates=None):
    queries=[q for e in evidence for q in e.get('sql',[])]
    if not queries and run.get('answer',{}).get('status')=='INSUFFICIENT_DATA':
        return {'status':'not_required_for_abstention','jobs':[]}
    c=(measurement or {}).get('components',{}).get('bigquery_query',{})
    jobs=c.get('job_evidence',[])
    disallowed=[];missing=[]
    cfg=run.get('config',{})
    for job in jobs:
        refs=job.get('referenced_tables') or []
        extra=(cache_estimates or {}).get(job.get('job_id'),{})
        if not refs and job.get('cache_hit') is True and extra.get('status')=='estimated':refs=extra.get('estimated_referenced_tables') or []
        if not refs:missing.append(job.get('job_id'))
        for table in refs:
            if table.get('project_id')!=cfg.get('project_id') or table.get('dataset_id')!=cfg.get('dataset_id') or table.get('table_id') not in set(FIELDS)|{'v_revenue_events'}:
                disallowed.append(table)
    from .economics import match_sql_queries
    sql_proof=match_sql_queries(queries,[j.get('query') or '' for j in jobs],allow_alias=c.get('attribution')!='direct_job_id')
    sql_match=sql_proof['complete']
    alias_match=any(p['method']=='select_alias_case_estimate' for p in sql_proof['matches'])
    linked=c.get('attribution') in ('time_agent_sql_estimate','direct_job_id') and not c.get('ambiguous_job_ids')
    direct=c.get('attribution')=='direct_job_id'
    if direct:
        from .economics import job_candidates
        linked=linked and bool(run.get('direct_jobs')) and {j.get('job_id') for j in jobs}=={j.get('job_id') for j in run['direct_jobs']} and all(job_candidates(j,[run])==[run['run_id']] for j in jobs)
    status='failed' if disallowed else ('matched_direct' if direct else 'matched_alias_estimate' if alias_match else 'matched_estimate') if jobs and not missing and sql_match and linked and all(j.get('state')=='DONE' and not j.get('error_result') for j in jobs) else 'unverified'
    reasons=[]
    if not jobs:reasons.append('対応するジョブ証拠なし')
    if missing:reasons.append('参照テーブル情報なし（キャッシュヒット等）。SQL文字列だけで取得元を確認済みにしない')
    if disallowed:reasons.append('許可範囲外の参照テーブル')
    if not sql_match:reasons.append('回答に保存されたSQLとジョブSQLが一致しない')
    if not linked:reasons.append('実行とジョブの対応未確認または候補競合')
    return {'status':status,'jobs':jobs,'reasons':reasons,'unallowed_tables':disallowed,'scope':'job_metadata_and_allowed_tables_not_sql_semantic_proof','cache_supplement':cache_estimates or {},'sql_matching':sql_proof}


def common_timing(rows, variants):
    """Paired question/repetition timing, never a fastest/success-only sample."""
    names=[v['name'] for v in variants]
    all_keys=sorted({(r['case_id'],r['repetition']) for r in rows})
    finished=all(r['attempt_state'] not in ('pending','running') for r in rows)
    groups={name:{(r['case_id'],r['repetition']):r for r in rows if r['variant']==name} for name in names}
    selected=[];excluded=[]
    for key in all_keys:
        reasons=[]
        for name in names:
            r=groups[name].get(key)
            if not r:reasons.append({'variant':name,'reason':'missing_trial'});continue
            duration=r['elapsed_seconds']
            if r['attempt_state'] not in ('completed','recovered') or 'run_identity_or_frozen_config_mismatch' in r['errors']:
                reason='execution_or_identity_not_confirmed'
            elif not isinstance(duration,(int,float)) or isinstance(duration,bool) or not math.isfinite(duration) or duration<0:
                reason='duration_unavailable'
            elif r['outcome']=='answered':reason=None
            elif r['outcome']=='abstained' and r['structured_success']:reason=None
            else:reason='not_answered_or_valid_abstention'
            if reason:reasons.append({'variant':name,'reason':reason})
        entry={'case_id':key[0],'repetition':key[1]}
        if reasons:excluded.append({**entry,'reasons':reasons})
        else:selected.append(entry)
    available=len(names)>1 and finished
    pairs=selected if available else []
    summaries={name:{'median_seconds':statistics.median([groups[name][(p['case_id'],p['repetition'])]['elapsed_seconds'] for p in pairs]) if pairs else None,
                     'trials':len(pairs)} for name in names}
    return {'version':'common-response-pairs-v1','status':'complete' if available else 'pending' if not finished else 'single_variant',
            'pairs':pairs,'excluded':excluded,'total_pairs':len(all_keys),'groups':summaries,
            'basis':'same_question_and_repetition_across_all_variants; answered_including_wrong_answers_or_structured_valid_abstention',
            'selection':'post_run_descriptive_subset_not_overall_performance_or_quality_rank'}


def report(root, ident):
    folder,value=load_plan(root,ident);rows=[]
    cache_path=folder/'cache-provenance.json';cache=read(cache_path) if cache_path.exists() else {}
    measures=validated_measurements(root,value)
    if cache.get('plan_sha256')!=sha(value) or cache.get('measurement_sha256')!={k:sha(v) for k,v in measures.items()}:cache={}
    elif cache:
        from .provenance import supplement
        cache['estimates']=supplement(value,measures,cache.get('current_snapshot'))
    reviews=[read(p) for p in sorted((folder/'reviews').glob('*.json'))]
    for trial in value['trials']:
        path=attempt_path(folder,trial);attempt=read(path) if path.exists() else {'state':'pending'}
        runfolder=root/'artifacts/lab2'/trial['run_id'];runpath=runfolder/'run.json'
        run=read(runpath) if runpath.exists() else {}
        evidence=[read(p) for p in sorted(runfolder.glob('evidence-*.json'))]
        measurepath=runfolder/'economics.json';measurement=read(measurepath) if measurepath.exists() else None
        evidence_hash=digest(run,evidence)
        if measurement and (measurement.get('input_sha256')!=evidence_hash or measurement.get('pricing_sha256')!=hashlib.sha256(json.dumps(value['pricing'],sort_keys=True).encode()).hexdigest()):measurement=None
        case=value['cases'][trial['case_id']];errors=[]
        variant=next(v for v in value['variants'] if v['name']==trial['variant'])
        expected_hashes={k:v for k,v in value['context']['files'].items() if k in ('workshop/root_agent.py','workshop/root_evidence.py','workshop/routes.py','agent/root-instructions.md','requirements-adk.txt')}
        identity=(run.get('run_id')==trial['run_id'] and run.get('question')==trial['question'] and run.get('config')==value['context']['config'] and run.get('root_model_requested')==variant['model'] and run.get('options',{}).get('evidence_cache_enabled')==(variant['cache']=='on') and run.get('source_sha256')==expected_hashes and run.get('routing',{}).get('policy','data_agent')==variant.get('route_policy','data_agent'))
        if not identity:errors.append('run_identity_or_frozen_config_mismatch')
        if attempt['state'] not in ('completed','recovered'):errors.append('execution_not_completed')
        if run.get('state')!='completed':errors.append('root_execution_not_completed')
        errors+=check_answer(run.get('answer'),case['expected'],check_sources=False,allow_extra_dimensions=True)
        result=attempt['state'] if attempt['state'] in ('pending','running') else outcome(run)
        if (case['answerability']=='answerable' and result!='answered') or (case['answerability']=='unobservable' and result!='abstained'):
            errors.append('question_class_outcome_mismatch')
        provenance=evidence_source(run,evidence,measurement,cache.get('estimates',{}).get(trial['run_id']))
        candidates=[x for x in reviews if x['run_id']==trial['run_id'] and x['evidence_sha256']==evidence_hash and x['plan_sha256']==sha(value)]
        manual=candidates[-1] if candidates else None
        review_scope=manual.get('reviewer_scope','legacy_unspecified') if manual else 'participant'
        if manual and review_scope!='participant':manual=None
        routing=run.get('routing',{})
        selected=routing.get('selected_route')
        total=measurement.get('reference_total_usd') if measurement and identity else None
        upper=measurement.get('reference_total_upper_usd') if measurement and identity else None
        small=measurement.get('known_subtotal_usd',0) if measurement and identity else 0
        missing_components=sum(c.get('reference_usd') is None for c in measurement['components'].values()) if measurement and identity else 3
        source_pass=provenance['status'] in ('matched_estimate','matched_alias_estimate','matched_direct','not_required_for_abstention')
        rows.append({'run_id':trial['run_id'],'trace_console_url':run.get('trace_console_url'),'case_id':trial['case_id'],'variant':trial['variant'],'variant_label':label_of(value,trial['variant']),'repetition':trial['repetition'],
            'question_class':case['question_class'],'question':trial['question'],'attempt_state':attempt['state'],
            'outcome':result,'root_status':run.get('answer',{}).get('status'),'structured_success':not errors,'errors':errors,
            'reviewer_scope':review_scope,'official_review':None,'provenance':provenance,'human_review':manual['decision'] if manual else 'pending',
            'verified_success':not errors and source_pass and bool(manual and manual['decision']=='pass'),
            'review_required':not errors,'routing':routing,'expected_routes':case['expected_routes'],
            'expected_route_match':selected in case['expected_routes'] if selected else None,
            'elapsed_seconds':attempt.get('wall_seconds'),'reference_total_usd':total,'reference_total_upper_usd':upper,'known_subtotal_usd':small,
            'missing_cost_components':missing_components,'evidence_sha256':evidence_hash,
            'cost_components':measurement.get('components') if measurement and identity else None,
            'answer':run.get('answer'),'evidence':evidence})
    timing=common_timing(rows,value['variants'])
    groups=[]
    for variant in value['variants']:
        group=[x for x in rows if x['variant']==variant['name']];n=len(group)
        finished=all(x['attempt_state'] not in ('pending','running') for x in group)
        complete=finished and all(x['reference_total_usd'] is not None for x in group)
        successes=sum(x['structured_success'] for x in group);verified=sum(x['verified_success'] for x in group)
        ready=all(not x['review_required'] or (x['human_review']!='pending' and x['provenance']['status']!='unverified') for x in group)
        total=round(sum(x['reference_total_usd'] for x in group),10) if complete else None
        # Upper bound only when every missing total is itself bounded; never a partial sum.
        bounded=finished and not complete and all(x['reference_total_usd'] is not None or x['reference_total_upper_usd'] is not None for x in group)
        upper=round(sum(x['reference_total_usd'] if x['reference_total_usd'] is not None else x['reference_total_upper_usd'] for x in group),10) if bounded else None
        durations=[x['elapsed_seconds'] for x in group if x['elapsed_seconds'] is not None]
        groups.append({'variant':variant['name'],'label':variant.get('label') or variant['name'],'route_policy':variant.get('route_policy','data_agent'),'show_route_match':variant.get('route_policy')=='router','review_scope':'participant_learning_only','planned':n,'attempted':sum(x['attempt_state']!='pending' for x in group),'finished':finished,
            'structured_successes':successes,'structured_success_rate':successes/n,'verified_successes':verified,
            'verified_success_rate':verified/n if ready and finished else None,'human_review_pending':sum(x['review_required'] and x['human_review']=='pending' for x in group),
            'reference_total_usd':total,'known_subtotal_usd':round(sum(x['known_subtotal_usd'] for x in group),10),
            'incomplete_cost_trials':sum(x['reference_total_usd'] is None for x in group),'missing_cost_components':sum(x['missing_cost_components'] for x in group),
            'reference_usd_per_structured_success':total/successes if total is not None and successes else None,
            'reference_total_upper_usd':upper,'reference_usd_per_structured_success_upper':upper/successes if upper is not None and successes else None,
            'reference_usd_per_verified_success':total/verified if total is not None and verified and ready else None,
            'median_wall_seconds':statistics.median(durations) if durations else None,
            'common_timing':timing['groups'][variant['name']] if len(value['variants'])>1 else None,
            'physical_route_calls':{route:sum(event.get('route')==route for x in group for event in x['routing'].get('events',[])) for route in sorted({event['route'] for x in group for event in x['routing'].get('events',[]) if event.get('route')})},
            'route_matches':sum(x['expected_route_match'] is True for x in group),'route_unknown':sum(x['expected_route_match'] is None for x in group),
            'coverage_by_class':{category:{'planned':sum(x['question_class']==category for x in group),'structured_successes':sum(x['question_class']==category and x['structured_success'] for x in group),'abstained':sum(x['question_class']==category and x['outcome']=='abstained' for x in group)} for category in sorted({x['question_class'] for x in group})}})
    result={'version':VERSION,'batch_id':ident,'generated_at':timestamp(),'plan_sha256':sha(value),'pricing_version':value['pricing']['version'],
            'review_policy':'participant-v1','evaluation_scope':'public_structured_contract_separate_provenance_and_human_review',
            'actual_billed_usd':None,'ranking':None,'groups':groups,'trials':rows,'timing_comparison':timing,
            'limitations':['公開問題の暫定評価。本番品質・公式順位ではない','合格率の分母は予定試行。未実行数と合わせて読む','費用は公表単価の参考換算。部分小計で順位を付けない','Data Agentの取得元は時刻等で推定、SQL ToolはジョブIDで照合。SQLの意味と回答文は人が確認する','人の確認は参加者の学習用。公式評価には主催者環境での再実行と確認が必要']}
    atomic_save(folder/'report.json',result)
    from .benchmark_report import render
    temporary=folder/'report.html.tmp';temporary.write_text(render(result),encoding='utf-8');temporary.replace(folder/'report.html')
    return result


def review(root, ident, run_id, decision, note):
    folder,value=load_plan(root,ident)
    if decision not in ('pass','fail') or not note.strip():raise ValueError('判定と確認内容を記入してください')
    result=report(root,ident)
    row=next((x for x in result['trials'] if x['run_id']==run_id),None)
    if not row or row['attempt_state'] in ('pending','running'):raise ValueError('終了した予定実行を指定してください')
    record={'run_id':run_id,'decision':decision,'note':note,'reviewed_at':timestamp(),'evidence_sha256':row['evidence_sha256'],'plan_sha256':sha(value),'source':'explicit_human_review','reviewer_scope':'participant','official_eligible':False}
    atomic_save(folder/'reviews'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'-'+uuid.uuid4().hex+'.json'),record)
    return report(root,ident)


def validated_measurements(root,value):
    results={}
    for trial in value['trials']:
        folder=root/'artifacts/lab2'/trial['run_id']
        if not (folder/'run.json').exists() or not (folder/'economics.json').exists():continue
        run=read(folder/'run.json');measurement=read(folder/'economics.json')
        evidence=[read(p) for p in sorted(folder.glob('evidence-*.json'))]
        if run.get('run_id')==trial['run_id'] and run.get('config')==value['context']['config'] and measurement.get('input_sha256')==digest(run,evidence) and measurement.get('pricing_sha256')==hashlib.sha256(json.dumps(value['pricing'],sort_keys=True).encode()).hexdigest():results[trial['run_id']]=measurement
    return results


def collect(root, ident, mode, account=None):
    folder,value=load_plan(root,ident)
    if config(require_project=True,root=root)!=value['context']['config'] or read(root/'data/pricing-2026-10-07.json')!=value['pricing']:
        raise ValueError('照合するProject設定と価格表を評価計画に揃えてください')
    ids=[t['run_id'] for t in value['trials'] if (root/'artifacts/lab2'/t['run_id']/'run.json').exists() and read(root/'artifacts/lab2'/t['run_id']/'run.json').get('finished_at')]
    if ids:collect_costs(root,value['context']['config'],ids,mode,account)
    from .provenance import capture,supplement
    current=capture(root,account) if value.get('cloud_snapshot') else {}
    measurements=validated_measurements(root,value)
    atomic_save(folder/'cache-provenance.json',{'plan_sha256':sha(value),'collected_at':timestamp(),'current_snapshot':current,'measurement_sha256':{k:sha(v) for k,v in measurements.items()},'estimates':supplement(value,measurements,current)})
    return report(root,ident)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['plan','run','collect','report','review','links','show'])
    p.add_argument('--batch');p.add_argument('--preset',choices=sorted(PRESETS));p.add_argument('--cases',nargs='+');p.add_argument('--repeats',type=int)
    p.add_argument('--cache',action='append',choices=['on','off']);p.add_argument('--model',action='append');p.add_argument('--language',choices=['ja','en'],default='ja')
    p.add_argument('--route',action='append',choices=['data_agent','verified_sql','router']);p.add_argument('--account');p.add_argument('--billing-mode',choices=['unknown','on-demand','capacity'],default='unknown')
    p.add_argument('--run-id');p.add_argument('--decision',choices=['pass','fail']);p.add_argument('--note',default='');p.add_argument('--port',type=int,default=8080)
    a=p.parse_args()
    try:
        cfg=config(require_project=True);print('対象Project: '+cfg['project_id'],flush=True)
        def new_plan():
            pairs=None
            if a.repeats is not None and not 1<=a.repeats<=10:p.error('--repeats は 1〜10 で指定してください')
            if a.preset:
                if a.cases or a.repeats is not None or a.cache or a.route:p.error('--preset と --cases・--repeats・--cache・--route は一緒に指定できません')
                preset=PRESETS[a.preset];a.cases=preset['cases'];a.repeats=preset['repeats'];pairs=preset['variants']
            from .provenance import capture
            snapshot=capture(ROOT,a.account)
            if not snapshot['complete']:raise CloudError('評価計画のクラウド確認が未完了です。Checkpointで開始状態を整えてください: '+str(snapshot['errors']))
            v=plan(ROOT,a.cases,1 if a.repeats is None else a.repeats,a.cache or ['on'],a.model or [DEFAULT_MODEL],a.language,cloud_snapshot=snapshot,routes=a.route or ['data_agent'],pairs=pairs,preset=a.preset)
            print('評価計画: '+v['batch_id']+' / '+str(len(v['trials']))+' 回の質問を予定しました。まだ質問は送っていません。')
            print('比べる構成: '+'、'.join(x['label'] for x in v['variants']),flush=True)
            return v
        if a.command=='plan':
            new_plan();return 0
        if a.preset and a.command!='run':p.error('--preset は plan と run で使います')
        if a.command=='run' and a.preset:
            if a.batch:p.error('--preset と --batch は一緒に指定できません')
            if a.cases or a.repeats is not None or a.cache or a.route:p.error('--preset と --cases・--repeats・--cache・--route は一緒に指定できません')
            try:latest=latest_plan(ROOT,cfg)
            except ValueError:latest=None
            if latest and latest.get('preset')==a.preset and unfinished(ROOT,latest):
                a.batch=latest['batch_id'];print('途中で止まった評価計画 '+a.batch+' を続けます（まだ送っていない質問から）',flush=True)
            else:
                a.batch=new_plan()['batch_id']
        if not a.batch:
            latest=latest_plan(ROOT,cfg);a.batch=latest['batch_id']
            print('評価計画 '+a.batch+' を使います（日本時間 '+jst(latest.get('created_at'))+' に作成、'+str(len(latest['trials']))+' 回の質問）',flush=True)
        if a.command=='run':
            from .checkpoint import require_ready
            require_ready(ROOT,a.account)
            execute(ROOT,a.batch,a.account);result=report(ROOT,a.batch)
            print(json.dumps(result['groups'],ensure_ascii=False,indent=2))
            print_links(ROOT,cfg,load_plan(ROOT,a.batch)[1],a.account);return 0
        elif a.command=='links':
            print_links(ROOT,cfg,load_plan(ROOT,a.batch)[1],a.account,attempts=1);return 0
        elif a.command=='show':
            from .web import main as web_main
            return web_main(['--port',str(a.port),'--benchmark',a.batch])
        elif a.command=='collect':result=collect(ROOT,a.batch,a.billing_mode,a.account)
        elif a.command=='review':result=review(ROOT,a.batch,a.run_id,a.decision,a.note)
        else:result=report(ROOT,a.batch)
        print(json.dumps(result['groups'],ensure_ascii=False,indent=2));return 0
    except (CloudError,OSError,ValueError) as exc:print(str(exc),file=sys.stderr);return 2
    except KeyboardInterrupt:print('中断記録を保存しました。同じ計画で再開できます。',file=sys.stderr);return 130

if __name__=='__main__':sys.exit(main())

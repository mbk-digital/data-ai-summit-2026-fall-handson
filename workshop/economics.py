"""Collect and reconcile #3 usage. Original Root records remain immutable."""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode
from .cloud import Client, CloudError, timestamp, validate_cloud_config
from .settings import ROOT, config

VERSION = 'economics-v1'
PRICE_PATH = ROOT/'data/pricing-2026-10-07.json'
EXTRA_JOB_REASON = '生成SQLにないジョブ候補または別名照合の競合あり'
SCOPE = ['root_model', 'data_agent', 'bigquery_query']
EXCLUDED = ['Trace・Logging', 'Catalog・データ保存', '環境準備・Agent作成', '観測データ収集', 'Runtime', '税・割引・無料枠・為替']


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    tmp.replace(path)


def epoch(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def number(value):
    if isinstance(value, bool) or value is None: raise ValueError('Missing/invalid usage')
    n = int(value)
    if n < 0 or str(n) != str(value): raise ValueError('Invalid usage')
    return n


def digest(record, evidence):
    return hashlib.sha256(json.dumps([record, evidence], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def component(source, kind, attribution, usage=None, usd=None, reasons=None):
    return {'source': source, 'usage_kind': kind, 'usage': usage,
            'attribution': attribution, 'amount_kind': 'reference_price_conversion' if usd is not None else 'unavailable',
            'reference_usd': round(usd, 10) if usd is not None else None, 'reasons': reasons or []}


def not_invoked(run, service):
    if run.get('implementation_version')!='root-v3' or run.get('state')!='completed':return False
    routing=run.get('routing',{});events=routing.get('events',[])
    sql_policy=routing.get('policy') in ('verified_sql','router') and routing.get('selected_route') in (None,'verified_sql')
    if not sql_policy:return False
    if service=='root':return routing.get('selected_route') is None and run.get('root_model_invoked') is False and not run.get('root_model_usage') and not events
    if service=='data_agent':return run.get('physical_call_attempts')==0 and not any(e.get('route')=='data_agent' for e in events)
    return routing.get('selected_route') is None and not run.get('direct_jobs') and not events


def unused_component(service):
    result=component('application execution record','not_invoked','direct_run_id',{'calls':0},0,['この経路では'+service+'を呼び出していない。システム全体が無料という意味ではない'])
    result['known_reference_usd']=0
    return result


def root_cost(run, prices):
    if not_invoked(run,'root'):return unused_component('Rootモデル')
    events = {}; reasons = []; inp = out = cached = thought = 0
    model = run.get('root_model_requested')
    rate = prices['root_models'].get(model)
    if not rate: reasons.append('モデルの単価が未登録')
    # A dated introductory price is not carried past its end; the later price is not guessed.
    through = rate.get('price_through') if rate else None
    if through and str(run.get('started_at', ''))[:10] > through: reasons.append('モデル単価の適用期限切れ（'+through+'まで）')
    for event in run.get('root_model_usage', []):
        ident = event.get('event_id')
        if not ident: reasons.append('モデルイベントIDなし'); continue
        if ident in events and events[ident] != event: reasons.append('同一イベントの使用量が矛盾')
        events[ident] = event
    if not events: reasons.append('Root使用量なし')
    try:
        for event in events.values():
            u = event['usage']
            if event.get('model_version') != model: reasons.append('実モデル版と単価の対応未確認')
            if u.get('traffic_type') != 'ON_DEMAND': reasons.append('Rootの従量課金を確認できない')
            if any(d.get('modality') != 'TEXT' for k in ('prompt_tokens_details','candidates_tokens_details') for d in u.get(k, [])):
                reasons.append('テキスト以外の入力・出力は換算対象外')
            i = number(u.get('prompt_token_count')); c = number(u.get('candidates_token_count'))
            t = number(u.get('thoughts_token_count', 0)); cache = number(u.get('cached_content_token_count', 0))
            if cache > i or i+c+t != number(u.get('total_token_count')): reasons.append('Token内訳が合計と不一致')
            inp += i; out += c+t; cached += cache; thought += t
    except (ValueError, KeyError): reasons.append('Root使用量の欠落・不正')
    if run.get('state') != 'completed': reasons.append('失敗・中断の未報告使用量は不明')
    usage = {'input_tokens': inp, 'output_tokens': out, 'reasoning_tokens_in_output': thought, 'cached_input_tokens': cached, 'events': len(events)} if events else None
    amount = ((inp-cached)*rate['input_per_million']+cached*rate['cached_input_per_million']+out*rate['output_per_million'])/1e6 if not reasons else None
    result = component('ADK event.usage_metadata', 'measured', 'direct_run_id', usage, amount, reasons)
    only_incomplete = reasons == ['失敗・中断の未報告使用量は不明']
    result['known_reference_usd'] = round(((inp-cached)*rate['input_per_million']+cached*rate['cached_input_per_million']+out*rate['output_per_million'])/1e6,10) if only_incomplete else amount
    return result


def in_run(start, end, run):
    try:
        # A small clock tolerance is part of the estimate, never a direct ID match.
        return epoch(run['started_at'])-2 <= epoch(start) and epoch(end) <= epoch(run['finished_at'])+2
    except (KeyError, ValueError, TypeError): return False


def agent_id(run):
    return (run.get('data_agent') or '').rsplit('/',1)[-1]


def trace_candidates(trace, runs):
    spans = trace.get('spans', [])
    agents = {s.get('labels', {}).get('gen_ai.agent.id', '').rsplit('/',1)[-1] for s in spans}-{''}
    if not spans or not agents: return []
    start = min(s['startTime'] for s in spans); end = max(s['endTime'] for s in spans)
    return [r['run_id'] for r in runs if agents == {agent_id(r)} and in_run(start,end,r)]


def native_cost(run, traces, runs, prices, complete):
    if not_invoked(run,'data_agent'):return unused_component('Data Agent')
    matched = []; ambiguous = []; events = {}; reasons = []
    for trace in traces:
        candidates = trace_candidates(trace, runs)
        if run['run_id'] not in candidates: continue
        if len(candidates) != 1: ambiguous.append(trace['traceId']); continue
        matched.append(trace['traceId'])
        if not any('gen_ai.usage.input_tokens' in s.get('labels', {}) for s in trace.get('spans', [])):
            reasons.append('対応Traceの一部に使用量なし')
        for span in trace.get('spans', []):
            labels = span.get('labels', {})
            if not any(k.startswith('gen_ai.usage.') for k in labels): continue
            event = labels.get('gcp.vertex.agent.event_id')
            if not event: reasons.append('標準TraceのモデルイベントIDなし'); continue
            try:
                counts = tuple(number(labels.get(k)) for k in ('gen_ai.usage.input_tokens','gen_ai.usage.output_tokens'))
                reasoning = number(labels.get('gen_ai.usage.reasoning.output_tokens', 0))
                if reasoning > counts[1]: reasons.append('標準Traceの出力Token内訳が矛盾')
                counts += (reasoning,)
            except ValueError: reasons.append('標準Traceの使用量に欠落・不正'); continue
            if event in events and events[event] != counts: reasons.append('同一イベントの使用量が矛盾')
            events[event] = counts
    expected = run.get('physical_call_attempts')
    if not complete: reasons.append('Trace一覧が不完全')
    if not events: reasons.append('対応するToken記録なし（反映待ちの可能性）')
    if ambiguous: reasons.append('同じTraceに複数の実行が該当')
    if not expected or len(matched) != expected: reasons.append('Trace数と取得試行数が不一致')
    if run.get('state') != 'completed': reasons.append('失敗・中断の未報告使用量は不明')
    usage = {'input_tokens':sum(x[0] for x in events.values()), 'output_tokens':sum(x[1] for x in events.values()),
             'reasoning_tokens_in_output':sum(x[2] for x in events.values()), 'events':len(events), 'traces':len(matched)} if events else None
    rate = prices['data_agent']
    amount = (usage['input_tokens']*rate['input_per_million']+usage['output_tokens']*rate['output_per_million'])/1e6 if not reasons else None
    result = component('Data Agent standard Cloud Trace', 'service_reported', 'ambiguous' if ambiguous else 'time_agent_estimate' if matched else 'unmatched', usage, amount, reasons)
    result['known_reference_usd'] = round((usage['input_tokens']*rate['input_per_million']+usage['output_tokens']*rate['output_per_million'])/1e6,10) if reasons == ['失敗・中断の未報告使用量は不明'] else amount
    result.update(trace_ids=matched, ambiguous_trace_ids=ambiguous,
                  billing_token_mapping='assumed_otel_output_includes_reasoning_not_invoice_verified',
                  trial_through=rate['trial_through'], price_effective_from=rate['effective_from'])
    return result


def job_candidates(job, runs):
    labels = job.get('labels') or {}
    direct=[]
    for r in runs:
        for recorded in r.get('direct_jobs',[]):
            if (r.get('routing',{}).get('selected_route')=='verified_sql' and recorded.get('job_id')==job.get('job_id')
                and job.get('project_id')==r.get('config',{}).get('project_id')
                and job.get('query')==recorded.get('query') and job.get('query_parameters')==recorded.get('query_parameters')
                and str(job.get('location','')).lower()==str(recorded.get('location','')).lower()
                and in_run(job.get('creation_time'),job.get('end_time'),r)):
                direct.append(r['run_id'])
    if direct:return sorted(set(direct))
    if labels.get('ca-bq-job') != 'true': return []
    return [r['run_id'] for r in runs if labels.get('data-agent-id') == agent_id(r)
            and in_run(job['creation_time'], job['end_time'], r)]


def sql_key(sql):
    """Compare lexical tokens, preserving literal contents and token boundaries.

    This is a conservative formatting comparison, not SQL semantic equivalence.
    Unhandled triple-quoted syntax falls back to exact text.
    """
    sql = sql.strip().rstrip(';').strip()
    if "'"*3 in sql or '"'*3 in sql: return (sql,)
    pattern = r"""'(?:\\.|''|[^'\\])*'|"(?:\\.|""|[^"\\])*"|`(?:\\.|[^`\\])*`|--[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_][A-Za-z_0-9]*|[0-9]+(?:\.[0-9]+)?|<>|!=|<=|>=|\|\||[^\s]"""
    return tuple(re.findall(pattern, sql))


def select_alias_key(sql):
    """Fold only explicit top-level SELECT-list aliases in a single SELECT.

    No CAST types, table aliases, alias references, CTEs, nested SELECTs,
    comments, quoted names, or additional statements are normalized.
    """
    tokens=sql_key(sql)
    if not tokens or tokens[0].upper()!='SELECT':return None
    upper=[t.upper() for t in tokens]
    if upper.count('SELECT')!=1 or any(t in ('WITH','UNION','INTERSECT','EXCEPT',';','|>') for t in upper):return None
    if any(t.startswith(('--','/*')) or "'"*3 in t or '"'*3 in t for t in tokens):return None
    if len(tokens)>1 and upper[1]=='AS':return None  # SELECT AS STRUCT/VALUE/type
    output=list(tokens);stack=[];in_list=True
    for i,t in enumerate(tokens):
        if t in ('(', '['):stack.append(t)
        elif t in (')', ']'):
            if not stack or stack.pop()!=('(' if t==')' else '['):return None
        elif not stack:
            if upper[i]=='FROM':in_list=False
            if in_list and upper[i]=='AS' and i>1 and tokens[i-1]!='.':
                if i+1>=len(tokens) or not re.fullmatch('[A-Za-z_][A-Za-z_0-9]*',tokens[i+1]):return None
                if i+2<len(tokens) and tokens[i+2]!=',' and upper[i+2]!='FROM':return None
                output[i+1]=tokens[i+1].lower()
    return tuple(output) if not stack else None


def compare_sql(expected,actual,allow_alias=True):
    if expected==actual:return {'method':'exact_text','changes':[]}
    left=sql_key(expected);right=sql_key(actual)
    if left==right:return {'method':'lexical','changes':[]}
    if allow_alias:
        a=select_alias_key(expected);b=select_alias_key(actual)
        if a is not None and a==b:
            return {'method':'select_alias_case_estimate','changes':[{'token_index':i,'expected':x,'actual':y} for i,(x,y) in enumerate(zip(left,right)) if x!=y]}
    return {'method':'unmatched','changes':[]}


def match_sql_queries(expected,actual,allow_alias=True):
    # Deduplicate formatting-equivalent evidence, never resolve competing aliases.
    unique={}
    for i,q in enumerate(expected):unique.setdefault(sql_key(q),i)
    matches=[];covered=set();unmatched=[]
    for j,query in enumerate(actual):
        candidates=[(i,compare_sql(expected[i],query,allow_alias)) for i in unique.values()]
        strict=[c for c in candidates if c[1]['method'] in ('exact_text','lexical')]
        chosen=strict or [c for c in candidates if c[1]['method']=='select_alias_case_estimate']
        if len(chosen)!=1:
            unmatched.append({'actual_index':j,'reason':'ambiguous_alias_candidates' if chosen else 'no_match'});continue
        i,proof=chosen[0];covered.add(i);matches.append({'actual_index':j,'expected_index':i,**proof})
    missing=sorted(set(unique.values())-covered)
    return {'version':'sql-format-and-select-alias-v1','complete':bool(expected and actual) and not unmatched and not missing,
            'matches':matches,'unmatched_actual':unmatched,'unmatched_expected':missing}


def bq_cost(run, evidence, jobs, runs, prices, mode, complete):
    if not_invoked(run,'bigquery'):return unused_component('BigQueryクエリ')
    direct=run.get('routing',{}).get('selected_route')=='verified_sql'
    if direct:
        wanted={j['job_id'] for j in run.get('direct_jobs',[])}
        complete=bool(wanted) and wanted.issubset({j.get('job_id') for j in jobs})
    matched = []; ambiguous = []; reasons = []
    expected_sql = [q for e in evidence for q in e.get('sql', [])]
    for job in jobs:
        candidates = job_candidates(job, runs)
        if run['run_id'] not in candidates: continue
        if len(candidates) != 1: ambiguous.append(job['job_id']); continue
        matched.append(job)
    if not complete: reasons.append('BigQueryジョブ一覧が不完全')
    if direct and {j['job_id'] for j in matched}!={j['job_id'] for j in run.get('direct_jobs',[])}:reasons.append('記録したSQLジョブID・パラメータ・実行の対応が不一致')
    if ambiguous: reasons.append('同じジョブに複数の実行が該当')
    if not matched: reasons.append('対応ジョブなし。未取得を0円にしない')
    sql_proof=match_sql_queries(expected_sql,[j.get('query') or '' for j in matched],allow_alias=not direct)
    if sql_proof['unmatched_actual']: reasons.append(EXTRA_JOB_REASON)
    if sql_proof['unmatched_expected']: reasons.append('生成SQLに対応するジョブが不足')
    if any(j.get('statement_type') == 'SCRIPT' or j.get('parent_job_id') for j in matched): reasons.append('SCRIPT親子の集計は未対応')
    if any(j.get('state') != 'DONE' or j.get('error_result') for j in matched): reasons.append('失敗・未完了ジョブの換算は保留')
    capacity = any(j.get('reservation_id') or j.get('edition') for j in matched)
    if mode != 'on-demand' or capacity: reasons.append('容量課金または料金方式未確認：bytesから金額換算しない')
    billed = processed = 0; valid = True
    try:
        for job in matched:
            processed += number(job.get('total_bytes_processed'))
            # The API can omit bytes for cache hits; only an explicit hit means zero.
            billed += 0 if job.get('cache_hit') is True else number(job.get('total_bytes_billed'))
    except ValueError: valid = False; reasons.append('処理量または課金対象量が未取得')
    location = run.get('config', {}).get('bigquery_location', '').upper()
    rate = prices['bigquery_on_demand_per_tib'].get(location)
    if rate is None: reasons.append('リージョンの単価未登録')
    if run.get('state') != 'completed': reasons.append('失敗・中断の未報告使用量は不明')
    result = component('BigQuery jobs.get' if direct else 'INFORMATION_SCHEMA.JOBS_BY_PROJECT', 'measured', 'ambiguous' if ambiguous else ('direct_job_id' if direct else 'time_agent_sql_estimate') if matched else 'unmatched',
        {'processed_bytes':processed, 'billed_bytes':billed, 'jobs':len(matched)} if valid and matched else None,
        billed/(1024**4)*rate if not reasons else None, reasons)
    result['known_reference_usd'] = round(billed/(1024**4)*rate,10) if reasons == ['失敗・中断の未報告使用量は不明'] else result['reference_usd']
    # Extra candidate jobs only make the attribution uncertain; summing every candidate still bounds the charge.
    result['upper_bound_usd'] = round(billed/(1024**4)*rate,10) if reasons == [EXTRA_JOB_REASON] and valid and matched else None
    result['job_evidence'] = [{k:j.get(k) for k in ('job_id','project_id','query','referenced_tables','state','error_result','cache_hit','creation_time','end_time','statement_type','parent_job_id','labels','location','query_parameters','principal_sha256')} for j in matched]
    for proof,job in zip(result['job_evidence'],matched):
        proof['principal_sha256']=hashlib.sha256(job['user_email'].encode()).hexdigest() if job.get('user_email') else job.get('principal_sha256')
    for proof in sql_proof['matches']:proof['job_id']=matched[proof['actual_index']]['job_id']
    result.update(job_ids=[j['job_id'] for j in matched], ambiguous_job_ids=ambiguous, billing_mode='capacity' if capacity else mode,sql_matching=sql_proof)
    return result


def reconcile(run, evidence, jobs, traces, runs, prices, mode, completeness):
    components = {'root_model':root_cost(run, prices),
                  'data_agent':native_cost(run, traces, runs, prices, completeness['traces']),
                  'bigquery_query':bq_cost(run,evidence,jobs,runs,prices,mode,completeness['jobs'])}
    amounts = [c['reference_usd'] for c in components.values()]
    total = round(sum(amounts),10) if all(x is not None for x in amounts) else None
    bounded = total is None and all(c['reference_usd'] is not None or c.get('upper_bound_usd') is not None for c in components.values())
    upper = round(sum(c['reference_usd'] if c['reference_usd'] is not None else c['upper_bound_usd'] for c in components.values()),10) if bounded else None
    return {'version':VERSION, 'run_id':run['run_id'], 'input_sha256':digest(run,evidence),
            'collected_at':timestamp(), 'pricing_version':prices['version'],
            'pricing_sha256':hashlib.sha256(json.dumps(prices,sort_keys=True).encode()).hexdigest(),
            'currency':'USD', 'scope':SCOPE, 'excluded':EXCLUDED,
            'status':'reference_estimate' if total is not None else 'partial',
            'reference_total_usd':total, 'reference_total_upper_usd':upper, 'known_subtotal_usd':round(sum(c.get('known_reference_usd') or 0 for c in components.values()),10),
            'actual_billed_usd':None, 'components':components,
            'assumptions':['既知の実行記録内で重複候補を検査。別端末・Consoleの未記録実行は検出を保証しない',
                           '時刻は前後2秒の許容。Traceの反映遅延・サンプリングによる欠落はあり得る',
                           'Data Agentの出力Tokenはreasoningを含むと仮定。課金Tokenとの一致は未検証',
                           '無料試用・無料枠・割引を適用しない公表単価の参考換算。請求額ではない']}


def load_runs(root, cfg):
    result = []
    for path in (root/'artifacts/lab2').glob('*/run.json'):
        r = read(path)
        if r.get('config',{}).get('project_id') == cfg['project_id'] :
            r['_folder'] = path.parent
            result.append(r)
    return result


def collect(root, cfg, ids, mode, account=None):
    all_items = load_runs(root,cfg)
    selected = [r for r in all_items if r.get('finished_at') and (not ids or r['run_id'] in ids)]
    if not selected or (ids and set(ids)-{r['run_id'] for r in selected}): raise ValueError('対象の完了・失敗記録がありません')
    start = min(epoch(r['started_at']) for r in selected)-2; end = max(epoch(r['finished_at']) for r in selected)+2
    if end-start > 86400: raise ValueError('24時間以内の実行を --run-id で指定してください')
    since = dt.datetime.fromtimestamp(start,dt.timezone.utc).isoformat(); until = dt.datetime.fromtimestamp(end,dt.timezone.utc).isoformat()
    client = Client(cfg,account); collection = root/'artifacts/lab3/collections'/uuid.uuid4().hex
    traces = []; errors = {}; complete = {'traces':False,'jobs':False}; jobs = []
    try:
        token = None
        for page in range(100):
            args = {'startTime':since,'endTime':until,'view':'COMPLETE','pageSize':1000}
            if token: args['pageToken'] = token
            payload = client.request('GET','https://cloudtrace.googleapis.com/v1/projects/'+client.project+'/traces?'+urlencode(args))
            atomic_save(collection/('traces-'+str(page)+'.json'),payload)
            traces.extend(payload.get('traces',[])); token=payload.get('nextPageToken')
            if not token: complete['traces']=True; break
    except CloudError as exc: errors['traces']=str(exc)
    try:
        # JSON serialization retains nested labels and typed booleans exactly.
        # Filter the metadata read, not the application query. It is excluded from agent costs.
        sql = """SELECT TO_JSON_STRING(t) AS payload FROM (
          SELECT job_id, project_id, user_email, creation_time, start_time, end_time, state,
            total_bytes_processed, total_bytes_billed, total_slot_ms, cache_hit,
            reservation_id, edition, statement_type, parent_job_id, error_result,
            query, referenced_tables, labels
          FROM `"""+client.project+'.region-'+client.location.lower()+"""`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
          WHERE creation_time >= @since AND creation_time <= @until
            AND EXISTS(SELECT 1 FROM UNNEST(labels) WHERE key='ca-bq-job' AND value='true')
          ORDER BY creation_time LIMIT 1001
        ) t"""
        rows = client.query(sql,[{'name':k,'parameterType':{'type':'TIMESTAMP'},'parameterValue':{'value':v}} for k,v in [('since',since),('until',until)]])
        raw = [json.loads(r['payload']) for r in rows]
        atomic_save(collection/'jobs.json',raw)
        for j in raw:
            j = dict(j); j['labels'] = {l['key']:l['value'] for l in j.get('labels',[])}; jobs.append(j)
        complete['jobs'] = len(jobs)<1001
    except CloudError as exc: errors['jobs']=str(exc)
    from .routes import job_url,job_evidence
    direct_ids=set()
    for item in selected:
        for record in item.get('direct_jobs',[]):
            try:
                value=client.request('GET',job_url(cfg,record['job_id']))
                atomic_save(collection/('direct-'+record['job_id']+'.json'),value)
                normalized=job_evidence(value)
                if normalized['job_id'] not in direct_ids:jobs.append(normalized);direct_ids.add(normalized['job_id'])
            except (CloudError,ValueError) as exc:errors[record['job_id']]=str(exc)
    prices = read(root/'data/pricing-2026-10-07.json')
    runs = [{**{k:v for k,v in r.items() if k!='_folder'}, 'finished_at':r.get('finished_at') or timestamp()} for r in all_items]
    result = []
    for item in selected:
        run = {k:v for k,v in item.items() if k!='_folder'}
        evidence = [read(p) for p in sorted(item['_folder'].glob('evidence-*.json'))]
        report = reconcile(run,evidence,jobs,traces,runs,prices,mode,complete)
        report['collection_id'] = collection.name
        atomic_save(item['_folder']/'economics.json',report); result.append(report)
    atomic_save(collection/'manifest.json',{'since':since,'until':until,'completeness':complete,'errors':errors,
        'run_ids':[r['run_id'] for r in selected], 'measurement_jobs':client.jobs, 'pricing':prices})
    return result


def export_recorded(root, cfg, ids, output):
    """Export only a numeric teaching summary, never raw records or telemetry."""
    if not ids: raise ValueError('--run-id で記録を指定してください')
    summaries = []
    for index, ident in enumerate(ids, 1):
        if not re.fullmatch('[a-f0-9]{32}', ident): raise ValueError('Invalid run ID')
        folder = root/'artifacts/lab2'/ident
        run = read(folder/'run.json'); report = read(folder/'economics.json')
        evidence = [read(p) for p in sorted(folder.glob('evidence-*.json'))]
        if run.get('config',{}).get('project_id') != cfg['project_id'] or report['input_sha256'] != digest(run,evidence):
            raise ValueError('Projectまたは元記録が照合時と異なります')
        components = {}
        for key in SCOPE:
            c = report['components'][key]
            components[key] = {k:c[k] for k in ('source','usage_kind','usage','attribution','amount_kind','reference_usd','reasons')}
        summaries.append({'record':'recorded-'+str(index), 'recorded_at':run['started_at'],
            'evidence_cache_enabled':run['options']['evidence_cache_enabled'],
            'elapsed_seconds':run['elapsed_seconds'], 'data_agent_calls':run['data_agent_calls'],
            'pricing_version':report['pricing_version'],'reference_total_usd':report['reference_total_usd'],
            'actual_billed_usd':None, 'components':components})
    # Exclusive creation protects an existing recorded example.
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:
        json.dump({'label':'保存した実行の匿名化要約。新しい推論・請求実績ではない',
                   'currency':'USD','scope':SCOPE,'excluded':EXCLUDED,'records':summaries},f,ensure_ascii=False,indent=2,allow_nan=False)
        f.write('\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['collect','export-recorded'])
    p.add_argument('--run-id',action='append',dest='ids')
    p.add_argument('--billing-mode',choices=['unknown','on-demand','capacity'],default='unknown')
    p.add_argument('--account')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/lab3-recorded.json')
    a=p.parse_args()
    try:
        cfg=config(require_project=True);validate_cloud_config(cfg)
        print('対象Project: '+cfg['project_id']+' / '+cfg['bigquery_location'],flush=True)
        if a.command == 'export-recorded':
            export_recorded(ROOT,cfg,a.ids,a.output);print('匿名化要約を保存しました: '+str(a.output));return 0
        for r in collect(ROOT,cfg,a.ids,a.billing_mode,a.account):
            print(r['run_id']+' / '+r['status']+' / USD '+str(r['reference_total_usd'])+'（公表単価による参考換算、請求額ではありません）')
        return 0
    except (CloudError,ValueError,OSError) as exc: print(str(exc),file=sys.stderr); return 2

if __name__=='__main__': sys.exit(main())

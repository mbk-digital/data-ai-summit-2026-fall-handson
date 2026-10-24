"""Small, explicit SQL capability boundary. No evaluation IDs, labels or answers."""
import datetime as dt
import hashlib
import re
import time
from urllib.parse import urlencode

from .cloud import BQ, Client, CloudError, validate_cloud_config
from .catalog import as_document, concept

POLICIES=('data_agent','verified_sql','router')
METRICS={'純売上':'net_sales_jpy','粗利益':'gross_profit_jpy','返金額':'refund_jpy','売上原価':'cogs_jpy',
         'net sales':'net_sales_jpy','gross profit':'gross_profit_jpy','refund amount':'refund_jpy','COGS':'cogs_jpy'}
MONTHS='January February March April May June July August September October November December'.split()


def parse_question(question,cfg):
    q=question.strip();metric=None;group=False;year=month=None
    m=re.fullmatch(r'(\d{4})年(\d{1,2})月の(純売上|粗利益|売上原価)はいくらですか。[\s]*(?:返品の原価回収も反映してください。)?',q)
    if m:year,month=int(m[1]),int(m[2]);metric=METRICS[m[3]]
    m=re.fullmatch(r'(\d{4})年(\d{1,2})月に計上した返金額の合計はいくらですか。',q)
    if m:year,month=int(m[1]),int(m[2]);metric='refund_jpy'
    m=re.fullmatch(r'(\d{4})年(\d{1,2})月の(純売上が最も大きい商品IDと金額|販売数量（返品控除前）が最も多い商品IDと数量)を教えてください。',q)
    if m:year,month=int(m[1]),int(m[2]);metric='net_sales_jpy' if m[3].startswith('純売上') else 'sold_quantity';group=True
    month_pattern='('+'|'.join(MONTHS)+')'
    patterns=[(r'What were net sales for '+month_pattern+r' (\d{4})\?','net_sales_jpy',False),
              (r'What was gross profit for '+month_pattern+r' (\d{4})\?','gross_profit_jpy',False),
              (r'What was the total refund amount recognized in '+month_pattern+r' (\d{4})\?','refund_jpy',False),
              (r'What was '+month_pattern+r' (\d{4}) COGS, including recovered inventory cost\?','cogs_jpy',False),
              (r'Which product had the highest net sales in '+month_pattern+r' (\d{4}), and how much\?','net_sales_jpy',True),
              (r'Which product had the largest sold quantity before returns in '+month_pattern+r' (\d{4}), and how many units\?','sold_quantity',True)]
    for pattern,key,top in patterns:
        m=re.fullmatch(pattern,q)
        if m:month=MONTHS.index(m[1])+1;year=int(m[2]);metric=key;group=top
    if metric is None:return None
    try:
        start=dt.date(year,month,1);end=dt.date(year+1,1,1) if month==12 else dt.date(year,month+1,1)
    except ValueError:return None
    period={'start':start.isoformat(),'end_exclusive':end.isoformat()}
    if period not in (cfg['current_period'],cfg['previous_period']):return None
    return {'metric':metric,'group_by':'product_id' if group else None,'period':period,
            'template':'monthly_top_product_v1' if group else 'monthly_total_v1'}


def choose(policy,question,cfg,rules,root):
    if policy not in POLICIES:raise ValueError('Unknown route policy')
    parsed=parse_question(question,cfg)
    starter=(root/'semantics/lab1-starter.txt').read_text()
    standard=starter.replace('[[返金の計上日]]','refunded_date（返金日）').replace('[[返品で戻す原価]]','recovered_cost_jpy（実際に回収できた原価）')
    supported_definition=concept(as_document(rules))[1]==concept(as_document(standard))[1]
    supported=bool(parsed and supported_definition)
    selected='data_agent' if policy=='data_agent' else 'verified_sql' if supported else 'data_agent' if policy=='router' else None
    reason='FIXED_DATA_AGENT_POLICY' if policy=='data_agent' else 'SUPPORTED_SQL_TEMPLATE' if supported else 'UNSUPPORTED_DEFINITION' if not supported_definition else 'OUTSIDE_SQL_CAPABILITY'
    return {'policy':policy,'selected_route':selected,'decision_reason':reason,'decision_source':'application_capability_rules',
            'sql_request':parsed if supported else None,'events':[],'provider_retries_observed':False,'fallback_policy':'no_retry_or_fallback_after_execution_failure'}


def query_spec(request,cfg):
    validate_cloud_config(cfg)
    if request['metric'] not in set(METRICS.values())|{'sold_quantity'} or request['group_by'] not in (None,'product_id'):raise ValueError('Unsupported SQL request')
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]',cfg['project_id']):raise ValueError('Invalid project ID')
    metric=request['metric'];group=request['group_by'];period=request['period']
    for value in period.values():dt.date.fromisoformat(value)
    sql=('SELECT '+('product_id, ' if group else '')+'COALESCE(SUM('+metric+'), 0) AS '+metric+
         ' FROM `'+cfg['project_id']+'.'+cfg['dataset_id']+'.v_revenue_events` WHERE event_date >= @start AND event_date < @end')
    if group:sql+=' GROUP BY product_id ORDER BY '+metric+' DESC, product_id ASC LIMIT 1'
    params=[{'name':k,'parameterType':{'type':'DATE'},'parameterValue':{'value':v}} for k,v in [('start',period['start']),('end',period['end_exclusive'])]]
    return {'query':sql,'useLegacySql':False,'parameterMode':'NAMED','queryParameters':params,'maximumBytesBilled':str(100*1024*1024)}


def job_url(cfg,job_id):
    if not re.fullmatch(r'ec_[a-f0-9]{32}_[1-9][0-9]*',job_id):raise ValueError('Invalid recorded SQL job ID')
    validate_cloud_config(cfg)
    return BQ+'/projects/'+cfg['project_id']+'/jobs/'+job_id+'?'+urlencode({'location':cfg['bigquery_location']})


def execute_sql(cfg,request,job_id,account=None):
    client=Client(cfg,account);spec=query_spec(request,cfg);url=job_url(cfg,job_id)
    # The caller saves the deterministic job ID before this single submission.
    body={'jobReference':{'projectId':cfg['project_id'],'jobId':job_id,'location':cfg['bigquery_location']},
          'configuration':{'query':spec,'labels':{'workshop-route':'verified_sql'}}}
    value=client.request('POST',BQ+'/projects/'+cfg['project_id']+'/jobs',body);started=time.monotonic()
    while value.get('status',{}).get('state')!='DONE':
        if time.monotonic()-started>180:raise CloudError('SQL job still running; do not resubmit: '+job_id)
        time.sleep(1);value=client.request('GET',url)
    if value.get('status',{}).get('errorResult'):raise CloudError('SQL job failed: '+str(value['status']['errorResult']))
    result=client.request('GET',BQ+'/projects/'+cfg['project_id']+'/queries/'+job_id+'?'+urlencode({'location':cfg['bigquery_location'],'maxResults':100}))
    if not result.get('jobComplete') or result.get('pageToken') or result.get('errors'):raise CloudError('SQL result incomplete')
    fields=result.get('schema',{}).get('fields',[]);rows=[]
    for row in result.get('rows',[]):
        if len(row['f'])!=len(fields):raise CloudError('SQL row shape mismatch')
        rows.append([int(cell['v']) if field['type'] in ('INTEGER','INT64') and cell['v'] is not None else cell['v'] for field,cell in zip(fields,row['f'])])
    if not rows:raise CloudError('SQL result has no evidence rows')
    return {'status':'SUCCESS','final_text':['検証済みSQLテンプレートの実行結果です。表の値・期間・対象を使って回答してください。'],
            'sql':[spec['query']],'query_parameters':spec['queryParameters'],'tables':[{'headers':[f['name'] for f in fields],'rows':rows}],
            'template':request['template'],'job_reference':value.get('jobReference'),'job_response':value,'source_route':'verified_sql'}


def job_evidence(value):
    q=value.get('statistics',{}).get('query',{});stats=value.get('statistics',{});ref=value.get('jobReference',{});spec=value.get('configuration',{}).get('query',{})
    def instant(key):
        return dt.datetime.fromtimestamp(int(stats[key])/1000,dt.timezone.utc).isoformat() if stats.get(key) is not None else None
    return {'job_id':ref.get('jobId'),'project_id':ref.get('projectId'),'location':ref.get('location'),'query':spec.get('query'),
            'query_parameters':spec.get('queryParameters',[]),'state':value.get('status',{}).get('state'),'error_result':value.get('status',{}).get('errorResult'),
            'creation_time':instant('creationTime'),'end_time':instant('endTime'),'cache_hit':q.get('cacheHit'),
            'total_bytes_processed':q.get('totalBytesProcessed',0 if q.get('cacheHit') is True else None),'total_bytes_billed':q.get('totalBytesBilled'),
            'reservation_id':stats.get('reservation_id') or stats.get('reservationUsage'),'edition':stats.get('edition'),
            'statement_type':q.get('statementType'),'parent_job_id':stats.get('parentJobId'),
            'referenced_tables':[{'project_id':r.get('projectId'),'dataset_id':r.get('datasetId'),'table_id':r.get('tableId')} for r in q.get('referencedTables',[])],
            'labels':value.get('configuration',{}).get('labels',{}),'principal_sha256':hashlib.sha256(value['user_email'].encode()).hexdigest() if value.get('user_email') else None}

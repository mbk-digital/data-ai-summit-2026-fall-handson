"""ADK Root Agent: one existing Data Agent, bounded evidence calls, real telemetry."""
import argparse
import fcntl
import asyncio
import datetime
import hashlib
import json
import sys
import time
import uuid
import re
from urllib.parse import urlencode
from typing import Literal

from .cloud import CloudError, gcloud, save, timestamp, trace_console_url, validate_cloud_config
from .settings import ROOT, config
from .data_agent import AgentClient, fingerprint
from .catalog import Catalog, as_document
from .lab1 import make_contexts
from .routes import choose, execute_sql, query_spec, POLICIES

# requirements-adk.txt に固定した直接依存。間接依存まで同一とは限らないため、
# 実行時に解決された版と uv.lock のハッシュを記録へ残す。
PINNED_DISTRIBUTIONS = ('google-adk', 'google-genai', 'google-auth', 'opentelemetry-sdk', 'opentelemetry-exporter-gcp-trace')


def environment_record():
    """実行に使われた Python と依存の実測値。古い記録へ後付けはしない。"""
    from importlib.metadata import PackageNotFoundError, version
    resolved = {}
    for name in PINNED_DISTRIBUTIONS:
        try:
            resolved[name] = version(name)
        except PackageNotFoundError:
            resolved[name] = None
    lock = ROOT/'uv.lock'
    return {'python_version': sys.version.split()[0],
            'uv_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None,
            'resolved_distributions': resolved}
from .root_evidence import RequestEvidence, compact_response, finalize, calculate_change, required_tool, review_evidence, strip_json_fence


def initialize():
    path = ROOT/'participant/lab2.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # 教材の仕込み：既定は false（同じ分析を③に 2 回頼む作り）。#2 ではこのまま動かして観測で無駄を見つけ、
        # #3 で true（1 回目の結果を再利用する版）と比べる。名前と形式は Checkpoint の診断・評価の照合が読むので変えない。
        with path.open('x', encoding='utf-8') as f:
            json.dump({'evidence_cache_enabled': False}, f, indent=2)
            f.write('\n')
    value = json.loads(path.read_text(encoding='utf-8'))
    if set(value) != {'evidence_cache_enabled'} or type(value['evidence_cache_enabled']) is not bool:
        raise ValueError('participant/lab2.json は evidence_cache_enabled: true または false を指定してください。')
    return value


async def run(args):
    # Optional dependencies are imported only for #2 execution.
    from google.auth.credentials import Credentials
    from google.genai import Client as GenAIClient, types
    from google.adk.agents import Agent
    from google.adk.models import Gemini
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.agents.run_config import RunConfig, TelemetryConfig
    from google.adk.tools.tool_context import ToolContext
    from google.adk.tools.data_agent import DataAgentToolset, DataAgentCredentialsConfig, DataAgentToolConfig
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from google.cloud.trace_v2 import TraceServiceClient
    from pydantic import BaseModel, ConfigDict, StrictInt

    cfg = config(require_project=True)
    validate_cloud_config(cfg)
    # Root モデルの呼び出し先。Gemini 3.8 Flash は us-central1 では提供されず global のみ（2026-10-07 確認）。
    # runtime_location（Agent Engine の配置）とは別に管理する。
    model_location = cfg.get('root_model_location') or cfg.get('runtime_location')
    if not model_location: raise ValueError('Rootモデルの呼び出し先として root_model_location を設定してください。')
    options = initialize()
    if getattr(args, 'cache', None) is not None:
        options = {'evidence_cache_enabled': args.cache == 'on'}
    print('対象Project: '+cfg['project_id']+' / Rootモデル: '+args.model+' / '+model_location, flush=True)
    print('教材による重複呼び出し: '+('改善済み（同一依頼内で再利用）' if options['evidence_cache_enabled'] else '有効（2つの処理が同じ分析を要求）'), flush=True)
    rules_path = ROOT/'participant/semantics.txt'
    rules = rules_path.read_text(encoding='utf-8')
    if not rules.strip() or '[[' in rules or ']]' in rules: raise ValueError('先に#1の業務定義を完成させてください。')
    run_id = getattr(args, 'run_id', None) or uuid.uuid4().hex
    if not re.fullmatch('[a-f0-9]{32}', run_id): raise ValueError('Invalid run ID')
    expected = getattr(args, 'expected_context', None)
    actual_context = fingerprint({'config':cfg, 'rules':rules})
    if expected and expected != actual_context: raise ValueError('設定または定義が変わりました。画面を更新してください。')
    folder = ROOT/'artifacts/lab2'/run_id
    if folder.exists(): raise ValueError('実行IDが既にあります。上書きしません。')
    source_hashes = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('workshop/root_agent.py', 'workshop/root_evidence.py', 'workshop/routes.py', 'agent/root-instructions.md', 'requirements-adk.txt')}
    record = {'source_sha256': source_hashes, 'environment': environment_record(), 'implementation_version': 'root-v3', 'root_model_invoked': False, 'direct_jobs': [], 'analysis_requests':0, 'analysis_reuses':0, 'physical_call_attempts':0, 'run_id': run_id, 'started_at': timestamp(), 'config': cfg, 'options': options,
              'question': args.question, 'root_model_requested': args.model, 'state': 'running',
              'root_model_usage': [], 'data_agent_usage': {'status': 'unavailable', 'value': None, 'scope': 'per_request', 'standard_trace_lookup': 'not_performed', 'native_trace_match': 'not_established'},
              'bigquery_bytes': {'status': 'unavailable', 'value': None},
              'cost': {'status': 'unavailable', 'value': None, 'pricing_version': None},
              'routing': {'policy': 'data_agent', 'selected_route': 'data_agent', 'decision_reason': 'FIXED_DATA_AGENT_POLICY', 'decision_source': 'application', 'events': [], 'provider_retries_observed': False},
              'fault_injection': 'two_consumers_request_identical_evidence', 'calculations': []}

    record['routing'] = choose(getattr(args,'route','data_agent'),args.question,cfg,rules,ROOT)

    class CloudShellCredentials(Credentials):
        # Cloud Trace はユーザー認証情報に quota project を求める。指定がないと
        # PERMISSION_DENIED（SERVICE_DISABLED）で全 Span の送信が失敗する。
        def __init__(self, quota_project):
            super().__init__()
            self._quota_project_id = quota_project
        def refresh(self, request):
            self.token = gcloud(['auth', 'print-access-token'], args.account)
            self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(minutes=40)
    credentials = CloudShellCredentials(cfg['project_id'])
    save(folder/'run.json', record)
    try: credentials.refresh(None)
    except Exception as exc:
        record.update(state='failed', error=str(exc), finished_at=timestamp())
        save(folder/'run.json', record)
        raise

    class FileExporter(SpanExporter):
        def export(self, spans):
            with (folder/'spans.jsonl').open('a', encoding='utf-8') as f:
                for span in spans: f.write(json.dumps(json.loads(span.to_json()), ensure_ascii=False)+'\n')
            return SpanExportResult.SUCCESS
        def shutdown(self): pass
    class ReportingExporter(SpanExporter):
        def __init__(self, delegate): self.delegate = delegate; self.results = []
        def export(self, spans):
            try:
                result = self.delegate.export(spans)
                self.results.append(str(result)); return result
            except Exception as exc:
                self.results.append(type(exc).__name__); return SpanExportResult.FAILURE
        def shutdown(self): self.delegate.shutdown()
    provider = TracerProvider(resource=Resource.create({'service.name': 'summit-ec-root'}))
    provider.add_span_processor(SimpleSpanProcessor(FileExporter()))
    remote = None
    if not args.local_trace_only:
        remote = ReportingExporter(CloudTraceSpanExporter(project_id=cfg['project_id'], client=TraceServiceClient(credentials=credentials)))
        provider.add_span_processor(SimpleSpanProcessor(remote))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer('workshop.root')

    class Period(BaseModel):
        start: str
        end_exclusive: str
    class Measure(BaseModel):
        name: Literal['net_sales_jpy', 'gross_profit_jpy', 'gross_profit_delta_jpy', 'refund_jpy', 'cogs_jpy', 'sold_quantity', 'gross_sales_jpy']
        value: StrictInt
        unit: Literal['JPY', 'ITEM']
    class Dimension(BaseModel):
        name: str
        value: str
    class Answer(BaseModel):
        model_config = ConfigDict(extra='forbid')
        status: Literal['OK', 'INSUFFICIENT_DATA', 'ERROR']
        reason_code: Literal['NONE', 'FUTURE_OUTCOME_NOT_OBSERVED', 'MISSING_EVIDENCE', 'EVIDENCE_RETRIEVAL_FAILED']
        period: Period
        comparison_period: Period | None = None
        measure_items: list[Measure]
        dimension_items: list[Dimension]
        customer_message_ja: str

    toolset = DataAgentToolset(tool_filter=['ask_data_agent'],
        credentials_config=DataAgentCredentialsConfig(credentials=credentials),
        data_agent_tool_config=DataAgentToolConfig(location=cfg['data_agent_location'], max_query_result_rows=100))
    native = (await toolset.get_tools())[0]
    store = None
    started = time.monotonic()
    save(folder/'run.json', record)
    try:
        with tracer.start_as_current_span('workshop.request', attributes={'workshop.run_id': run_id, 'workshop.cache_enabled': options['evidence_cache_enabled']}) as span:
            record['trace_id'] = format(span.get_span_context().trace_id, '032x')
            record['trace_console_url'] = trace_console_url(cfg['project_id'], record['trace_id'])
            with tracer.start_as_current_span('catalog.fetch_definition'):
                receipt = Catalog(cfg, args.account).sync(as_document(rules))
                record['catalog_receipt'] = receipt
                ctx = make_contexts(cfg, rules, receipt)['view']
                record['data_agent'] = AgentClient(cfg, args.account).ensure('view', ctx)
                record['configuration_hash'] = fingerprint({'context': ctx, 'root_model': args.model, 'options': options, 'route_policy': record['routing']['policy'], 'selected_route':record['routing']['selected_route'], 'source_sha256': source_hashes})
                save(folder/'run.json', record)
            if record['routing']['selected_route'] is None:
                record['answer']={'status':'UNSUPPORTED','reason_code':record['routing']['decision_reason'],
                    'period':cfg['current_period'],'comparison_period':None,'definition_version':cfg['definition_version'],
                    'measures':{},'dimensions':{},'data_sources':[],
                    'customer_message_ja':'このSQL経路の対応範囲外です。振り分け構成またはData Agent構成で比較してください。'}
                record['state']='completed'
                return 0
            current_tool_context = None
            async def fetch(query, consumer):
                if record['routing']['selected_route']=='verified_sql':
                    number=len(record['direct_jobs'])+1;job_id='ec_'+run_id+'_'+str(number)
                    spec=query_spec(record['routing']['sql_request'],cfg)
                    record['direct_jobs'].append({'job_id':job_id,'query':spec['query'],'query_parameters':spec['queryParameters'],'location':cfg['bigquery_location']})
                    route_event={'route':'verified_sql','job_id':job_id,'consumer':consumer,'reason':'SUPPORTED_SQL_TEMPLATE','started_at':timestamp(),'state':'running'}
                    record['routing']['events'].append(route_event);record['phase']='verified_sql'
                    save(folder/'run.json',record)
                    with tracer.start_as_current_span('verified_sql.execute',attributes={'workshop.job_id':job_id,'workshop.consumer':consumer}):
                        try:
                            compact=await asyncio.to_thread(execute_sql,cfg,record['routing']['sql_request'],job_id,args.account)
                            save(folder/('evidence-'+str(number)+'.json'),compact);route_event['state']='completed'
                            return compact
                        except Exception:
                            route_event['state']='failed';raise
                        finally:
                            route_event['finished_at']=timestamp();save(folder/'run.json',record)
                record['physical_call_attempts'] += 1
                number = record['physical_call_attempts']
                record['phase'] = 'data_agent'
                save(folder/'run.json', record)
                print('Data Agent 呼び出し '+str(number)+'（'+consumer+'）…', flush=True)
                with tracer.start_as_current_span('data_agent.ask_data_agent', attributes={'workshop.consumer': consumer, 'workshop.call_number': number}) as call_span:
                    route_event = {'route': 'data_agent', 'call_number': number, 'consumer': consumer,
                                   'reason': 'EVIDENCE_REQUEST', 'started_at': timestamp(), 'state': 'running'}
                    record['routing']['events'].append(route_event)
                    save(folder/'run.json', record)
                    try:
                        raw = await native.run_async(args={'data_agent_name': record['data_agent'], 'query': query}, tool_context=current_tool_context)
                        compact = compact_response(raw)
                        route_event['state'] = 'completed' if compact['status'] == 'SUCCESS' else 'failed'
                    except Exception:
                        route_event['state'] = 'failed'
                        raise
                    finally:
                        route_event['finished_at'] = timestamp()
                        save(folder/'run.json', record)
                    if compact['status'] != 'SUCCESS': call_span.set_status(trace.Status(trace.StatusCode.ERROR, 'Data Agent retrieval failed'))
                    save(folder/('evidence-'+str(number)+'.json'), compact)
                    return compact
            def reviewer(value):
                with tracer.start_as_current_span('evidence.review_table') as review_span:
                    result = review_evidence(value, cfg)
                    review_span.set_attribute('workshop.review.table_count', len(value.get('tables', [])))
                    review_span.set_attribute('workshop.review.failed_checks', sum(not c['passed'] for c in result['checks']))
                    return result
            # 教材の仕込み：回答を作る処理（answer）と根拠を確かめる処理（evidence_review）が、同じ質問で③に頼む。
            # evidence_cache_enabled が false なら 2 回とも③へ送り、true なら 2 回目は 1 回目の結果を使う（RequestEvidence.both）。
            # false のとき 1 回目（answer）の結果は使われず、2 回目だけが構造検査されて回答に使われる。1 回目と 2 回目は突き合わせない。
            store = RequestEvidence(options['evidence_cache_enabled'], fetch, reviewer)
            async def compute_change(table_index: int, row_index: int, previous_column: str, current_column: str) -> dict:
                """Calculate current minus previous from the LAST returned table. Indices start at zero. Use for every delta measure; never supply invented numeric operands."""
                with tracer.start_as_current_span('evidence.calculate_change'):
                    try:
                        value = calculate_change(store.results[-1:], table_index, row_index, previous_column, current_column)
                        record['calculations'].append(value)
                        return value
                    except (IndexError, KeyError, ValueError, ArithmeticError) as exc:
                        return {'status': 'ERROR', 'error': str(exc)}
            analysis_requests = 0
            async def analyze_ec(tool_context: ToolContext) -> dict:
                """Retrieve analysis from the application-selected EC route (Data Agent or verified SQL). Call exactly once. The application forwards the original user question unchanged and adds the table formatting instruction. Returns final answer, SQL and tables. Two fixed consumers model the workshop duplicate-call fault."""
                nonlocal current_tool_context, analysis_requests
                analysis_requests += 1
                record['analysis_requests'] = analysis_requests
                span.set_attribute('workshop.analysis_requests', analysis_requests)
                trace.get_current_span().set_attribute('workshop.reused_analysis', analysis_requests > 1)
                if analysis_requests > 1:
                    record['analysis_reuses'] += 1
                    span.set_attribute('workshop.analysis_reuses', record['analysis_reuses'])
                    return dict(store.results[-1], retrieval_note='Already retrieved. Use this evidence; do not call analyze_ec again.')
                current_tool_context = tool_context
                query = args.question + '\n月ごとの比較を求めている場合は、カテゴリ等のグループごとに前月と当月を別々の列（previous_gross_profit_jpy、current_gross_profit_jpy）で取得し、期間をまとめて合算しないでください。比較でない質問にはこの追加指定は不要です。'
                record['data_agent_query'] = query
                with tracer.start_as_current_span('evidence.two_consumers') as s:
                    result = await store.both(query)
                    s.set_attribute('workshop.physical_calls', len(store.results))
                    s.set_attribute('workshop.cache_hits', sum(e['cache_hit'] for e in store.events))
                    record['evidence_reviews'] = store.reviews
                    record['phase'] = 'root_answer'
                    save(folder/'run.json', record)
                    return result
            # Runtime type annotations must be concrete for ADK's tool schema builder.
            analyze_ec.__annotations__['tool_context'] = ToolContext
            instruction = (ROOT/'agent/root-instructions.md').read_text(encoding='utf-8')
            instruction += '\nThe application selected route: '+str(record['routing']['selected_route'])+'. analyze_ec returns evidence from that route; do not change routes or invent a fallback.\n'
            instruction += '\nConfigured periods and definitions (not answers):\n'+json.dumps({k:cfg[k] for k in ['as_of_date','current_period','previous_period','definition_version']})+'\n'+receipt['body']
            model_client = GenAIClient(vertexai=True, project=cfg['project_id'], location=model_location, credentials=credentials, http_options=types.HttpOptions(timeout=120000, retry_options=types.HttpRetryOptions(attempts=2)))
            def require_initial_evidence(callback_context, llm_request):
                # A model must not bypass retrieval then self-certify insufficient data.
                required = required_tool(store.results, record['calculations'])
                mode = 'ANY' if required else 'AUTO'
                allowed = [required] if required else None
                llm_request.config.tool_config = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=mode, allowed_function_names=allowed))
            agent = Agent(name='ec_root', model=Gemini(model=args.model, client=model_client), instruction=instruction,
                          tools=[analyze_ec, compute_change], output_schema=Answer, before_model_callback=require_initial_evidence,
                          generate_content_config=types.GenerateContentConfig(temperature=0))
            sessions = InMemorySessionService()
            session = await sessions.create_session(app_name='ec_workshop', user_id='participant')
            runner = Runner(app_name='ec_workshop', agent=agent, session_service=sessions)
            final_text = None
            record['root_model_invoked']=True;save(folder/'run.json',record)
            async for event in runner.run_async(user_id='participant', session_id=session.id,
                    new_message=types.Content(role='user', parts=[types.Part(text=args.question)]), run_config=RunConfig(max_llm_calls=5, telemetry=TelemetryConfig(capture_message_content='NO_CONTENT'))):
                if event.usage_metadata is not None:
                    record['root_model_usage'].append({'event_id': event.id, 'model_version': event.model_version,
                        'status': 'measured', 'source': 'ADK event.usage_metadata', 'usage': event.usage_metadata.model_dump(mode='json', exclude_none=True)})
                if event.is_final_response() and event.content:
                    final_text = ''.join(p.text or '' for p in event.content.parts or [] if not p.thought)
            with tracer.start_as_current_span('evidence.validate_numeric_output'):
                answer = Answer.model_validate_json(strip_json_fence(final_text or '{}')).model_dump(mode='json')
                record['answer'] = finalize(answer, store.results[-1:], cfg, record['calculations'])
            record['state'] = 'completed'
            if record['answer']['status'] == 'ERROR': span.set_status(trace.Status(trace.StatusCode.ERROR, record['answer']['reason_code']))
            print(json.dumps(record['answer'], ensure_ascii=False, indent=2), flush=True)
    except Exception as exc:
        record.update(state='failed', error=type(exc).__name__+': '+str(exc))
        print(record['error'], file=sys.stderr)
    finally:
        await toolset.close()
        record['elapsed_seconds'] = round(time.monotonic()-started, 3)
        record['finished_at'] = timestamp()
        record['data_agent_calls'] = len(store.results) if store and record['routing']['selected_route']=='data_agent' else 0
        record['sql_calls'] = len(record['direct_jobs'])
        record['cache_events'] = store.events if store else []
        record['evidence_reviews'] = store.reviews if store else []
        provider.force_flush()
        record['trace_export'] = {'local': 'spans.jsonl', 'cloud_export_results': remote.results if remote else [],
                                  'backend_read_verified': False}
        save(folder/'run.json', record)
        provider.shutdown()
        if record.get('trace_console_url'): print('Traceを開く: '+record['trace_console_url'], flush=True)
        print('実行記録: artifacts/lab2/'+run_id+'/run.json', flush=True)
        print('Data Agent '+str(record['data_agent_calls'])+'回 / '+str(record['elapsed_seconds'])+'秒 / 金額は未算出', flush=True)
    return 0 if record['state']=='completed' and record['answer']['status']!='ERROR' else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--question', default='2026年7月から8月にかけて、粗利益の減少額が最も大きいカテゴリと、その減少額を教えてください。')
    p.add_argument('--route',choices=POLICIES,default='data_agent',help='今回だけ処理経路を指定。routerは対応範囲を判定')
    p.add_argument('--model', default='gemini-3.8-flash')
    p.add_argument('--account')
    p.add_argument('--cache', choices=['on','off'], help='今回だけ再利用を切り替える。参加者ファイルは保持する')
    p.add_argument('--run-id', help=argparse.SUPPRESS)
    p.add_argument('--expected-context', help=argparse.SUPPRESS)
    p.add_argument('--local-trace-only', action='store_true', help='Cloud Traceには送らずローカル記録のみ')
    args = p.parse_args()
    try:
        lock_path = ROOT/'artifacts/.root-execution.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return asyncio.run(run(args))
    except (ImportError, CloudError, ValueError, OSError) as exc:
        print(str(exc)+'\n#2の追加セットアップと設定を確認してください。', file=sys.stderr); return 2


if __name__ == '__main__': sys.exit(main())

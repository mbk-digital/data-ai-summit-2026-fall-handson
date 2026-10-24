"""Guarded Data Agent standard observability setup, collection and removal."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import sys
from urllib.parse import urlencode
from .resource_state import compare as compare_resource, equivalent
from .cloud import Client, CloudError, save, timestamp
from .settings import ROOT, config

BASE = 'https://cloudaicompanion.googleapis.com/v1/'


def settings_enabled(setting):
    """metrics と traces が両方有効か。設定が存在するだけでは有効とみなさない。"""
    value = (setting or {}).get('conversationalAnalyticsSetting', {})
    return value.get('metricsEnabled') is True and value.get('tracesEnabled') is True


def binding_matches(binding, targets):
    """対象Projectへのbindingとproductが期待どおりか。"""
    return bool(binding and binding.get('target') in targets and binding.get('product') == 'GEMINI_IN_BIGQUERY')


@contextlib.contextmanager
def exclusive_lock():
    """観測設定の同時変更を防ぐ。main()に直接書いていたものを共有するため切り出した。"""
    lock = ROOT/'artifacts/observability/.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def summarize_traces(payload):
    """Use event IDs to avoid counting nested call_llm/generate_content twice."""
    reports = []
    for t in payload.get('traces', []):
        agents = sorted({s.get('labels', {}).get('gen_ai.agent.id') for s in t.get('spans', []) if s.get('labels', {}).get('gen_ai.agent.id')})
        if not agents: continue  # ADK Root is reported separately.
        events = {}
        conflicts = []
        unattributed = 0
        for span in t.get('spans', []):
            labels = span.get('labels', {})
            if 'gen_ai.usage.input_tokens' not in labels: continue
            event = labels.get('gcp.vertex.agent.event_id')
            if not event:
                unattributed += 1
                continue
            counts = {k: int(labels[k]) if k in labels else None for k in (
                'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'gen_ai.usage.reasoning.output_tokens')}
            if event in events and events[event] != counts: conflicts.append(event)
            events[event] = counts
        totals = {key: sum(e[key] for e in events.values()) if events and all(e[key] is not None for e in events.values()) and not conflicts else None
                  for key in ('gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'gen_ai.usage.reasoning.output_tokens')}
        reports.append({'trace_id':t['traceId'], 'agent_names':agents, 'model_events':len(events),
                        'tokens':totals, 'conflicting_event_ids':conflicts, 'unattributed_spans':unattributed,
                        'source':'Data Agent standard Cloud Trace', 'kind':'service_reported_usage',
                        'billing_actual':False, 'root_request_match':'not_established'})
    return reports


class Observability:
    def __init__(self, cfg, account=None):
        self.c = Client(cfg, account)
        self.parent = 'projects/' + self.c.project + '/locations/global'
        self.collection = self.parent + '/gibqObservabilitySettings'
        self.ident = 'ec-workshop-' + hashlib.sha256(cfg['dataset_id'].encode()).hexdigest()[:12]
        self.name = self.collection + '/' + self.ident
        self.folder = ROOT/'artifacts/observability'/self.c.project
        self.path = self.folder/'resources.json'
        self.ledger = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, name):
        return self.c.request('GET', BASE + name, missing_ok=True)

    def status(self):
        result = self.c.request('GET', BASE + self.collection)
        save(self.folder/'settings-status.json', result)
        return result

    def ensure(self, name, collection, query, payload):
        value = self.get(name)
        record = self.ledger.get(name, {})
        if value is not None:
            if record.get('state') != 'present' or not equivalent(record.get('snapshot'), value):
                raise CloudError('観測設定が台帳と異なります。上書きしません: ' + name)
            return value
        if record and record.get('state') != 'deleted':
            raise CloudError('観測設定の作成が保留中です。クラウドと台帳を確認してください。')
        self.ledger[name] = {'state':'requested','payload':payload}
        save(self.path,self.ledger)
        response = self.c.request('POST', BASE + collection + '?' + urlencode(query), payload)
        self.ledger[name]['response'] = response
        save(self.path,self.ledger)
        value = self.get(name)
        if value is None: raise CloudError('作成結果を確認できません。')
        self.ledger[name].update(state='present',snapshot=value)
        save(self.path,self.ledger)
        return value

    def expected_targets(self):
        project = self.c.request('GET', 'https://cloudresourcemanager.googleapis.com/v1/projects/' + self.c.project) or {}
        return {'projects/' + self.c.project, 'projects/' + str(project.get('projectNumber', ''))}

    def verify(self):
        """有効フラグとbindingを読み取りだけで確認する。リソースは変更しない。"""
        setting = self.get(self.name)
        binding = self.get(self.name + '/settingBindings/workshop-project') if setting else None
        enabled = settings_enabled(setting)
        bound = binding_matches(binding, self.expected_targets()) if setting else False
        return {'setting_enabled': enabled, 'binding_ready': bound, 'ready': enabled and bound}

    def enable(self, quiet=False):
        existing = self.status()
        if existing.get('nextPageToken') or existing.get('unreachable'):
            raise CloudError('設定一覧が不完全です。変更しません。')
        if any(s['name'].split('/')[-1] != self.ident for s in existing.get('gibqObservabilitySettings', [])):
            raise CloudError('他の観測設定があります。既存の適用範囲を確認してから統合してください。')
        self.ensure(self.name,self.collection,{'gibq_observability_setting_id':self.ident},
                    {'conversational_analytics_setting':{'metrics_enabled':True,'traces_enabled':True}})
        collection = self.name + '/settingBindings'
        self.ensure(collection+'/workshop-project',collection,{'setting_binding_id':'workshop-project'},
                    {'target':'projects/'+self.c.project,'product':'GEMINI_IN_BIGQUERY'})
        if quiet:
            return
        print('標準Observabilityを有効化しました。過去の実行には遡及しません。')
        print('次はBigQuery AgentsのMonitoringをConsoleで確認します:')
        print('https://console.cloud.google.com/bigquery/agents_hub;agentsHubTab=Monitoring;project='+self.c.project)
        print('まだ質問していないため、メトリクスやTraceが空でも正常です。')

    def cleanup(self, execute=False):
        items=[]
        for name,r in self.ledger.items():
            if r.get('state')=='deleted':continue
            if not (name==self.name or name==self.name+'/settingBindings/workshop-project'):
                raise CloudError('観測台帳の対象が一致しません。')
            actual=self.get(name)
            if actual is not None and (actual!=r.get('snapshot') or r.get('state') not in {'present','delete_requested'}):
                raise CloudError('変更済み観測設定は削除しません。')
            if actual is None and r.get('state') not in {'present','delete_requested'}:
                raise CloudError('作成未確認の観測設定です。')
            items.append((name,actual))
        if any(name==self.name and value is not None for name,value in items):
            links=self.c.request('GET',BASE+self.name+'/settingBindings')
            if links.get('nextPageToken') or links.get('unreachable') or any(b['name'].split('/')[-1]!='workshop-project' for b in links.get('settingBindings',[])):
                raise CloudError('教材外の観測bindingがあります。削除しません。')
        for name,value in sorted(items,key=lambda x:0 if '/settingBindings/' in x[0] else 1):
            print(('削除: ' if execute else '削除候補: ')+name)
            if execute:
                if self.get(name)!=value:raise CloudError('観測設定が削除直前に変わりました。')
                self.ledger[name]['state']='delete_requested';save(self.path,self.ledger)
                if value is not None:self.c.request('DELETE',BASE+name)
                self.ledger[name]['state']='deleted';save(self.path,self.ledger)
        return [name for name,_ in items]

    def collect(self, since):
        # Save raw telemetry separately; no token or cost is inferred from missing data.
        end=timestamp();result={'since':since,'until':end,'sources':{}}
        endpoints={
          'traces':'https://cloudtrace.googleapis.com/v1/projects/'+self.c.project+'/traces?'+urlencode({'startTime':since,'endTime':end,'view':'COMPLETE','pageSize':1000}),
          'metric_descriptors':'https://monitoring.googleapis.com/v3/projects/'+self.c.project+'/metricDescriptors?'+urlencode({'filter':'metric.type = starts_with("geminidataanalytics.googleapis.com/")','pageSize':1000})}
        for key,url in endpoints.items():
            try: result['sources'][key]=self.c.request('GET',url)
            except CloudError as e:result['sources'][key]={'error':str(e)}
        result['sources']['time_series'] = {}
        for descriptor in result['sources'].get('metric_descriptors', {}).get('metricDescriptors', []):
            metric = descriptor['type']
            if '/chat/' not in metric: continue
            url = 'https://monitoring.googleapis.com/v3/projects/'+self.c.project+'/timeSeries?'+urlencode({'filter':'metric.type="'+metric+'"', 'interval.startTime':since, 'interval.endTime':end, 'view':'FULL'})
            try: result['sources']['time_series'][metric] = self.c.request('GET', url)
            except CloudError as e: result['sources']['time_series'][metric] = {'error':str(e)}
        try:
            result['sources']['logs']=self.c.request('POST','https://logging.googleapis.com/v2/entries:list',
                {'resourceNames':['projects/'+self.c.project],'filter':'timestamp >= "'+since+'" AND (protoPayload.serviceName="geminidataanalytics.googleapis.com" OR protoPayload.serviceName="cloudaicompanion.googleapis.com")','pageSize':1000,'orderBy':'timestamp desc'})
        except CloudError as e:result['sources']['logs']={'error':str(e)}
        result['native_trace_summary'] = summarize_traces(result['sources'].get('traces', {}))
        save(self.folder/('telemetry-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'.json'),result)
        print(json.dumps({'native_traces':result['native_trace_summary'], 'available_sources':{k:list(v) for k,v in result['sources'].items()}},ensure_ascii=False,indent=2))
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['status','enable','collect','cleanup']);p.add_argument('--account');p.add_argument('--since');p.add_argument('--execute',action='store_true');args=p.parse_args()
    if args.execute and args.command!='cleanup':p.error('--execute is only for cleanup')
    cfg=config(require_project=True);print('対象Project: '+cfg['project_id'])
    try:
        with exclusive_lock():
            o=Observability(cfg,args.account)
            if args.command=='collect':
                if not args.since:p.error('--since RFC3339 is required')
                datetime.datetime.fromisoformat(args.since.replace('Z','+00:00'))
                o.collect(args.since)
            elif args.command=='cleanup':o.cleanup(args.execute)
            elif args.command=='status':print(o.status())
            else:o.enable()
        return 0
    except (CloudError,ValueError,OSError) as e:print(str(e),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())

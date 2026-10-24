"""Developer PoC: isolated Catalog resources and one graph. Not the lab1 CLI."""
import argparse
import fcntl
import hashlib
import json
import re
import time
import urllib.parse
from .resource_state import compare as compare_resource, equivalent
from .cloud import Client, CloudError, LABELS, save, timestamp, validate_cloud_config
from .settings import ROOT, config
from .data_agent import AgentClient, context
from .reference import cases

API = 'https://dataplex.googleapis.com/v1/'
OVERVIEW = 'dataplex-types.global.overview'


class Poc:
    def __init__(self, cfg, account=None, run_id='initial'):
        validate_cloud_config(cfg)
        if not re.fullmatch(r'[a-z0-9-]{1,40}', run_id):
            raise ValueError('Invalid run ID')
        self.run_id = run_id
        self.c = Client(cfg, account)
        self.cfg, self.account = cfg, account
        self.parent = 'projects/%s/locations/us' % self.c.project
        self.folder = ROOT / 'artifacts/semantics-poc' / self.c.project / cfg['dataset_id']
        self.ledger_path = self.folder / 'resources.json'
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}

    def persist(self):
        save(self.ledger_path, self.ledger)

    def wait(self, response):
        deadline = time.monotonic() + 120
        while '/operations/' in response.get('name', ''):
            if response.get('done'):
                if response.get('error'):
                    raise CloudError(json.dumps(response['error']))
                return response.get('response', {})
            if time.monotonic() > deadline:
                raise CloudError('Operation pending: ' + response['name'])
            time.sleep(2)
            response = self.c.request('GET', API + response['name'])
        return response

    def get(self, name):
        return self.c.request('GET', API + name + ('?view=ALL' if '/entries/' in name else ''), missing_ok=True)

    def ensure(self, collection, ident, payload, id_key):
        name = collection + '/' + ident
        actual = self.get(name)
        if actual is not None:
            if self.ledger.get(name, {}).get('state') != 'present' or not equivalent(self.ledger.get(name, {}).get('snapshot'), actual):
                raise CloudError('Unrecorded or changed resource: ' + name)
            if self.ledger[name]['payload'] != payload:
                raise CloudError('Definition changed; do not silently overwrite: ' + name)
            return actual
        if self.ledger.get(name, {}).get('state') == 'deleted':
            save(self.folder / ('deleted-resource-' + str(time.time_ns()) + '.json'), self.ledger[name])
            del self.ledger[name]
        if name in self.ledger:
            raise CloudError('Recorded resource absent; inspect interrupted operation: ' + name)
        self.ledger[name] = {'state': 'requested', 'payload': payload, 'requested_at': timestamp()}
        self.persist()
        response = self.c.request('POST', API + collection + '?' + urllib.parse.urlencode({id_key: ident}), payload)
        self.ledger[name]['operation_or_response'] = response
        self.persist()
        self.wait(response)
        actual = self.get(name)
        if actual is None:
            raise CloudError('Create not visible: ' + name)
        self.ledger[name].update(state='present', snapshot=actual)
        self.persist()
        return actual

    def catalog(self):
        text = (ROOT / 'semantics/okf/net-revenue.md').read_text(encoding='utf-8')
        suffix = hashlib.sha256((self.cfg['dataset_id'] + text).encode()).hexdigest()[:12]
        ident = 'ec-okf-poc-' + suffix
        metadata = {'displayName': 'EC OKF connection PoC', 'description': 'workshop_cli:summit_ec_v1:' + suffix, 'labels': LABELS}
        group = self.ensure(self.parent + '/entryGroups', ident, metadata, 'entryGroupId')
        kind = self.ensure(self.parent + '/entryTypes', ident, metadata, 'entryTypeId')
        payload = {'entryType': kind['name'], 'entrySource': {'displayName': '純売上', 'description': 'Synthetic EC metric; OKF v0.2 minimal concept'},
                   'aspects': {OVERVIEW: {'data': {'content': text, 'contentType': 'MARKDOWN'}}}}
        # Use caller project ID consistently for inventory keys.
        entry = self.ensure(self.parent + '/entryGroups/' + ident + '/entries', 'net-revenue', payload, 'entryId')
        lookup = self.c.request('POST', API + self.parent + ':lookupContext', {'resources': [entry['name']], 'options': {'format': 'json'}})
        official = self.c.request('GET', API + 'projects/dataplex-types/locations/global/aspectTypes/overview')
        candidates = []
        for aspect in entry.get('aspects', {}).values():
            if aspect.get('aspectType', '').endswith('/locations/global/aspectTypes/overview'):
                actual_type = self.c.request('GET', API + aspect['aspectType'])
                if actual_type.get('uid') == official.get('uid'):
                    candidates.append(aspect.get('data', {}).get('content'))
        fetched = candidates[0] if len(candidates) == 1 else None
        if fetched != text:
            raise CloudError('Catalog body differs from uploaded definition')
        receipt = {'entry': entry['name'], 'update_time': entry.get('updateTime'), 'retrieved_at': timestamp(),
                   'sha256': hashlib.sha256(fetched.encode()).hexdigest(), 'lookup_context': lookup,
                   'entry_response': entry, 'route': 'entries.get -> glossaryTerms; explicit application retrieval'}
        path = self.folder / ('catalog-' + suffix + '-' + self.run_id + '.json')
        agent = AgentClient(self.cfg, self.account)
        ctx = context(self.cfg, 'raw')
        ctx['glossaryTerms'] = [{'displayName': '純売上', 'description': fetched}]
        name = agent.ensure('defined', ctx)
        receipt['agent'] = name
        receipt['sent_context'] = ctx
        output = self.folder / ('q01-' + suffix + '-' + self.run_id + '.json')
        if output.exists():
            print('Retained existing question attempt: ' + output.name)
        else:
            # Preserve the retrieval receipt that actually preceded this request.
            if path.exists():
                raise CloudError('Receipt already exists without a question; inspect before retry')
            save(path, receipt)
            question = next(c['question_ja'] for c in cases() if c['case_id'] == 'Q01')
            result = agent.chat(name, question, output)
            print('Catalog Q01: ' + result['state'])
        print('Catalog receipt: ' + str(path))

    def graph_rows(self):
        return self.c.query('SELECT * FROM `%s.%s.INFORMATION_SCHEMA.PROPERTY_GRAPHS` WHERE property_graph_name = \'ec_semantics_poc\'' % (self.c.project, self.cfg['dataset_id']))

    def graph(self):
        key = 'graph:' + self.c.project + '.' + self.cfg['dataset_id'] + '.ec_semantics_poc'
        rows = self.graph_rows()
        if rows:
            if self.ledger.get(key, {}).get('snapshot') != rows:
                raise CloudError('Unrecorded or changed graph')
        else:
            if self.ledger.get(key, {}).get('state') == 'deleted':
                save(self.folder / ('deleted-graph-' + str(time.time_ns()) + '.json'), self.ledger[key])
                del self.ledger[key]
            if key in self.ledger:
                raise CloudError('Graph creation previously attempted; inspect evidence before retry')
            sql = (ROOT / 'semantics/graph/create.sql').read_text().replace('{{PROJECT_ID}}', self.c.project).replace('{{DATASET_ID}}', self.cfg['dataset_id'])
            self.ledger[key] = {'state': 'requested', 'sql': sql, 'requested_at': timestamp()}
            self.persist()
            try:
                self.c.query(sql)
            finally:
                self.ledger[key]['jobs'] = self.c.jobs
                self.persist()
            rows = self.graph_rows()
            if not rows:
                raise CloudError('Graph not visible after creation')
            self.ledger[key].update(state='present', snapshot=rows)
            self.persist()
        return self.inspect_graph('inspect-expand.sql')

    def inspect_graph(self, filename='multiple-refunds.sql'):
        rows = self.graph_rows()
        key = 'graph:' + self.c.project + '.' + self.cfg['dataset_id'] + '.ec_semantics_poc'
        record = self.ledger.get(key, {})
        if not rows:
            raise CloudError('Graphがありません。先に uv run --locked python3 -m workshop.semantics_poc graph で準備してください。')
        if record.get('state') != 'present' or record.get('snapshot') != rows:
            raise CloudError('台帳外または変更されたGraphです。確認してから再実行してください。')
        if filename not in {'multiple-refunds.sql', 'inspect-expand.sql'}:
            raise ValueError('Unsupported inspection query')
        sql = (ROOT / 'semantics/graph' / filename).read_text().replace('{{PROJECT_ID}}', self.c.project).replace('{{DATASET_ID}}', self.cfg['dataset_id'])
        original = self.c.request
        def fresh(method, url, payload=None, **kwargs):
            if payload and 'query' in payload:
                payload = dict(payload, useQueryCache=False)
            return original(method, url, payload, **kwargs)
        self.c.request = fresh
        try:
            result = self.c.query(sql)
            save(self.folder / ('graph-inspect.json' if filename == 'multiple-refunds.sql' else 'graph-result.json'),
                 {'rows': result, 'jobs': self.c.jobs, 'sql': sql, 'checked_at': timestamp()})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print('関係の確認結果です。返金のない明細は含まれず、全体の売上集計には使いません。')
            return result
        finally:
            self.c.request = original

    def cleanup_plan(self):
        # Read-only plan. No delete action until dependency and freshness checks are reviewed.
        plan = []
        for name, record in self.ledger.items():
            actual = self.graph_rows() if name.startswith('graph:') else self.get(name)
            plan.append({'name': name, 'unchanged': bool(record.get('snapshot')) and actual == record['snapshot'], 'state': record['state']})
        save(self.folder / 'cleanup-plan.json', plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2))

    def cleanup(self, execute=False):
        from .catalog import cleanup_catalog
        cleanup_catalog(self, False)
        for key, record in list(self.ledger.items()):
            if not key.startswith('graph:') or record.get('state') == 'deleted':
                continue
            expected_key = 'graph:' + self.c.project + '.' + self.cfg['dataset_id'] + '.ec_semantics_poc'
            if key != expected_key:
                raise CloudError('Invalid graph inventory')
            rows = self.graph_rows()
            if not rows:
                if record.get('state') not in {'present', 'delete_requested'}:
                    raise CloudError('Unconfirmed graph creation')
            elif rows != record.get('snapshot') or record.get('state') not in {'present', 'delete_requested'}:
                raise CloudError('Changed graph; no deletion')
            print(('削除: ' if execute else '削除候補: ') + key, flush=True)
            if execute:
                if rows and self.graph_rows() != rows:
                    raise CloudError('Graph changed before deletion')
                record['state'] = 'delete_requested'
                self.persist()
                if rows:
                    self.c.query('DROP PROPERTY GRAPH `' + key[6:] + '`')
                record['state'] = 'deleted'
                self.persist()
        cleanup_catalog(self, execute)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['catalog', 'graph', 'cleanup-plan', 'cleanup'])
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--inspect', action='store_true', help='Graphを作成せずChallenge Aの固定クエリだけを実行')
    parser.add_argument('--account')
    parser.add_argument('--run-id', default='initial', help='Use a new ID for a deliberate new inference')
    args = parser.parse_args()
    lock_path = ROOT / 'artifacts/semantics-poc/.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        poc = Poc(config(require_project=True), args.account, args.run_id)
        print('Target: ' + poc.parent, flush=True)
        if args.execute and args.command != 'cleanup':
            parser.error('--execute is only for cleanup')
        if args.inspect and args.command != 'graph':
            parser.error('--inspect is only for graph')
        if args.inspect:
            poc.inspect_graph()
        elif args.command == 'cleanup':
            poc.cleanup(args.execute)
        else:
            getattr(poc, args.command.replace('-', '_'))()


if __name__ == '__main__':
    main()

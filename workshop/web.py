"""Cloud Shell web preview. One subprocess per request; no deployment needed."""
import argparse
import fcntl
import html
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .settings import ROOT, config
from .cloud import modern_trace_url, save, timestamp
from .data_agent import fingerprint

ID = re.compile(r'^[a-f0-9]{32}$')
ASSETS = ROOT/'workshop/web_assets'
DEFAULT_QUESTION = '2026年7月から8月にかけて、粗利益の減少額が最も大きいカテゴリと、その減少額を教えてください。'


def read_json(path, default=None):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return default


# #1 の prepare は 1 回では完成しない。最初の実行は Catalog に [[...]] 入りの
# Entry を作るところまでで、参加者が Console で編集したあと、もう一度 prepare を
# 実行して初めて完成した定義がここへ保存される。
MISSING_DEFINITION_ROUTES = (
    '・#1 から参加している方：Knowledge Catalog の overview で 2 か所を置き換えて保存し、'
    'もう一度 #1 の prepare を実行してください。最初の prepare だけでは完成しません。'
    '編集した定義を取り込む 2 回目の実行で、ここへ保存されます。\n'
    '・Summit から参加している方：#1 は行いません。Summit の事前課題（Checkpoint）の'
    ' plan と apply を実行すると、完成した定義が用意されます。')


def current_context(root=ROOT):
    cfg = config(require_project=True, root=root)
    path = root/'participant/semantics.txt'
    # #1 を終えていない参加者に、素の FileNotFoundError を見せない。
    # 事前イベント参加者と Summit 単独参加者で経路が違うため、両方を案内する。
    if not path.is_file():
        raise ValueError('業務定義が用意できていません（participant/semantics.txt がありません）。\n'
                         + MISSING_DEFINITION_ROUTES)
    rules = path.read_text(encoding='utf-8')
    if not rules.strip():
        raise ValueError('業務定義が空です（participant/semantics.txt）。\n' + MISSING_DEFINITION_ROUTES)
    if '[[' in rules or ']]' in rules:
        raise ValueError('業務定義に未編集の箇所（[[...]]）が残っています。\n'
                         'Knowledge Catalog の overview で 2 か所を置き換えて保存し、'
                         'もう一度 #1 の prepare を実行すると、取得した定義がここへ保存されます。')
    return cfg, fingerprint({'config':cfg, 'rules':rules})


def public_record(record, folder):
    """Only workshop evidence is served. No worker logs, credentials or arbitrary files."""
    keys = ('run_id','implementation_version','question','started_at','finished_at','state','phase','answer','options',
            'elapsed_seconds','data_agent_calls','physical_call_attempts','analysis_requests','analysis_reuses','cache_events',
            'routing','sql_calls','root_model_usage','cost','evidence_reviews','configuration_hash','trace_console_url','error','root_model_requested','source_sha256')
    result = {k:record.get(k) for k in keys}
    result['trace_console_url'] = modern_trace_url(result.get('trace_console_url'))
    result['context'] = {k:record.get('config',{}).get(k) for k in ('project_id','dataset_id','definition_version','current_period','previous_period','as_of_date','bigquery_location','data_agent_location','catalog_location','runtime_location')}
    result['catalog_receipt'] = record.get('catalog_receipt')
    result['evidence'] = [read_json(p,{}) for p in sorted(folder.glob('evidence-*.json'))]
    from .economics import digest
    measurement = read_json(folder/'economics.json')
    result['economics'] = measurement if measurement and measurement.get('input_sha256') == digest(record,result['evidence']) else None
    return result


INTERRUPTED='実行が途中で止まりました（サーバーの再起動など）。もう一度「実行する」を押してください。'


class Runs:
    def __init__(self, root=ROOT, model='gemini-3.8-flash', local_trace=False):
        self.root, self.model, self.local_trace = root, model, local_trace
        self.lock = threading.Lock()
        self.active = None
        self.process = None
        self.cfg, self.context = current_context(root)
        self.token = secrets.token_urlsafe(32)
        self.closed = False

    def records(self):
        found=[]
        for p in (self.root/'artifacts/lab2').glob('*/run.json'):
            r=read_json(p)
            if r and r.get('config',{}).get('project_id')==self.cfg['project_id'] and r.get('config',{}).get('dataset_id')==self.cfg['dataset_id']:
                found.append(public_record(r,p.parent))
        return sorted(found,key=lambda r:r.get('started_at') or '',reverse=True)[:30]

    def start(self, body):
        if not isinstance(body,dict): raise ValueError('入力を確認してください。')
        question=body.get('question','')
        cache=body.get('cache')
        key=body.get('request_id','')
        if not isinstance(question,str) or not question.strip() or len(question)>2000: raise ValueError('質問を 1〜2000 文字で入力してください。')
        if cache not in ('on','off') or not isinstance(key,str) or not ID.fullmatch(key): raise ValueError('実行設定が不正です。画面を更新してください。')
        cfg,context=current_context(self.root)
        if context!=self.context or body.get('context')!=context: raise ValueError('対象 Project または定義が変わりました。サーバーを再起動してください。')
        job=self.root/'artifacts/web'/key
        payload={'question':question.strip(),'cache':cache,'context':context}
        with self.lock:
            old=read_json(job/'job.json')
            if old:
                if old.get('input')!=payload:raise ValueError('同じ実行 ID で異なる質問は実行できません。')
                return key
            if self.closed: raise ValueError('サーバーは終了処理中です。')
            if self.active: raise BlockingIOError('現在の分析が終わるまでお待ちください。')
            self.active=key
            save(job/'job.json',{'run_id':key,'state':'running','started_at':timestamp(),'input':payload})
            thread=threading.Thread(target=self._worker,args=(key,payload,job),daemon=True)
            thread.start()
        return key

    def _worker(self, key, payload, job):
        result=read_json(job/'job.json')
        try:
            command=[sys.executable,'-m','workshop.root_agent','--run-id',key,'--expected-context',payload['context'],
                     '--question',payload['question'],'--cache',payload['cache'],'--model',self.model]
            if self.local_trace:command.append('--local-trace-only')
            with (job/'worker.log').open('w',encoding='utf-8') as log:
                with self.lock:
                    if self.closed: raise RuntimeError('サーバーが終了しました。')
                    self.process=subprocess.Popen(command,cwd=self.root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try: code=self.process.wait(timeout=480)
                except subprocess.TimeoutExpired:
                    self.stop_process()
                    raise RuntimeError('8 分以内に完了しませんでした。結果と使用量は未確定です。')
            result.update(state='completed' if code==0 else 'failed',exit_code=code)
            if code: result['error']='実行を完了できませんでした。設定・認証と保存された実行記録を確認してください。'
        except Exception as exc: result.update(state='failed',error=str(exc))
        finally:
            result['finished_at']=timestamp()
            with self.lock:save(job/'job.json',result);self.active=None;self.process=None

    def stop_process(self):
        p=self.process
        if p and p.poll() is None:
            try:os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=3)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
            except ProcessLookupError:pass

    def state(self):
        # Failed jobs without a Root record (e.g. missing SDK) remain visible.
        # Read active and the job files under the same lock that start() and _worker() hold, so a run
        # started or finished in between cannot be mistaken for one cut off by a server restart.
        jobs=[]
        with self.lock:
            active=self.active
            for p in (self.root/'artifacts/web').glob('*/job.json'):
                r=read_json(p)
                if r and r.get('input',{}).get('context')==self.context:
                    if r.get('state')=='running' and r['run_id']!=active:
                        r=dict(r,state='interrupted',error=r.get('error') or INTERRUPTED)
                    jobs.append(r)
        records=self.records()
        by_id={j['run_id']:j for j in jobs}
        for record in records:
            job=by_id.get(record['run_id'])
            if job and record.get('state') not in ('completed','failed') and record['run_id']!=active:
                record['state']=job['state'] if job['state']!='running' else 'interrupted'
                record['error']=record.get('error') or job.get('error') or INTERRUPTED
        existing={r['run_id'] for r in records}
        for job in jobs:
            if job['run_id'] in existing:continue
            records.append({'run_id':job['run_id'],'started_at':job['started_at'],'state':job['state'],
                'question':job['input']['question'],'implementation_version':'web-launch',
                'options':{'evidence_cache_enabled':job['input']['cache']=='on'},
                'error':job.get('error'),
                'evidence':[]})
        return {'project':self.cfg['project_id'],'dataset':self.cfg['dataset_id'],'model':self.model,
                'context':self.context,'active':active,'records':sorted(records,key=lambda r:r.get('started_at') or '',reverse=True)[:30],
                'jobs':sorted(jobs,key=lambda r:r['started_at'],reverse=True)[:30], 'cloud_trace_enabled':not self.local_trace}


def handler(manager, port, web_host=None):
    hosts={f'localhost:{port}',f'127.0.0.1:{port}'}
    if web_host:hosts.add(f'{port}-{web_host}')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self, code, value, mime='application/json; charset=utf-8'):
            body=json.dumps(value,ensure_ascii=False).encode() if mime.startswith('application/json') else value.encode()
            self.send_response(code)
            self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://console.cloud.google.com https://shell.cloud.google.com")
            self.end_headers();self.wfile.write(body)
        def valid_host(self):return self.headers.get('Host','').lower() in hosts
        def do_GET(self):
            if not self.valid_host():return self.reply(403,{'error':'Host not allowed'})
            path=urlparse(self.path).path
            if path=='/' and getattr(manager,'home_benchmark',None):
                from .benchmark_report import page
                content=page(manager.root,manager.cfg,manager.home_benchmark)
                return self.reply(200,content,'text/html; charset=utf-8') if content is not None else self.reply(404,{'error':'評価記録がありません。'})
            if path=='/':
                page=(ASSETS/'index.html').read_text(encoding='utf-8').replace('{{CSRF}}',html.escape(manager.token)).replace('{{CONTEXT}}',html.escape(manager.context)).replace('{{REQUEST_ID}}',secrets.token_hex(16))
                page=page.replace('<span id="project">読み込み中</span>', '<span id="project">'+html.escape(manager.cfg['project_id']+' / '+manager.cfg['dataset_id'])+'</span>').replace('<span id="model"></span>', '<span id="model">'+html.escape(manager.model)+'</span>')
                fallback=''.join('<article><h3>'+html.escape(r.get('question') or '')+'</h3><p>'+html.escape((r.get('answer') or {}).get('customer_message_ja') or r.get('state') or '未確定')+'</p></article>' for r in manager.records()[:5])
                page=page.replace('{{FALLBACK}}',fallback or '<p>実行記録はまだありません。</p>')
                return self.reply(200,page,'text/html; charset=utf-8')
            if path in ('/styles.css','/app.js'):
                return self.reply(200,(ASSETS/path[1:]).read_text(encoding='utf-8'),'text/css; charset=utf-8' if path.endswith('css') else 'text/javascript; charset=utf-8')
            if path=='/api/state':return self.reply(200,manager.state())
            if path=='/benchmarks' or path.startswith('/benchmarks/'):
                from .benchmark_report import page
                content=page(manager.root,manager.cfg,None if path=='/benchmarks' else path[len('/benchmarks/'):])
                return self.reply(200,content,'text/html; charset=utf-8') if content is not None else self.reply(404,{'error':'評価記録がありません。'})
            if path=='/lab1':
                from .lab1 import example
                page=manager.root/'artifacts/lab1/index.html'
                if not page.exists():page=example(manager.root)
                return self.reply(200,page.read_text(encoding='utf-8').replace('<main>', '<main><a href="/" style="display:block;padding:1rem;color:#2B356B">← #2 Observability へ戻る</a>', 1),'text/html; charset=utf-8')
            return self.reply(404,{'error':'ページがありません。'})
        def do_POST(self):
            if not self.valid_host():return self.reply(403,{'error':'Host not allowed'})
            try:
                n=int(self.headers.get('Content-Length','0'))
                if n<=0 or n>16000:raise ValueError('入力サイズが不正です。')
                raw=self.rfile.read(n)
                form=self.headers.get('Content-Type','').split(';')[0]=='application/x-www-form-urlencoded'
                body={k:v[0] for k,v in parse_qs(raw.decode()).items()} if form else json.loads(raw)
                token=body.get('csrf','') if isinstance(body,dict) else ''
                if not secrets.compare_digest(str(token),manager.token):return self.reply(403,{'error':'画面を更新してから再実行してください。'})
                origin=self.headers.get('Origin')
                if origin and urlparse(origin).netloc.lower() not in hosts:return self.reply(403,{'error':'Origin not allowed'})
                if urlparse(self.path).path!='/api/run':return self.reply(404,{'error':'ページがありません。'})
                key=manager.start(body)
                if form:
                    return self.reply(202,'<html lang="ja"><meta charset="utf-8"><title>分析を開始しました</title><h1>分析を開始しました</h1><p>しばらくしてから画面に戻って結果を確認してください。</p><a href="/">結果画面に戻る</a></html>','text/html; charset=utf-8')
                return self.reply(202,{'run_id':key})
            except BlockingIOError as exc:return self.reply(409,{'error':str(exc)})
            except (ValueError,OSError,TypeError) as exc:return self.reply(400,{'error':str(exc)})
    return Handler


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8080);p.add_argument('--model',default='gemini-3.8-flash');p.add_argument('--local-trace-only',action='store_true')
    p.add_argument('--benchmark',help='この評価計画の結果をトップに出す（workshop.benchmark show が使う）');a=p.parse_args(argv)
    if not 2000<=a.port<=65000:p.error('port must be 2000..65000')
    lock_path=ROOT/'artifacts/web/.server.lock';lock_path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with lock_path.open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                print('別の Web サーバーがすでに動いています。そのサーバーを Ctrl+C で止めてから起動してください'
                      '（どこで動いているか分からない場合は pkill -f "workshop.web"）。',file=sys.stderr)
                return 2
            m=Runs(model=a.model,local_trace=a.local_trace_only);m.home_benchmark=a.benchmark
            with ThreadingHTTPServer(('127.0.0.1',a.port),handler(m,a.port,os.environ.get('WEB_HOST'))) as server:
                print('対象 Project: '+m.cfg['project_id']+' / '+m.cfg['dataset_id'],flush=True)
                print(f'Cloud Shell のターミナルの上のバーにある「ウェブプレビュー」→「ポート {a.port} でプレビュー」を選ぶと、ブラウザの新しいタブで教材の画面が開きます。',flush=True)
                print('このターミナルは閉じずにそのままにします（閉じると画面も止まります）。終了は Ctrl+C。',flush=True)
                try:server.serve_forever()
                except KeyboardInterrupt:pass
                finally:
                    with m.lock:m.closed=True
                    m.stop_process()
        return 0
    except (OSError,ValueError) as exc:print(str(exc),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())

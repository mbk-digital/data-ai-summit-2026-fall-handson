import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from workshop.lab1 import initialize, run, render, make_contexts, prepare_console
from workshop.settings import ROOT


class FakeClient:
    calls = []
    fail = False
    def __init__(self, cfg, account=None):
        self.cfg = cfg
    def ensure(self, variant, ctx):
        self.calls.append(("ensure", variant, ctx))
        return "projects/participant-project/locations/us/dataAgents/" + variant
    def chat(self, name, question, output):
        from workshop.cloud import save
        self.calls.append(("chat", name, question))
        r = {"state": "transport_or_parse_error" if self.fail else "completed", "question": question,
             "started_at": "2026-09-05T00:00:00Z", "elapsed_seconds": 1,
             "messages": [] if self.fail else [{"systemMessage": {"text": {"textType": "FINAL_RESPONSE", "parts": ["テスト用回答。実測ではない。"]}}}]}
        save(output, r)
        return r


class FakeCatalog:
    document = None
    def __init__(self, cfg, account=None, root=None):
        self.entry = f"projects/{cfg['project_id']}/locations/eu/entryGroups/test-group/entries/business-rules"
    def prepare_console_definition(self, starter):
        from workshop.catalog import concept
        document = self.document or starter
        fields, body = concept(document)
        return {'title': fields['title'], 'body': body, 'document': document,
                'entry': self.entry, 'body_sha256': 'test-hash'}
    def sync(self, document):
        from workshop.catalog import concept
        fields, body = concept(document)
        return {'title': fields['title'], 'body': body, 'entry': self.entry, 'body_sha256': 'test-hash'}


class Lab1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'semantics').mkdir()
        cfg = json.loads((ROOT/'config/workshop.json').read_text())
        cfg.update(project_id='participant-project',bigquery_location='US',data_agent_location='us')
        (self.root/'config/workshop.json').write_text(json.dumps(cfg))
        (self.root/'semantics/lab1-starter.txt').write_text('返金は[[返金日]]、原価は[[原価]]。')
        FakeClient.calls = []
        FakeClient.fail = False
        FakeCatalog.document = None

    def execute(self, **kwargs):
        with patch('builtins.print'):
            return run(root=self.root, client_factory=FakeClient, catalog_factory=FakeCatalog, **kwargs)

    def test_first_edit_resume_preserves_baseline_and_user_changes(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(sum(c[0]=='chat' for c in FakeClient.calls), 2)
        p = self.root/'participant/semantics.txt'
        p.write_text('返金は返金日に計上。原価は実際の回収額だけ戻す。')
        before=p.read_bytes()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(sum(c[0]=='chat' for c in FakeClient.calls), 6)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.execute(), 0)
        self.assertEqual(sum(c[0]=='chat' for c in FakeClient.calls), 6)
        self.assertEqual(len(list((self.root/'artifacts/lab1').rglob('snapshot.json'))), 2)
        ensures=[c for c in FakeClient.calls if c[0]=='ensure']
        self.assertEqual(ensures[1][2]['systemInstruction'],ensures[2][2]['systemInstruction'])
        self.assertEqual(ensures[1][2]['glossaryTerms'],ensures[2][2]['glossaryTerms'])
        self.assertNotIn('返金日に計上', ensures[1][2]['systemInstruction'])

    def test_failure_is_retained_and_not_silently_retried(self):
        FakeClient.fail=True
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.execute(), 1)
        self.assertEqual(sum(c[0]=='chat' for c in FakeClient.calls), 1)
        FakeClient.fail=False
        self.assertEqual(self.execute(repeat=True), 0)
        self.assertEqual(sum(c[0]=='chat' for c in FakeClient.calls), 3)
        states=[json.loads(p.read_text())['state'] for p in (self.root/'artifacts/lab1').rglob('raw.Q01.json')]
        self.assertIn('transport_or_parse_error', states)
        self.assertIn('completed', states)

    def test_result_escapes_untrusted_text_and_excludes_progress(self):
        cfg={'project_id':'participant-project','dataset_id':'ec_workshop'}
        case={'case_id':'Q01','question_ja':'質問'}
        record={'state':'completed','messages':[
            {'systemMessage':{'text':{'textType':'THOUGHT','parts':['PRIVATE_PROGRESS']}}},
            {'systemMessage':{'text':{'textType':'FINAL_RESPONSE','parts':['<script>alert(1)</script>']}}},
            {'systemMessage':{'data':{'result':{'data':[{'value':'<img src=x>'}]}}}}
        ]}
        record['lab'] = {'catalog_receipt': {'entry': 'entry', 'body': '<script>bad()</script>'}}
        page=render(cfg,[case],{('raw','Q01'):record},'<iframe>','test')
        self.assertNotIn('PRIVATE_PROGRESS',page)
        self.assertNotIn('<script>',page)
        self.assertNotIn('<img src=x>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('&lt;iframe&gt;',page)
        self.assertIn('Catalog から取得した定義',page)

    def test_blank_rules_never_create_a_completed_comparison(self):
        cfg={'project_id':'participant-project','dataset_id':'ec_workshop'}
        self.assertEqual(list(make_contexts(cfg,'')),['raw'])
        self.assertEqual(list(make_contexts(cfg,'[[未編集]]')),['raw'])

    def test_prepare_console_does_not_send_questions(self):
        output = io.StringIO()
        with redirect_stdout(output):
            agents = prepare_console(root=self.root, client_factory=FakeClient, catalog_factory=FakeCatalog)
        self.assertEqual(list(agents), ['raw'])
        self.assertEqual([call[0] for call in FakeClient.calls], ['ensure'])
        self.assertIn('ここでは質問を送信しません', output.getvalue())
        self.assertIn('① 業務定義なし：エージェント カタログの「EC分析 ① 業務定義なし」（ID: raw）', output.getvalue())
        self.assertIn('Q01:', output.getvalue())
        # 手順書と同じ順：エントリは見るだけ → ①に質問。編集を先に促さない。
        text = output.getvalue()
        self.assertIn('まだ編集しません', text)
        self.assertLess(text.index('Knowledge Catalog で'), text.index('BigQuery Agents で'))
        self.assertNotIn('Knowledge Catalog Entry:', text)
        expected_url = 'https://console.cloud.google.com/dataplex/dp-entries/projects/participant-project/locations/eu/entryGroups/test-group/entries/business-rules?project=participant-project'
        self.assertIn(expected_url, text)
        self.assertNotIn('/dp-search', text)

        starter = (self.root/'semantics/lab1-starter.txt').read_text()
        FakeCatalog.document = ('---\ntype: Metric\ntitle: Test\ndescription: Test\n---\n\n' + starter)
        FakeCatalog.document = FakeCatalog.document.replace('[[返金日]]', '返金日').replace('[[原価]]', '回収原価')
        FakeClient.calls = []
        complete_output = io.StringIO()
        with redirect_stdout(complete_output):
            agents = prepare_console(root=self.root, client_factory=FakeClient, catalog_factory=FakeCatalog)
        self.assertIn(expected_url, complete_output.getvalue())
        self.assertEqual(list(agents), ['raw', 'defined', 'view'])
        self.assertEqual([call[0] for call in FakeClient.calls], ['ensure', 'ensure', 'ensure'])
        self.assertEqual((self.root/'participant/semantics.txt').read_text().strip(), FakeCatalog.document)

    def test_prepare_console_says_whether_agent_was_created_or_reused(self):
        class Reusing(FakeClient):
            def ensure(self, variant, ctx):
                self.last_created = variant != 'raw'
                return super().ensure(variant, ctx)
        output = io.StringIO()
        with redirect_stdout(output):
            prepare_console(root=self.root, client_factory=Reusing, catalog_factory=FakeCatalog)
        self.assertIn('① 業務定義なしのエージェントを準備しています… 作成済みのものをそのまま使います（変更なし）', output.getvalue())
        self.assertNotIn(' 完了', output.getvalue())

    def test_apply_rules_stops_before_creating_agents_when_step2_is_unsaved(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, '手順 2 の編集がまだ保存されていません'):
                prepare_console(root=self.root, client_factory=FakeClient, catalog_factory=FakeCatalog, apply=True)
        self.assertEqual(FakeClient.calls, [])

    def test_unsaved_step2_message_does_not_blame_settings(self):
        import sys
        from contextlib import redirect_stderr
        from workshop import lab1
        err = io.StringIO()
        with patch.object(lab1, 'prepare_console', side_effect=lab1.StepNotReady('手順 2 の編集がまだ保存されていません。')), \
             patch.object(lab1, 'ROOT', self.root), patch.object(sys, 'argv', ['lab1', 'apply-rules']), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(lab1.main(), 2)
        self.assertIn('まだ実行できません', err.getvalue())
        self.assertNotIn('権限', err.getvalue())

    def test_apply_rules_creates_defined_agents_and_points_to_glossary(self):
        starter = (self.root/'semantics/lab1-starter.txt').read_text()
        FakeCatalog.document = ('---\ntype: Metric\ntitle: Test\ndescription: Test\n---\n\n' + starter)
        FakeCatalog.document = FakeCatalog.document.replace('[[返金日]]', '返金日').replace('[[原価]]', '回収原価')
        output = io.StringIO()
        with redirect_stdout(output):
            agents = prepare_console(root=self.root, client_factory=FakeClient, catalog_factory=FakeCatalog, apply=True)
        self.assertEqual(list(agents), ['raw', 'defined', 'view'])
        text = output.getvalue()
        self.assertIn('ルールを読み取りました: 「Test」', text)
        self.assertIn('用語集', text)
        self.assertNotIn('Knowledge Catalogを開く', text)
        self.assertIn('Q01:', text)
        self.assertIn('Q04:', text)

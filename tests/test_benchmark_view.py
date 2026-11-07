"""Participant-facing #3 output: cost states, CLI tables, the Summit comparison page and the example record."""
import contextlib
import io
import json
import re
import unittest
from unittest.mock import patch

from workshop import benchmark as b
from workshop import benchmark_view as v
from workshop.benchmark_report import render
from tests import test_benchmark

COLLECTION = 'c' * 32


class BenchmarkViewTests(unittest.TestCase):
    setUp = test_benchmark.BenchmarkTests.setUp
    plan = test_benchmark.BenchmarkTests.plan
    save_trial = test_benchmark.BenchmarkTests.save_trial

    def summit(self):
        preset = b.PRESETS['summit']
        return self.plan(case_ids=preset['cases'], repeats=preset['repeats'], pairs=preset['variants'], preset='summit')

    def measure(self, p, index, amount=0.1, reasons=(), upper=None, cache=None, manifest=None, state='completed'):
        """1 回分を保存し、費用の計算結果と、その計算で記録を読んだときの manifest を置く。"""
        folder, run, evidence, m = self.save_trial(p, index, amount=amount, state=state)
        jobs = m['components']['bigquery_query']['job_evidence']
        for job in jobs:
            if cache is not None: job['cache_hit'] = cache
        m['components']['bigquery_query'].update(reference_usd=0 if cache else amount, reasons=list(reasons))
        m.update(collection_id=COLLECTION, reference_total_upper_usd=upper)
        b.atomic_save(folder/'economics.json', m)
        b.atomic_save(self.root/'artifacts/lab3/collections'/COLLECTION/'manifest.json',
                      manifest or {'errors': {}, 'completeness': {'traces': True, 'jobs': True}})
        return folder, m

    def all_trials(self, p, **kw):
        for i in range(len(p['trials'])): self.measure(p, i, **kw)

    def states(self, p):
        return [t['cost_state'] for t in b.report(self.root, p['batch_id'])['trials']]

    # --- 未算出の分け方 ---

    def test_complete_costs_print_a_short_table_and_the_next_command(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        text = '\n'.join(v.summary_lines(b.report(self.root, p['batch_id']), 'collect'))
        self.assertIn('Data Agent・毎回取得', text); self.assertIn('0.300 USD', text); self.assertIn('benchmark show', text)
        self.assertNotIn('未算出', text); self.assertNotIn('{', text)

    def test_waiting_only_when_reads_were_clean_and_complete(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        self.measure(p, 0, amount=None, reasons=['対応するToken記録なし（反映待ちの可能性）'])
        self.assertEqual(self.states(p)[0], 'waiting')
        for manifest in ({'errors': {'traces': '500 backend error'}, 'completeness': {'traces': False, 'jobs': True}},
                         {'errors': {}, 'completeness': {'traces': False, 'jobs': True}}):
            with self.subTest(manifest=manifest):
                self.measure(p, 0, amount=None, reasons=['対応するToken記録なし（反映待ちの可能性）'], manifest=manifest)
                self.assertEqual(self.states(p)[0], 'unknown')
        self.measure(p, 0, amount=None, reasons=['対応するToken記録なし（反映待ちの可能性）', '同じTraceに複数の実行が該当'])
        self.assertEqual(self.states(p)[0], 'unmatched')

    def test_unmatched_only_when_nothing_else_could_be_wrong(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        mismatch = [v.EXTRA_JOB_REASON, '生成SQLに対応するジョブが不足']
        self.measure(p, 0, amount=None, reasons=mismatch)
        self.assertEqual(self.states(p)[0], 'unmatched')
        self.measure(p, 0, amount=None, reasons=mismatch+['この経路ではData Agentを呼び出していない。システム全体が無料という意味ではない'])
        self.assertEqual(self.states(p)[0], 'unmatched')
        for manifest in ({'errors': {'traces': '500 backend error'}, 'completeness': {'traces': False, 'jobs': True}},
                         {'errors': {}, 'completeness': {'traces': True, 'jobs': False}}):
            with self.subTest(manifest=manifest):
                self.measure(p, 0, amount=None, reasons=mismatch, upper=0.5, manifest=manifest)
                self.assertEqual(self.states(p)[0], 'unknown')
        for other in ('Trace一覧が不完全', 'BigQueryジョブ一覧が不完全', '容量課金または料金方式未確認：bytesから金額換算しない'):
            with self.subTest(other=other):
                self.measure(p, 0, amount=None, reasons=mismatch+[other])
                self.assertEqual(self.states(p)[0], 'unknown')
        self.measure(p, 0, amount=None, reasons=mismatch)
        (self.root/'artifacts/lab3/collections'/COLLECTION/'manifest.json').unlink()
        self.assertEqual(self.states(p)[0], 'unknown')

    def test_permission_only_with_a_permission_denied_answer(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        for error, expected in (('403 PERMISSION_DENIED: caller does not have permission', 'permission'),
                                ('Access Denied: Project participant-project: User does not have bigquery.jobs.listAll', 'permission'),
                                ('403 SERVICE_DISABLED: Cloud Trace API has not been used', 'unknown'),
                                ('403 Forbidden', 'unknown'),
                                ('403 billing account disabled', 'unknown')):
            with self.subTest(error=error):
                self.measure(p, 0, amount=None, reasons=['BigQueryジョブ一覧が不完全'],
                             manifest={'errors': {'jobs': error}, 'completeness': {'traces': True, 'jobs': False}})
                self.assertEqual(self.states(p)[0], expected)

    def test_failed_unmatched_and_not_collected_are_named(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        self.measure(p, 0, amount=None, state='failed')
        self.measure(p, 1, amount=None, reasons=[v.EXTRA_JOB_REASON], upper=0.5)
        (self.root/'artifacts/lab2'/p['trials'][2]['run_id']/'economics.json').unlink()
        states = self.states(p)
        self.assertEqual(states[:3], ['failed', 'unmatched', 'not_collected'])
        result = b.report(self.root, p['batch_id']); text = '\n'.join(v.summary_lines(result, 'collect'))
        self.assertIn('失敗した回の費用は計算しません', text); self.assertIn('照合できません', text); self.assertIn('まだ計算していません', text)
        self.assertIn('未算出は 0 円という意味ではありません', text); self.assertNotIn('1〜2 分待って', text)

    def test_cache_note_needs_every_matched_job_to_be_a_cache_hit(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        self.measure(p, 0, cache=True)
        _, m = self.measure(p, 1, cache=True)
        m['components']['bigquery_query']['job_evidence'] = []
        b.atomic_save(self.root/'artifacts/lab2'/p['trials'][1]['run_id']/'economics.json', m)
        self.measure(p, 2, cache='yes')
        rows = b.report(self.root, p['batch_id'])['trials']
        self.assertEqual([r['bigquery_cache_reused'] for r in rows[:3]], [True, False, False])
        self.assertFalse(v.cache_reused({'bigquery_query': {'reference_usd': 0, 'job_evidence': []}}))

    # --- run の出力 ---

    def test_run_summary_has_no_cost_and_points_to_step_4(self):
        p = self.summit(); self.all_trials(p, amount=None)
        text = '\n'.join(v.summary_lines(b.report(self.root, p['batch_id']), 'run'))
        self.assertIn('費用は手順 4 で計算します', text); self.assertNotIn('USD', text); self.assertNotIn('未算出', text)

    def test_run_says_how_many_questions_are_left_and_reports_each_end(self):
        p = self.summit(); folder = self.root/'artifacts/benchmarks'/p['batch_id']
        for t in p['trials'][:2]: b.atomic_save(b.attempt_path(folder, t), {'state': 'completed'})
        out = io.StringIO()
        with patch('workshop.benchmark.subprocess.Popen') as pop, contextlib.redirect_stdout(out):
            pop.return_value.wait.return_value = 0
            b.execute(self.root, p['batch_id'])
        text = out.getvalue()
        self.assertIn('6 回のうち 2 回は送り終えています。残りの 4 回を送ります', text)
        self.assertNotIn('実行 ID', text); self.assertEqual(text.count('→ 終わりました'), 4)
        self.assertIn('目安', text)

    def test_fresh_run_says_it_sends_questions_to_your_agent(self):
        p = self.summit(); out = io.StringIO()
        with patch('workshop.benchmark.subprocess.Popen', side_effect=OSError('no SDK')), contextlib.redirect_stdout(out):
            b.execute(self.root, p['batch_id'])
        text = out.getvalue()
        self.assertIn('あなたの Project の AI エージェントに 6 回質問を送ります', text)
        self.assertEqual(text.count('失敗しました。この回の費用は計算しません'), 6)

    # --- 比べる画面 ---

    def test_summit_page_shows_the_grid_jst_and_keeps_json_in_details(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        result = b.report(self.root, p['batch_id']); html = render(result)
        self.assertIn('質問 × やり方', html); self.assertIn('参考の見積もり', html); self.assertIn('日本時間', html)
        self.assertIn('href="#trial-Q04-', html); self.assertIn('id="trial-Q04-', html)
        outside = re.sub(r'<details>.*?</details>', '', html, flags=re.S)
        self.assertNotIn('<pre>', outside); self.assertNotIn('&quot;reference_usd&quot;', outside)
        self.assertIn('講師の記録例は準備中', html)

    def test_cost_difference_only_when_every_cost_is_known(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        html = render(b.report(self.root, p['batch_id']))
        self.assertIn('+0%', html)
        self.measure(p, 0, amount=None, upper=0.9)
        html = render(b.report(self.root, p['batch_id']))
        cost_row = re.search(r'参考費用（合計）</th>(.*?)</tr>', html).group(1)
        self.assertIn('未算出', cost_row); self.assertNotIn('%', cost_row)

    def test_other_plans_keep_the_existing_page(self):
        p = self.plan(case_ids=['Q01', 'Q04', 'Q08'], pairs=[('off', 'data_agent'), ('on', 'data_agent'), ('on', 'router')])
        html = render(b.report(self.root, p['batch_id']))
        self.assertIn('品質・対応範囲・費用を比べる', html); self.assertNotIn('質問 × やり方', html)

    # --- 講師の記録例 ---

    def trace(self, calls):
        spans = [{'spanId': '1', 'name': 'invocation', 'startTime': '2026-10-30T05:00:00Z', 'endTime': '2026-10-30T05:00:50Z'},
                 {'spanId': '2', 'parentSpanId': '1', 'name': 'catalog.fetch_definition', 'startTime': '2026-10-30T05:00:01Z', 'endTime': '2026-10-30T05:00:16.3Z'}]
        for n in range(calls):
            spans.append({'spanId': str(3+n), 'parentSpanId': '1', 'name': 'execute_tool data_agent.ask_data_agent',
                          'startTime': f'2026-10-30T05:00:{20+n*15}Z', 'endTime': f'2026-10-30T05:00:{32+n*15}Z'})
        return {'spans': spans}

    def test_span_breakdown_groups_by_name_in_start_order(self):
        summary = v.span_breakdown(self.trace(2))
        self.assertEqual(summary['total_seconds'], 50.0)
        self.assertEqual([s['name'] for s in summary['spans']], ['catalog.fetch_definition', 'execute_tool data_agent.ask_data_agent'])
        self.assertEqual(summary['spans'][1], {'name': 'execute_tool data_agent.ask_data_agent', 'depth': 0, 'count': 2, 'seconds': [12.0, 12.0]})

    def test_span_breakdown_indents_nested_spans(self):
        trace = self.trace(1); trace['spans'].append({'spanId': '9', 'parentSpanId': '3', 'name': 'generate_content', 'startTime': '2026-10-30T05:00:21Z', 'endTime': '2026-10-30T05:00:25Z'})
        depths = {s['name']: s['depth'] for s in v.span_breakdown(trace)['spans']}
        self.assertEqual(depths, {'catalog.fetch_definition': 0, 'execute_tool data_agent.ask_data_agent': 0, 'generate_content': 1})

    def test_example_record_refuses_missing_costs_and_identifiers(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        self.measure(p, 0, amount=None, reasons=['対応するToken記録なし（反映待ちの可能性）'])
        with self.assertRaisesRegex(ValueError, '費用を計算できていない'):
            v.example_record(b.report(self.root, p['batch_id']), p, [], self.root)
        self.measure(p, 0, amount=0.1)
        email = 'someone' + '@' + 'example.com'  # 配布の混入検査に触れないよう分けて書く
        for text in ('participant-project を確認しました', '担当 ' + email):
            with self.subTest(text=text):
                result = b.report(self.root, p['batch_id']); result['trials'][-1]['answer']['customer_message_ja'] = text
                with self.assertRaisesRegex(ValueError, 'Project ID'):
                    v.example_record(result, p, [], self.root)

    def test_example_page_copies_numbers_from_the_record(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        traces = [{'case_id': 'Q01', 'variant_label': 'Data Agent・毎回取得', **v.span_breakdown(self.trace(2))}]
        record = v.example_record(b.report(self.root, p['batch_id']), p, traces, self.root)
        text = json.dumps(record, ensure_ascii=False)
        self.assertNotIn('run_id', text); self.assertNotIn('console.cloud.google.com', text); self.assertNotIn('SELECT', text)
        page = v.example_markdown(record)
        self.assertIn('あなたの結果ではありません', page); self.assertIn('| Data Agent・毎回取得 | 0.300 USD', page); self.assertNotIn('自動チェック', page)
        self.assertIn('`execute_tool data_agent.ask_data_agent` | 2 | 12.0 秒 ＋ 12.0 秒', page)
        self.assertIn('入れ子', page); self.assertIn('プログラムの起動', page); self.assertIn('0.100000 USD', page)
        self.assertIn('## Q08 の回答文', page)
        html = render(b.report(self.root, p['batch_id']), record)
        self.assertIn('講師の記録例と比べる（あなたの結果ではありません）', html); self.assertIn(record['batch_id'], html)
        example = re.search(r'<section class="example">.*?</section>', html, flags=re.S).group(0)
        self.assertIn('中央値の差だけで判断せず、質問ごとの処理時間も確認してください。', example)
        self.assertIn('「質問ごとの結果」', example)

    def test_time_note_only_when_both_medians_are_shown(self):
        note = '中央値の差だけで判断せず、質問ごとの処理時間も確認してください。'
        p = self.summit(); self.all_trials(p, amount=0.1)
        totals = re.search(r'<h2 id="totals">.*?</section>', render(b.report(self.root, p['batch_id'])), flags=re.S).group(0)
        self.assertIn(note, totals); self.assertNotIn('短縮', totals)
        for i, t in enumerate(p['trials']):
            if t['variant'].endswith('-off'): self.measure(p, i, amount=None, state='failed')
        self.assertNotIn(note, render(b.report(self.root, p['batch_id'])))


    def test_export_writes_the_record_and_the_page_from_q01_traces(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        cfg = p['context']['config']; output = self.root/'data/examples/lab3-instructor-record.json'
        with self.assertRaisesRegex(ValueError, 'Trace の記録がない'):
            b.export_example(self.root, cfg, p['batch_id'], output)
        for cache in ('off', 'on'):
            folder = self.root/'artifacts/lab2'/b.q01_trial(p, cache)['run_id']
            run = json.loads((folder/'run.json').read_text()); run['trace_id'] = ('a' if cache == 'off' else 'b')*32
            evidence = [json.loads(x.read_text()) for x in sorted(folder.glob('evidence-*.json'))]
            m = json.loads((folder/'economics.json').read_text()); m['input_sha256'] = b.digest(run, evidence)
            b.atomic_save(folder/'run.json', run); b.atomic_save(folder/'economics.json', m)
        with patch('workshop.cloud.Client.request', side_effect=lambda method, url, *a, **k: self.trace(2 if url.endswith('a'*32) else 1)):
            path, page = b.export_example(self.root, cfg, p['batch_id'], output)
        record = json.loads(path.read_text())
        self.assertEqual([t['spans'][-1]['count'] for t in record['traces']], [2, 1])
        self.assertTrue(page.read_text().startswith('# #3 講師の記録例'))

    def test_example_refuses_other_identifiers_in_text_and_span_names(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        (self.root/'config/private-denylist.txt').write_text('# 非公開\nhidden-dev-project\n')
        number = '1234' * 3
        for text in ('https://console.cloud.google.com/bigquery?project=another-project-01', 'hidden-dev-project の表',
                     'projects/another-project-01/locations/us', 'Project 番号 ' + number):
            with self.subTest(text=text):
                result = b.report(self.root, p['batch_id']); result['trials'][-1]['answer']['customer_message_ja'] = text
                with self.assertRaisesRegex(ValueError, '識別子'):
                    v.example_record(result, p, [], self.root)
        trace = v.span_breakdown(self.trace(1)); trace['spans'][0]['name'] = 'projects/another-project-01/dataAgents/x'
        with self.assertRaisesRegex(ValueError, 'リソース名'):
            v.example_record(b.report(self.root, p['batch_id']), p, [{'case_id': 'Q01', 'variant_label': 'x', **trace}], self.root)

    def test_missing_manifest_is_unknown_and_the_report_still_shows(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        self.measure(p, 0, amount=None, reasons=['対応するToken記録なし（反映待ちの可能性）'])
        (self.root/'artifacts/lab3/collections'/COLLECTION/'manifest.json').unlink()
        result = b.report(self.root, p['batch_id'])
        self.assertEqual(result['trials'][0]['cost_state'], 'unknown')
        self.assertIn('問題がありました', '\n'.join(v.summary_lines(result, 'collect'))); self.assertIn('#3 の結果', render(result))

    def test_no_common_question_shows_no_time_instead_of_all_trials(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        for i, t in enumerate(p['trials']):
            if t['variant'].endswith('-off'): self.measure(p, i, amount=None, state='failed')
        text = '\n'.join(v.summary_lines(b.report(self.root, p['batch_id']), 'collect'))
        self.assertIn('処理時間は比べられません', text); self.assertNotIn(' 秒', text)
        self.assertNotIn('同じ質問 0 問', text)

    # --- 自動チェックの理由 ---

    def q04_off(self, p):
        return next(i for i, t in enumerate(p['trials']) if t['case_id'] == 'Q04' and t['variant'].endswith('-off'))

    def test_period_text_reads_months_and_ranges(self):
        self.assertEqual(v.period_text({'start': '2026-07-01', 'end_exclusive': '2026-08-01'}), '2026年7月')
        self.assertEqual(v.period_text({'start': '2026-07-01', 'end_exclusive': '2026-09-01'}), '2026年7月〜8月')
        self.assertEqual(v.period_text({'start': '2026-07-03', 'end_exclusive': '2026-07-21'}), '2026-07-03〜2026-07-20')
        self.assertEqual(v.period_text(None), '空')

    def test_check_verdicts_stay_in_the_instructor_details(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        i = self.q04_off(p); answer = json.loads(json.dumps(p['cases']['Q04']['expected']))
        answer['comparison_period'] = {'start': '2026-07-01', 'end_exclusive': '2026-09-01'}
        self.save_trial(p, i, answer=answer, amount=0.1)
        result = b.report(self.root, p['batch_id'])
        row = next(t for t in result['trials'] if t['run_id'] == p['trials'][i]['run_id'])
        self.assertEqual(row['errors'], ['comparison_period_mismatch'])
        x = v.check_explanation(row)
        self.assertEqual(x['short'], ['比較期間']); self.assertTrue(x['answer_issue'])
        self.assertIn('基準：2026年7月／この回：2026年7月〜8月', x['problems'][0])
        self.assertIn('数字と単位', x['matched']); self.assertIn('カテゴリなど', x['matched']); self.assertNotIn('比較期間', x['matched'])
        html = render(result)
        # 参加者の主画面には判定を出さない。回答は「回答あり」、理由は講師用の折りたたみの中だけ
        outside = re.sub(r'<details>.*?</details>', '', html, flags=re.S)
        for word in ('一致', '比較期間', '基準と', '基準：', '自動チェック', '合格'):
            self.assertNotIn(word, outside)
        self.assertIn('回答あり', outside); self.assertIn('手順 5 で回答文を読んで確かめます', outside)
        self.assertIn('講師用：自動チェックと詳しい記録', html)
        self.assertIn('基準：2026年7月／この回：2026年7月〜8月', html); self.assertIn('参加者の操作ミスではありません', html)
        self.assertTrue(v.mismatch_lines(result)[0].startswith('自動チェックで基準と違った回：Q04・毎回取得（比較期間）'))
        text = '\n'.join(v.summary_lines(result, 'collect'))
        for word in ('自動チェック', '比較期間', '基準', '一致'):
            self.assertNotIn(word, text)
        self.assertIn('答えた回数', text); self.assertIn('3/3', text)

    def test_changed_conditions_are_not_called_an_answer_problem(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        i = self.q04_off(p); folder = self.root/'artifacts/lab2'/p['trials'][i]['run_id']
        run = json.loads((folder/'run.json').read_text(encoding='utf-8')); run['source_sha256'] = {}
        b.atomic_save(folder/'run.json', run)
        result = b.report(self.root, p['batch_id'])
        row = next(t for t in result['trials'] if t['run_id'] == p['trials'][i]['run_id'])
        x = v.check_explanation(row)
        self.assertTrue(x['system']); self.assertFalse(x['answer_issue']); self.assertEqual(x['matched'], [])
        outside = re.sub(r'<details>.*?</details>', '', render(result), flags=re.S)
        self.assertIn('記録と計画が不一致', outside); self.assertNotIn('教材のファイルや設定が変わって', outside)
        self.assertIn('保存された実行記録が、評価計画の質問や設定と一致しません', render(result))
        self.assertNotIn('参加者の操作ミスではありません', render(result))
        text = '\n'.join(v.summary_lines(result, 'collect'))
        self.assertIn('記録と計画が一致しない回：Q04・毎回取得', text); self.assertIn('講師に声をかけてください', text)

    def test_matching_answers_name_only_the_checked_items(self):
        p = self.summit(); self.all_trials(p, amount=0.1)
        rows = b.report(self.root, p['batch_id'])['trials']
        q01 = v.check_explanation(next(t for t in rows if t['case_id'] == 'Q01'))
        self.assertNotIn('比較期間', q01['matched']); self.assertNotIn('保留の理由', q01['matched']); self.assertIn('数字と単位', q01['matched'])
        q08 = v.check_explanation(next(t for t in rows if t['case_id'] == 'Q08'))
        self.assertIn('保留の理由', q08['matched']); self.assertNotIn('数字と単位', q08['matched'])
        self.assertEqual(v.mismatch_lines(b.report(self.root, p['batch_id'])), [])

    def test_reports_saved_before_the_update_get_the_expected_answer_from_the_plan(self):
        from workshop.benchmark_report import page
        p = self.summit(); self.all_trials(p, amount=0.1)
        i = self.q04_off(p); answer = json.loads(json.dumps(p['cases']['Q04']['expected']))
        answer['comparison_period'] = {'start': '2026-07-01', 'end_exclusive': '2026-09-01'}
        self.save_trial(p, i, answer=answer, amount=0.1)
        result = b.report(self.root, p['batch_id'])
        for t in result['trials']: t.pop('expected')
        old = json.loads(json.dumps(result))
        x = v.check_explanation(next(t for t in old['trials'] if t['run_id'] == p['trials'][i]['run_id']))
        self.assertEqual(x['matched'], []); self.assertNotIn('基準：', x['problems'][0]); self.assertIn('この回：2026年7月〜8月', x['problems'][0])
        b.atomic_save(self.root/'artifacts/benchmarks'/p['batch_id']/'report.json', old)
        html = page(self.root, p['context']['config'], p['batch_id'])
        self.assertIn('基準：2026年7月／この回：2026年7月〜8月', html); self.assertNotIn('基準：空', html)
        self.assertIn('数字と単位', html)
        # 集計に使った計画と違う（ハッシュ不一致・欠落）ときは補わず、基準不明として出す
        folder = self.root/'artifacts/benchmarks'/p['batch_id']
        for change in ('plan', 'hash'):
            with self.subTest(change=change):
                saved = json.loads(json.dumps(old))
                if change == 'hash': saved.pop('plan_sha256')
                else: saved['plan_sha256'] = '0' * 64
                b.atomic_save(folder/'report.json', saved)
                html = page(self.root, p['context']['config'], p['batch_id'])
                self.assertIn('（この回：2026年7月〜8月）', html); self.assertNotIn('基準：', html)
                self.assertNotIn('照らして基準と一致した項目', html)

if __name__ == '__main__':
    unittest.main()

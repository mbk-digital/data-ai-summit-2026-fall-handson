"""Participant-facing summaries of a benchmark report: cost states, CLI tables and the example record.

Only reads saved records. Every judgement (checks, costs, reviews) stays in benchmark.report.
"""
import json
import re

from .economics import EXTRA_JOB_REASON, read

COMMAND = 'cd ~/summit-workshop && uv run --locked python3 -m workshop.benchmark '
# 未算出は、記録から判別できる範囲で分ける。証拠がなければ「原因が分からない」にする。
WAITING = {'対応するToken記録なし（反映待ちの可能性）', 'Trace数と取得試行数が不一致'}
CONFLICT = {'同じジョブに複数の実行が該当', '同じTraceに複数の実行が該当', EXTRA_JOB_REASON}
SQL_MISMATCH = CONFLICT | {'生成SQLに対応するジョブが不足', '対応ジョブなし。未取得を0円にしない'}
STATE_NOTES = {
    'failed': '失敗した回の費用は計算しません。手順書の「困ったとき」を見てください。',
    'permission': 'Cloud Trace または BigQuery のジョブ情報を読む権限がないため、計算できません。待っても変わりません。講師に声をかけてください。',
    'unmatched': '一部の記録を、どの質問の分か照合できません。この費用は空けたまま進めます。「最大」が出ているときは、多めに見積もった上限で、確定した費用ではありません。',
    'waiting': '記録がまだ届いていない可能性があります。1〜2 分待って、同じコマンドをもう一度実行します。',
    'unknown': '記録を読むときに問題がありました。もう一度実行しても同じなら、講師に声をかけてください。',
    'not_collected': '費用はまだ計算していません。',
}


def permission_denied(text):
    """403 でも、API の無効・課金・割り当ての問題は権限不足と言わない。"""
    lowered = str(text).lower()
    if any(x in lowered for x in ('service_disabled', 'has not been used', 'is disabled', 'billing', 'quota')):
        return False
    return 'permission_denied' in lowered or 'access denied' in lowered or ('403' in lowered and 'permission' in lowered)


def cost_state(root, attempt_state, run, measurement):
    """1 回分の費用の状態。complete・pending・failed・not_collected・permission・unmatched・waiting・unknown。"""
    if measurement and measurement.get('reference_total_usd') is not None:
        return 'complete'
    if attempt_state in ('pending', 'running'):
        return 'pending'
    if attempt_state not in ('completed', 'recovered') or run.get('state') != 'completed':
        return 'failed'
    if not measurement:
        return 'not_collected'
    reasons = {r for c in measurement.get('components', {}).values() for r in c.get('reasons', [])}
    # manifest は収集の最後に保存する。途中で止まって無いときは、証拠なしとして扱う。
    ident = str(measurement.get('collection_id') or '')
    path = root/'artifacts/lab3/collections'/ident/'manifest.json'
    manifest = read(path) if re.fullmatch('[a-f0-9]{32}', ident) and path.is_file() else {}
    errors = manifest.get('errors') or {}
    complete = manifest.get('completeness') or {}
    if any(permission_denied(e) for e in errors.values()):
        return 'permission'
    clean = manifest and not errors and complete.get('traces') is True and complete.get('jobs') is True
    if reasons & CONFLICT or measurement.get('reference_total_upper_usd') is not None:
        # 取得の失敗や別の理由が混ざるときは、空けたまま進む案内にしない。
        other = {r for r in reasons - SQL_MISMATCH - WAITING if not r.startswith('この経路では')}
        return 'unmatched' if clean and not other else 'unknown'
    if clean and '対応ジョブなし。未取得を0円にしない' in reasons and not reasons - {'対応ジョブなし。未取得を0円にしない'} - WAITING:
        return 'unmatched'
    if clean and reasons and reasons <= WAITING:
        return 'waiting'
    return 'unknown'


def cache_reused(components):
    """BigQuery 分が 0 で、照合したジョブがすべて cache_hit is True のときだけ。空の一覧では言わない。"""
    c = (components or {}).get('bigquery_query') or {}
    jobs = c.get('job_evidence') or []
    return c.get('reference_usd') == 0 and bool(jobs) and all(j.get('cache_hit') is True for j in jobs)


def data_agent_calls(row):
    return sum(e.get('route') == 'data_agent' for e in (row.get('routing') or {}).get('events', []))


def width(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def pad(text, size):
    return text + ' ' * max(2, size - width(text))


def money(value):
    return f'{value:.3f} USD'


def group_cost(group, rows):
    """表の費用の欄。全回が揃ったときだけ合計。上限は「最大」として添えるだけ。"""
    if group['reference_total_usd'] is not None:
        return money(group['reference_total_usd'])
    if group.get('reference_total_upper_usd') is not None:
        return '未算出（最大 ' + money(group['reference_total_upper_usd']) + '）'
    missing = [r for r in rows if r.get('cost_state') != 'complete']
    failed = sum(r.get('cost_state') == 'failed' for r in missing)
    if failed and failed == len(missing):
        return '未算出（失敗 ' + str(failed) + ' 回）'
    return '未算出（' + str(len(missing)) + ' 回分）'


def timing_note(result):
    timing = result.get('timing_comparison') or {}
    if timing.get('status') != 'complete':
        return None
    if not timing.get('pairs'):
        return 'どのやり方でも答えた同じ質問がないため、処理時間は比べられません。'
    names = {g['variant']: g['label'] for g in result['groups']}
    left = [x['case_id'] + '（' + '、'.join(names.get(r['variant'], r['variant']) for r in x['reasons']) + '）' for x in timing.get('excluded', [])]
    text = '処理時間は、どのやり方でも答えた同じ質問 ' + str(len(timing['pairs'])) + ' 問の中央値です。'
    return text + ('除いた質問：' + '、'.join(left) + '。' if left else '')


def summary_lines(result, stage):
    """run・collect の最後に出す短い表と、次にすること。"""
    rows_of = {g['variant']: [t for t in result['trials'] if t['variant'] == g['variant']] for g in result['groups']}
    lines = []
    if stage == 'collect':
        lines.append('費用の計算（参考の見積もり：使った量 × 公表価格。請求額ではありません）')
    else:
        lines.append('質問の結果')
    lines.append('')
    size = max(width(g['label']) for g in result['groups']) + 4
    head = '  ' + pad('やり方', size) + pad('自動チェック', 14) + (pad('参考費用（合計）', 30) if stage == 'collect' else '') + '処理時間の中央値'
    lines.append(head)
    for g in result['groups']:
        # 比べるときは同じ質問・同じ回の中央値だけ。対象がなければ全回の中央値には戻さない。
        seconds = g.get('median_wall_seconds') if g.get('common_timing') is None else g['common_timing'].get('median_seconds')
        cells = '  ' + pad(g['label'], size) + pad(str(g['structured_successes']) + '/' + str(g['planned']), 14)
        if stage == 'collect':
            cells += pad(group_cost(g, rows_of[g['variant']]), 30)
        lines.append(cells + ('—' if seconds is None else f'{seconds:.1f} 秒'))
    lines.append('')
    note = timing_note(result)
    if note:
        lines.append(note)
    states = [t.get('cost_state') for t in result['trials']]
    if stage == 'run':
        if 'failed' in states:
            lines.append(STATE_NOTES['failed'])
        lines.append('費用は手順 4 で計算します。')
        return lines
    shown = [s for s in ('failed', 'permission', 'unmatched', 'waiting', 'unknown', 'not_collected') if s in states]
    for s in shown:
        count = states.count(s)
        lines.append(('' if s in ('failed',) else str(count) + ' 回分は、') + STATE_NOTES[s])
    if shown:
        lines.append('未算出は 0 円という意味ではありません。')
    lines.append('')
    lines.append('次は、比べる画面を開きます：' + COMMAND + 'show')
    return lines


# --- 講師の記録例 -------------------------------------------------------------

# 配布の検査（scripts/export_distribution.py）と同じ考え方。リポジトリに固定する前に、自由記述や処理名も含めて確かめる。
IDENTIFIERS = [('メールアドレス', r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
               ('Console の URL', r'console\.cloud\.google\.com'),
               ('リソース名', r'projects/[a-z][a-z0-9-]{4,28}[a-z0-9]'),
               ('Project 番号らしき数字', r'(?<![A-Za-z0-9_])\d{12}(?![A-Za-z0-9_])'),
               ('個人のパス', r'/(Users|home)/[A-Za-z0-9._-]+/')]


def identifier_kind(text, root, project=None):
    """残っている識別子の種類（値そのものは返さない）。なければ None。"""
    if project and project in text:
        return 'Project ID'
    for kind, pattern in IDENTIFIERS:
        if re.search(pattern, text):
            return kind
    denied = root/'config/private-denylist.txt'
    if denied.is_file():
        values = [x.strip() for x in denied.read_text(encoding='utf-8').splitlines() if x.strip() and not x.startswith('#')]
        if any(x in text for x in values):
            return '非公開の一覧にある文字列'
    return None


def check_public(text, root, project=None):
    kind = identifier_kind(text, root, project)
    if kind:
        raise ValueError('講師の記録例に Project ID などの識別子らしき文字列（' + kind + '）が残っています。書き出しません')


def span_breakdown(trace):
    """Root の Trace を、処理名ごとの回数と所要時間にまとめる（最初に始まった順）。"""
    import datetime as dt
    def seconds(span):
        start = dt.datetime.fromisoformat(span['startTime'].replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat(span['endTime'].replace('Z', '+00:00'))
        return (end - start).total_seconds()
    spans = sorted(trace.get('spans', []), key=lambda s: s['startTime'])
    if not spans:
        return None
    by_id = {s.get('spanId'): s for s in spans}
    root = next((s for s in spans if s.get('parentSpanId') not in by_id), spans[0])
    def depth(span):
        n = 0
        while span.get('parentSpanId') in by_id and n < 50:
            span = by_id[span['parentSpanId']]; n += 1
        return n
    names = {}; depths = {}
    for s in spans:
        if s is root:
            continue
        name = s.get('name', '')
        names.setdefault(name, []).append(round(seconds(s), 1))
        depths[name] = min(depths.get(name, 99), max(depth(s) - 1, 0))
    return {'root_name': root.get('name', ''), 'total_seconds': round(seconds(root), 1),
            'spans': [{'name': n, 'depth': depths[n], 'count': len(v), 'seconds': v} for n, v in names.items()]}


def example_record(result, plan, traces, root):
    """講師の記録例。費用がすべて揃った計画だけ。識別子と SQL・URL は入れない。"""
    if any(t.get('cost_state') != 'complete' for t in result['trials']) or any(g['reference_total_usd'] is None for g in result['groups']):
        raise ValueError('費用を計算できていない回があるため、講師の記録例にしません。collect をやり直すか、別の評価計画を使ってください')
    trials = []
    for t in result['trials']:
        components = t.get('cost_components') or {}
        trials.append({'case_id': t['case_id'], 'question': t['question'], 'variant_label': t['variant_label'],
                       'outcome': t['outcome'], 'structured_success': t['structured_success'],
                       'elapsed_seconds': t['elapsed_seconds'], 'data_agent_calls': data_agent_calls(t),
                       'reference_total_usd': t['reference_total_usd'],
                       'cost': {k: (components.get(k) or {}).get('reference_usd') for k in ('root_model', 'data_agent', 'bigquery_query')},
                       'bigquery_cache_reused': cache_reused(components),
                       'answer_text': (t.get('answer') or {}).get('customer_message_ja')})
    groups = [{'label': g['label'], 'planned': g['planned'], 'structured_successes': g['structured_successes'],
               'reference_total_usd': g['reference_total_usd'],
               'median_seconds': (g.get('common_timing') or {}).get('median_seconds'),
               'timing_trials': (g.get('common_timing') or {}).get('trials'),
               'data_agent_calls': sum(x['data_agent_calls'] for x in trials if x['variant_label'] == g['label'])} for g in result['groups']]
    record = {'version': 'lab3-instructor-record-v1', 'batch_id': result['batch_id'], 'created_at': plan.get('created_at'),
              'pricing_version': result['pricing_version'], 'groups': groups, 'trials': trials, 'traces': traces}
    check_public(json.dumps(record, ensure_ascii=False), root, (plan.get('context') or {}).get('config', {}).get('project_id'))
    return record


def example_markdown(record):
    """講師の記録例を GitHub で読める 1 ページにする。数字は JSON からだけ写す。"""
    from .benchmark import jst
    def usd(v, digits=3):
        return '—' if v is None else f'{v:.{digits}f} USD'
    out = ['# #3 講師の記録例', '',
           '> **これは講師の記録例です。あなたの結果ではありません。** 自分の環境で実行できなかったときや、自分の結果と比べるときに使います。',
           '', '評価計画 `' + record['batch_id'] + '`（日本時間 ' + jst(record.get('created_at')) + ' に作成）、価格表 ' + record['pricing_version'] + '。',
           '費用は**参考の見積もり**です（使った量 × 公表価格。請求額ではありません）。Data Agent は 2026-12-31 まで無料試用中です。', '',
           '## やり方ごとの合計（手順 4-4）', '',
           '| やり方 | 自動チェックの合格 | 参考費用（3 問の合計） | 処理時間の中央値 | Data Agent に頼んだ回数 |', '|---|---|---|---|---|']
    for g in record['groups']:
        seconds = '—' if g['median_seconds'] is None else f"{g['median_seconds']:.1f} 秒"
        out.append('| ' + ' | '.join([g['label'], str(g['structured_successes']) + '/' + str(g['planned']), usd(g['reference_total_usd']), seconds, str(g['data_agent_calls']) + ' 回']) + ' |')
    out += ['', '処理時間は、どのやり方でも答えた同じ質問の中央値です（' + str(record['groups'][0].get('timing_trials')) + ' 問）。', '',
            '## 質問ごとの結果', '', '| 質問 | やり方 | 結果 | 参考費用 | 処理時間 | Data Agent に頼んだ回数 |', '|---|---|---|---|---|---|']
    for t in record['trials']:
        status = ('判断を保留' if t['outcome'] == 'abstained' else '回答') + ('・自動チェック合格' if t['structured_success'] else '・自動チェック不合格')
        out.append('| ' + ' | '.join([t['case_id'], t['variant_label'], status, usd(t['reference_total_usd']), ('—' if t['elapsed_seconds'] is None else f"{t['elapsed_seconds']:.1f} 秒"), str(t['data_agent_calls']) + ' 回']) + ' |')
    if record.get('traces'):
        out += ['', '## Q01 の処理の内訳（手順 3）', '', 'Cloud Trace の記録を、処理名ごとにまとめたものです。Data Agent に頼む回数を減らしても残る処理と時間を確かめます。',
                '', '処理は入れ子になっています。字下げした処理は、その上の処理の一部です。そのため、時間を足し合わせても全体の時間にはなりません。']
        for trace in record['traces']:
            elapsed = next((t['elapsed_seconds'] for t in record['trials'] if t['case_id'] == trace['case_id'] and t['variant_label'] == trace['variant_label']), None)
            out += ['', '### ' + trace['variant_label'] + '（Trace 全体 ' + str(trace['total_seconds']) + ' 秒）', '']
            if elapsed is not None:
                out += ['上の「質問ごとの結果」の処理時間（' + f'{elapsed:.1f}' + ' 秒）は、プログラムの起動なども含めて測っているため、Trace 全体より長くなります。', '']
            out += ['| 処理名 | 回数 | 所要時間 |', '|---|---|---|']
            for s in trace['spans']:
                out.append('| ' + '　' * s.get('depth', 0) + '`' + s['name'] + '` | ' + str(s['count']) + ' | ' + ' ＋ '.join(str(x) + ' 秒' for x in s['seconds']) + ' |')
    out += ['', '## 費用の内訳', '', '小さな金額も 0 円に見えないよう、小数点以下 6 桁で示します。', '',
            '| 質問 | やり方 | Root Agent の AI | Data Agent | BigQuery |', '|---|---|---|---|---|']
    for t in record['trials']:
        bq = usd(t['cost']['bigquery_query'], 6) + ('（保存済みのクエリ結果を再利用）' if t['bigquery_cache_reused'] else '')
        out.append('| ' + ' | '.join([t['case_id'], t['variant_label'], usd(t['cost']['root_model'], 6), usd(t['cost']['data_agent'], 6), bq]) + ' |')
    out += ['', '## Q08 の回答文（手順 5）', '']
    for t in record['trials']:
        if t['case_id'] == 'Q08':
            out += ['**' + t['variant_label'] + '**', '', '> ' + (t['answer_text'] or '（回答文なし）').replace('\n', '\n> '), '']
    return '\n'.join(out).rstrip() + '\n'

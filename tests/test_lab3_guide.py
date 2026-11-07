"""The #3 guide quotes the instructor record, the estimate sheet and its links consistently."""
import csv
import json
import re
import unittest

from workshop.benchmark_view import example_markdown
from workshop.settings import ROOT

GUIDE = ROOT/'docs/labs/lab3.md'
RECORD = ROOT/'data/examples/lab3-instructor-record.json'
SHEET = ROOT/'data/examples/lab3-estimate.csv'
SHEET_ID = '1pLNnYmldSzcw6_O6ALXErKiIozIEC0wKE3e22tsYZi0'  # CSV と同じ数式で作った共有のスプレッドシート
# 参加者向けのページだけ。Public にも入るので、講師用の資料（docs/maintainers）はここに入れない
# （講師用メモのリンクは tests/test_maintainer_docs.py。Public には入れない）。
LINKED = ['README.md', 'docs/labs/lab3.md', 'docs/labs/lab3-example.md', 'docs/labs/summit.md', 'docs/labs/decision-sheet.md',
          'docs/labs/map.md', 'docs/labs/lab2.md', 'docs/labs/checkpoint.md']


def slug(heading):
    """GitHub の見出しのアンカー（小文字、記号を除く、空白は -）。"""
    return re.sub(r'[^\w\- ]', '', heading.strip().lower()).replace(' ', '-')


def yen(value):
    """手順 6 の表の丸め方：1,000 円未満は 1 円単位、1 万円未満は 10 円単位、それ以上は万円単位。"""
    if value < 1000:
        return f'約 {round(value):,} 円'
    return f'約 {round(value, -1):,.0f} 円' if value < 10000 else f'約 {round(value / 10000):,} 万円'


def section(text, start, end):
    """見出し start から見出し end の前まで。"""
    i = text.index(start)
    return text[i:text.index(end, i)]


def table(text, header):
    """header を含む行から始まる Markdown の表を {行名: {列名: セル}} にする（太字は外す）。"""
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith('|') and header in line)
    rows = []
    for line in lines[i:]:
        if not line.startswith('|'):
            break
        cells = [c.strip().replace('**', '') for c in line.strip().strip('|').split('|')]
        if not set(''.join(cells)) <= set('-'):
            rows.append(cells)
    return {r[0]: dict(zip(rows[0][1:], r[1:])) for r in rows[1:]}


def percent(before, after):
    return f'{(after - before) / before * 100:+.0f}%'.replace('-', '−')


class Lab3GuideTests(unittest.TestCase):
    def setUp(self):
        self.guide = GUIDE.read_text(encoding='utf-8')
        self.record = json.loads(RECORD.read_text(encoding='utf-8'))
        self.groups = {g['label']: g for g in self.record['groups']}

    def per_item(self, label):
        # 3 問の平均を小数第 2 位に丸めた標準値（手順 6 に明記）。
        return round(self.groups[label]['reference_total_usd'] / len([t for t in self.record['trials'] if t['variant_label'] == label]), 2)

    def test_guide_has_no_answers_in_comments_and_no_placeholders(self):
        self.assertNotIn('<!--', self.guide)
        self.assertNotIn('【仮', self.guide)
        self.assertIn(f'(https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit)', section(self.guide, '### 試算シート', '### 判断を書く'))
        # 採用判断は試算シートの「採用判断」タブ（gid=110606）かメモに書く。Summit では判断シートへ案内しない
        self.assertIn(f'(https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=110606#gid=110606)', section(self.guide, '### 判断を書く', '### 隣の人と話す'))
        self.assertNotIn('decision-sheet.md', self.guide)

    def test_step_4_quotes_the_instructor_record_in_order(self):
        text = section(self.guide, '### 4-4．', '## 手順 5．')
        off, on = self.groups['Data Agent・毎回取得'], self.groups['Data Agent・再利用']
        cost = next(line for line in text.splitlines() if line.startswith('- 費用：'))
        expected = f"3 問の合計が {off['reference_total_usd']:.3f} USD から {on['reference_total_usd']:.3f} USD に下がりました（{percent(off['reference_total_usd'], on['reference_total_usd'])}）"
        self.assertIn(expected, cost)
        time = next(line for line in text.splitlines() if line.startswith('- 時間：'))
        self.assertIn(f"処理時間の中央値は {off['median_seconds']:.1f} 秒から {on['median_seconds']:.1f} 秒でした（どちらのやり方でも答えた同じ {off['timing_trials']} 問の中央値）", time)
        traces = {t['variant_label']: t['total_seconds'] for t in self.record['traces']}
        self.assertIn(f"{traces['Data Agent・毎回取得']} 秒と {traces['Data Agent・再利用']} 秒", time)
        # 質問ごとの時間は、講師の記録例の数字だと分かる形で、記録から写す。
        elapsed = {(t['case_id'], t['variant_label']): t['elapsed_seconds'] for t in self.record['trials']}
        self.assertIn('講師の記録例では', time)
        for case in ('Q01', 'Q04', 'Q08'):
            before, after = elapsed[case, 'Data Agent・毎回取得'], elapsed[case, 'Data Agent・再利用']
            self.assertIn(f'{case} が {before:.1f} 秒から {after:.1f} 秒（{percent(before, after)}）', time)
        self.assertIn('一律に半分になるとは限りません。各条件 1 回の実行では、変更の効果と実行ごとの変動を切り分けられません。', text)
        spread = next(line for line in text.splitlines() if line.startswith('- ぶれ：'))
        self.assertIn('上限の値で数えた', spread)

    def test_explanations_come_after_the_participant_writes(self):
        # 4-4：自分の表の後に、折りたたんだ記録例と結論。入口は参加者に見える形で残す。
        text = section(self.guide, '### 4-4．', '## 手順 5．')
        fold = text.index('<details>')
        self.assertLess(text.index('参考費用（3 問の合計）'), fold)
        self.assertIn('結果がない方はこの例を使う', text[fold:])
        for result in ('- 費用：', '- 時間：', '- ぶれ：', '**無駄な依頼を 1 回減らすと'):
            with self.subTest(result=result):
                self.assertGreater(text.index(result), fold)
                self.assertLess(text.index(result), text.index('</details>'))
        # 手順 6：仮の判断を書く → 話す → 例を見て、必要なら直す。
        step_6 = self.guide[self.guide.index('### 判断を書く'):self.guide.index('## 困ったとき')]
        order = [step_6.index(mark) for mark in ('仮の判断', '1 分ずつ話します', '判断を直します', '<summary>考え方の例</summary>')]
        self.assertEqual(order, sorted(order))

    def test_step_6_table_follows_the_rounded_standard_values(self):
        reuse, fetch = self.per_item('Data Agent・再利用'), self.per_item('Data Agent・毎回取得')
        text = section(self.guide, '## 手順 6．', '### 試算シート')
        self.assertIn(f'再利用 {reuse:.2f} USD・毎回取得 {fetch:.2f} USD', text)
        cells = table(text, '経営会議の資料')
        for column, count, share in (('経営会議の資料', 4, 1), ('社内チャットの質問窓口', 9000, 0.1)):
            check = count * share * 10 / 60 * 5000
            with self.subTest(column=column):
                self.assertTrue(cells['人だけで対応した場合'][column].startswith(yen(count * 15 / 60 * 5000)))
                self.assertEqual(cells['再利用：AI の実行料金'][column], yen(count * reuse * 150))
                self.assertTrue(cells['人が確かめる作業（どちらのやり方でも同じ）'][column].startswith(yen(check)))
                self.assertEqual(cells['再利用：合計'][column], yen(count * reuse * 150 + check))
                self.assertEqual(cells['毎回取得：合計'][column], yen(count * fetch * 150 + check) + '（うち AI ' + yen(count * fetch * 150) + '）')

    def test_step_5_quotes_the_record_answers_and_the_data(self):
        text = section(self.guide, '<summary>確かめた例', '</details>')
        answers = {t['variant_label']: t['answer_text'] for t in self.record['trials'] if t['case_id'] == 'Q08'}
        for name, label in (('毎回取得', 'Data Agent・毎回取得'), ('再利用', 'Data Agent・再利用')):
            line = next(x for x in text.splitlines() if x.startswith('- **' + name + '**'))
            quotes = [q for q in re.findall(r'「([^」]+)」', line) if not q.startswith('金額は正しい')]
            self.assertTrue(quotes)
            for quote in quotes:
                with self.subTest(variant=name, quote=quote):
                    self.assertIn(quote, answers[label])
        from workshop.data import connect
        profit = {tuple(r[:2]): r[2] for r in connect().execute(
            "SELECT substr(event_date,1,7), category, SUM(gross_profit_jpy) FROM revenue_events WHERE category='Electronics' GROUP BY 1,2")}
        self.assertIn(f"7 月 {profit[('2026-07', 'Electronics')]:,} 円 → 8 月 {profit[('2026-08', 'Electronics')]:,} 円".replace('-', '−'), text)

    def test_estimate_sheet_gives_the_same_amounts_with_standard_values(self):
        with SHEET.open(encoding='utf-8') as stream:
            rows = list(csv.reader(stream))
        cells = {}
        for r, row in enumerate(rows, 1):
            for c, value in zip('ABC', row):
                cells[c + str(r)] = value
        def value(ref, depth=0):
            text = cells[ref]
            if not text.startswith('='):
                return float(text)
            return eval(re.sub(r'[A-C]\d+', lambda m: repr(value(m.group(0), depth + 1)), text[1:]), {'__builtins__': {}})
        reuse, fetch = self.per_item('Data Agent・再利用'), self.per_item('Data Agent・毎回取得')
        self.assertEqual((value('B8'), value('B9'), value('C8'), value('C9')), (reuse, fetch, reuse, fetch))
        self.assertAlmostEqual(value('B17'), 4 * reuse * 150 + 4 * 10 / 60 * 5000)
        self.assertAlmostEqual(value('C19'), 9000 * fetch * 150 + 900 * 10 / 60 * 5000)
        self.assertEqual((value('B12'), value('C12')), (5000, 11250000))

    def test_example_page_is_generated_from_the_record(self):
        self.assertEqual((ROOT/'docs/labs/lab3-example.md').read_text(encoding='utf-8'), example_markdown(self.record))

    def test_participant_pages_do_not_link_to_maintainer_docs(self):
        # 講師用の資料は Public に入らないので、参加者向けのページからリンクしない。
        # README の末尾の講師用資料へのリンクだけは、Public の書き出しで消す（docs/maintainers/distribution）。
        for name in LINKED:
            text = (ROOT/name).read_text(encoding='utf-8')
            for target in re.findall(r'\]\((?!https?:|mailto:)([^)#\s]+)', text):
                if name == 'README.md' and target == 'docs/maintainers/README.md':
                    continue
                with self.subTest(file=name, target=target):
                    resolved = ((ROOT/name).parent/target).resolve()
                    self.assertFalse(resolved.is_relative_to((ROOT/'docs/maintainers').resolve()), target)

    def test_relative_links_and_anchors_resolve(self):
        for name in LINKED:
            path = ROOT/name; text = path.read_text(encoding='utf-8')
            for target, anchor in re.findall(r'\]\((?!https?:|mailto:)([^)#\s]*)(?:#([^)\s]*))?\)', text):
                with self.subTest(file=name, target=target, anchor=anchor):
                    resolved = (path.parent/target).resolve() if target else path
                    self.assertTrue(resolved.exists(), resolved)
                    if anchor and resolved.suffix == '.md':
                        headings = [slug(h) for h in re.findall(r'^#+ (.+)$', resolved.read_text(encoding='utf-8'), flags=re.M)]
                        self.assertIn(anchor, headings)


if __name__ == '__main__':
    unittest.main()

"""配布ツリーの混入検査に、教材のソース自体が引っかからないことを確認する。

`scripts/export_distribution.py` は秘密情報・個人パス・Project 番号らしき文字列を
見つけると配布を中止する。テストコードに書いた例示値がこれに触れると、配布が作れなく
なる。実際にメール形式の例示アカウントと 12 桁の数字で発生したため、検査を先に回す。

**検査の対象一覧と判定は本番の実装をそのまま読み込む。** テスト側へ複製すると、
片方だけ変わったときにテストが通ったままエクスポートが失敗し得る。
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def exporter():
    spec = importlib.util.spec_from_file_location('export_distribution',
                                                  ROOT / 'scripts/export_distribution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DistributionScanTests(unittest.TestCase):
    def setUp(self):
        self.export_distribution = exporter()

    def test_no_source_file_trips_the_privacy_scan(self):
        hits = []
        for path in self.export_distribution.distributed_paths():
            found = self.export_distribution.private_content(path.read_text(encoding='utf-8'))
            if found is not None:
                hits.append('%s -> %s' % (path.relative_to(ROOT), found[:40]))
        self.assertEqual(hits, [], '配布の混入検査に触れる記述があります:\n  ' + '\n  '.join(hits))

    def test_the_scan_still_detects_a_planted_value(self):
        # 検査そのものが機能していることを確かめる。
        # このファイルも配布対象なので、例示値は組み立てて literal を残さない。
        planted_mail = 'someone' + '@' + 'example' + '.' + 'com'
        planted_number = '1234' + '5678' + '9012'
        self.assertIsNotNone(self.export_distribution.private_content('contact: ' + planted_mail))
        self.assertIsNotNone(self.export_distribution.private_content('number ' + planted_number))
        self.assertIsNone(self.export_distribution.private_content('通常の本文です。'))

    def test_denylisted_strings_are_caught_and_the_list_stays_local(self):
        # 組み立てた値で、Git に残らない一覧の仕組みだけを確かめる。
        secret_id = 'dev' + '-only-' + 'project'
        self.assertEqual(self.export_distribution.private_content('target ' + secret_id, [secret_id]), secret_id)
        self.assertIsNone(self.export_distribution.private_content('target other', [secret_id]))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.export_distribution.denylist(root), [])
            (root / 'config').mkdir()
            (root / self.export_distribution.DENYLIST).write_text('# 開発用\n' + secret_id + '\n\n', encoding='utf-8')
            self.assertEqual(self.export_distribution.denylist(root), [secret_id])
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn(self.export_distribution.DENYLIST, ignored)
        self.assertNotIn(self.export_distribution.DENYLIST, [str(p.relative_to(ROOT)) for p in self.export_distribution.distributed_paths()])

    def test_a_real_export_succeeds(self):
        """一時フォルダへ実際に書き出す。検査だけでなく書き出し全体を通す。"""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'tree'
            self.export_distribution.export(output)
            self.assertTrue((output / 'README.md').is_file())
            self.assertTrue((output / 'uv.lock').is_file(), 'uv.lock が配布に含まれていません')
            self.assertTrue((output / 'workshop/web_assets/app.js').is_file())
            # ローカル状態は含めない。
            self.assertFalse((output / 'config/workshop.local.json').exists())
            self.assertFalse((output / 'artifacts').exists())
            self.assertFalse((output / 'participant').exists())


if __name__ == '__main__':
    unittest.main()

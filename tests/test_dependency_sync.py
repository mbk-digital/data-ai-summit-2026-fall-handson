"""requirements-adk.txt と pyproject.toml の依存が一致することを検査する。

requirements-adk.txt は workshop/root_agent.py が実行記録へ残す provenance であり、
uv が解決する依存（pyproject.toml）とずれると記録が事実と食い違う。
tomllib は Python 3.11 以降のため、ここでは行単位で読む。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r'^([A-Za-z0-9._-]+)==([A-Za-z0-9._-]+)$')


def requirements_pins():
    pins = {}
    for line in (ROOT / 'requirements-adk.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise AssertionError('requirements-adk.txt はピン留めのみを許可します: ' + line)
        pins[match.group(1)] = match.group(2)
    return pins


def pyproject_pins():
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    block = re.search(r'^dependencies\s*=\s*\[(.*?)\]', text, re.MULTILINE | re.DOTALL)
    if not block:
        raise AssertionError('pyproject.toml に dependencies が見つかりません')
    pins = {}
    for item in re.findall(r'"([^"]+)"', block.group(1)):
        match = PIN.fullmatch(item.strip())
        if not match:
            raise AssertionError('pyproject.toml はピン留めのみを許可します: ' + item)
        pins[match.group(1)] = match.group(2)
    return pins


class DependencySyncTests(unittest.TestCase):
    def test_requirements_and_pyproject_pins_match(self):
        self.assertEqual(requirements_pins(), pyproject_pins())

    def test_requirements_file_exists(self):
        # workshop/root_agent.py は is_file() の確認なしにこのファイルをハッシュする。
        # 削除・改名すると #2 の実行が例外で停止する。
        self.assertTrue((ROOT / 'requirements-adk.txt').is_file())

    def test_checkpoint_adk_pin_matches_requirements(self):
        # workshop/checkpoint.py は導入済み google-adk の版を文字列で確認している。
        source = (ROOT / 'workshop/checkpoint.py').read_text(encoding='utf-8')
        pinned = requirements_pins()['google-adk']
        self.assertIn(pinned, source)

    def test_requires_python_allows_adk(self):
        text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('requires-python = ">=3.10"', text)


class EnvironmentRecordTests(unittest.TestCase):
    """実行記録に残す依存環境。5 ピンの同期テストとは目的が違う。

    同期テストは「教材が意図する版」の食い違いを防ぐ。こちらは「実際に使われた版」を
    後から読めるようにする。間接依存まで旧環境と同じとは限らないため、両方が要る。
    """

    def test_environment_record_shape(self):
        from workshop.root_agent import PINNED_DISTRIBUTIONS, environment_record
        record = environment_record()
        self.assertEqual(set(record), {'python_version', 'uv_lock_sha256', 'resolved_distributions'})
        self.assertRegex(record['python_version'], r'^\d+\.\d+')
        self.assertEqual(set(record['resolved_distributions']), set(PINNED_DISTRIBUTIONS))

    def test_environment_record_hashes_lock_when_present(self):
        from workshop.root_agent import environment_record
        record = environment_record()
        if (ROOT / 'uv.lock').is_file():
            self.assertRegex(record['uv_lock_sha256'], r'^[0-9a-f]{64}$')
        else:
            self.assertIsNone(record['uv_lock_sha256'])

    def test_pinned_distributions_cover_requirements(self):
        from workshop.root_agent import PINNED_DISTRIBUTIONS
        self.assertEqual(set(PINNED_DISTRIBUTIONS), set(requirements_pins()))

    def test_frozen_context_watches_lock(self):
        # 計画作成後に uv.lock が変わったら、評価の実行を止めたい。
        source = (ROOT / 'workshop/benchmark.py').read_text(encoding='utf-8')
        self.assertIn("root/'uv.lock'", source)


if __name__ == '__main__':
    unittest.main()

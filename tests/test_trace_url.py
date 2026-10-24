"""Cloud Trace のリンク。今の Trace Explorer で 1 つの Trace を直接開ける形にする。"""
import unittest

from workshop.cloud import modern_trace_url, trace_console_url


class TraceUrlTests(unittest.TestCase):
    def test_new_url_opens_one_trace_in_explorer(self):
        self.assertEqual(trace_console_url('my-project', 'abc123'),
                         'https://console.cloud.google.com/traces/explorer;traceId=abc123?project=my-project')

    def test_old_list_url_is_rewritten(self):
        old = 'https://console.cloud.google.com/traces/list?project=my-project&tid=abc123'
        self.assertEqual(modern_trace_url(old), trace_console_url('my-project', 'abc123'))

    def test_other_values_are_left_alone(self):
        for value in (None, 'https://console.cloud.google.com/traces/list?project=x', 'https://example.com/traces/list?tid=1&project=x'):
            self.assertEqual(modern_trace_url(value), value)


if __name__ == '__main__':
    unittest.main()

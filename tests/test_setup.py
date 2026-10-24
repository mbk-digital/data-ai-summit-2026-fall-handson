"""`workshop.setup` の受入条件と、レビューで検出された 4 件の回帰を固定する。

受入条件:
1. クラウド変更前に Project ID・アカウント・実施内容を確認できる
2. 途中失敗時に、完了済み工程と再開方法が表示される
3. 再実行が参加者の定義・既存設定・台帳を上書きしない
4. 費用照合専用の権限（bigquery.jobs.listAll）不足だけでは基本準備を止めない

回帰（RegressionTests）:
R1 確認した Project と実際の接続先が食い違う
R2 進捗ファイルが無いと既存設定を上書きする
R3 #2 の権限診断が本線から抜ける
R4 完了フラグだけで準備完了と判断する
R5 観測設定が無効でも準備完了と判断する
R6 途中再開で、省略した工程の前提を確認しない
R7 進捗に対象設定を保存しておらず、配置変更を検出できない
"""
import argparse
import io
import json
import tempfile
import pathlib
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from workshop import setup as setup_module
from workshop.cloud import CloudError, PERMISSIONS, stage_requirements


def arguments(**overrides):
    values = {'project': 'participant-project', 'account': None, 'bigquery_location': None,
              'data_agent_location': None, 'runtime_location': None, 'catalog_location': None,
              'yes': False}
    values.update(overrides)
    return argparse.Namespace(**values)


def patched(state=None, existing=None, account='participant-account'):
    """read_state / config / current_account を差し替えるための共通設定。"""
    state = dict(state or {})
    if 'enable-apis' in state.get('completed', []):
        state.setdefault('enabled_apis', setup_module.planned_apis())
    return (mock.patch.object(setup_module, 'read_state', return_value=state),
            mock.patch.object(setup_module, 'config', return_value=existing or {}),
            mock.patch.object(setup_module, 'current_account', return_value=account))


class PlanAndConfirmTests(unittest.TestCase):
    def test_plan_names_project_account_and_every_step(self):
        out = io.StringIO()
        with redirect_stdout(out):
            setup_module.show_plan('participant-project', 'participant-account', [], list(setup_module.STEPS))
        text = out.getvalue()
        self.assertIn('participant-project', text)
        for label in setup_module.LABELS.values():
            self.assertIn(label, text)
        self.assertIn('上書きしません', text)
        self.assertIn('利用料金', text)

    def test_plan_shows_the_real_account_not_a_placeholder(self):
        out = io.StringIO()
        with redirect_stdout(out):
            setup_module.show_plan('participant-project', 'participant-account', [], ['configure'])
        text = out.getvalue()
        self.assertIn('participant-account', text)
        self.assertNotIn('gcloudの現在のアカウント', text)

    def test_plan_shows_the_number_of_apis_before_enabling_them(self):
        # API 名の一覧は参加者に不要なため件数だけを示す。名前は準備の記録に残る。
        out = io.StringIO()
        with redirect_stdout(out):
            setup_module.show_plan('participant-project', 'participant-account', [], ['enable-apis'])
        self.assertIn('（' + str(len(setup_module.planned_apis())) + ' 件）', out.getvalue())

    def test_declining_confirmation_makes_no_cloud_call(self):
        a, b, c = patched()
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=False), \
             mock.patch.object(setup_module, 'configure_project') as configure, \
             mock.patch.object(setup_module, 'enable_apis') as apis, \
             mock.patch.object(setup_module, 'client_session') as session:
            out = io.StringIO()
            with redirect_stdout(out):
                code = setup_module.run(arguments())
        self.assertEqual(code, 1)
        self.assertIn('クラウドは変更していません', out.getvalue())
        configure.assert_not_called()
        apis.assert_not_called()
        session.assert_not_called()

    def test_non_interactive_without_yes_is_refused(self):
        with mock.patch('sys.stdin.isatty', return_value=False):
            with self.assertRaisesRegex(CloudError, '--yes'):
                setup_module.confirm(False)


class ResumeTests(unittest.TestCase):
    def test_completed_steps_are_skipped_and_reported(self):
        state = {'project_id': 'participant-project', 'completed': ['configure', 'enable-apis']}
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=False):
            out = io.StringIO()
            with redirect_stdout(out):
                setup_module.run(arguments())
        text = out.getvalue()
        self.assertIn('完了済み', text)
        self.assertIn(setup_module.LABELS['bootstrap'], text)

    def test_failure_reports_completed_steps_and_how_to_resume(self):
        recorded = []
        a, b, c = patched()
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'configure_project', return_value='config/workshop.local.json'), \
             mock.patch.object(setup_module, 'enable_apis', side_effect=CloudError('APIの有効化に失敗')), \
             mock.patch.object(setup_module, 'write_state', side_effect=lambda p, d, s: recorded.append(list(d))):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments())
        self.assertEqual(code, 2)
        message = err.getvalue()
        self.assertIn('完了した工程', message)
        self.assertIn('残りの工程から再開', message)
        self.assertEqual(recorded[-1], ['configure'])


class RegressionTests(unittest.TestCase):
    """レビューで検出された 4 件。テストが通っても検出できていなかったもの。"""

    def test_r1_connecting_to_a_different_project_stops_before_building(self):
        # 進捗のProjectを表示・承認したのに、client_session が別Projectを読み直す場合。
        state = {'project_id': 'participant-project', 'completed': ['configure', 'enable-apis']}
        client = mock.MagicMock()
        client.project = 'another-project'
        session = mock.MagicMock()
        session.return_value.__enter__.return_value = client
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'client_session', session), \
             mock.patch.object(setup_module, 'bootstrap') as bootstrap, \
             mock.patch.object(setup_module, 'write_state'):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments(project=None))
        self.assertEqual(code, 2)
        self.assertIn('接続先', err.getvalue())
        bootstrap.assert_not_called()

    def test_r2_existing_settings_are_not_overwritten_without_progress_file(self):
        # 旧手順で準備した参加者には進捗ファイルが無い。既存設定と違う指定は止める。
        a, b, c = patched({}, {'project_id': 'earlier-project'})
        with a, b, c:
            with self.assertRaisesRegex(ValueError, 'earlier-project'):
                setup_module.run(arguments(project='another-project'))

    def test_r2_existing_locations_are_not_overwritten(self):
        a, b, c = patched({}, {'project_id': 'participant-project', 'bigquery_location': 'US'})
        with a, b, c:
            with self.assertRaisesRegex(ValueError, '上書きしません'):
                setup_module.run(arguments(bigquery_location='EU'))

    def test_r2_matching_existing_settings_are_reused_not_rewritten(self):
        existing = {'project_id': 'participant-project', 'bigquery_location': 'US'}
        a, b, c = patched({}, existing)
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'configure_project') as configure, \
             mock.patch.object(setup_module, 'enable_apis', side_effect=CloudError('停止')), \
             mock.patch.object(setup_module, 'write_state'):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                setup_module.run(arguments(bigquery_location='US'))
        configure.assert_not_called()
        self.assertIn('そのまま使います', out.getvalue())

    def test_r3_lab2_diagnosis_is_on_the_main_path(self):
        self.assertIn('diagnose', setup_module.STEPS)
        self.assertEqual(setup_module.DIAGNOSE_STAGE, 'lab2')
        _, permissions = stage_requirements(setup_module.DIAGNOSE_STAGE)
        # モデル実行・Trace・Monitoring の権限が診断に含まれること。
        for permission in ('aiplatform.endpoints.predict', 'cloudtrace.traces.patch',
                           'cloudtrace.traces.get', 'monitoring.timeSeries.list'):
            self.assertIn(permission, permissions)

    def test_r3_failed_diagnosis_stops_before_observability(self):
        state = {'project_id': 'participant-project', 'completed': ['configure', 'enable-apis', 'bootstrap']}
        client = mock.MagicMock()
        client.project = 'participant-project'
        session = mock.MagicMock()
        session.return_value.__enter__.return_value = client
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'client_session', session), \
             mock.patch.object(setup_module, 'preflight', return_value={'can_attempt_stage': False}), \
             mock.patch.object(setup_module, 'Observability') as observability, \
             mock.patch.object(setup_module, 'write_state'):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments(project=None))
        self.assertEqual(code, 2)
        self.assertIn('不足', err.getvalue())
        observability.assert_not_called()

    def test_r4_all_steps_done_still_reads_actual_cloud_state(self):
        state = {'project_id': 'participant-project', 'completed': list(setup_module.STEPS)}
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'inspect_cloud') as inspect:
            inspect.return_value = {'stage_ready': True, 'data_ready': True,
                                    'observability_ready': True, 'ready': True}
            out = io.StringIO()
            with redirect_stdout(out):
                code = setup_module.run(arguments())
        self.assertEqual(code, 0)
        inspect.assert_called_once()
        self.assertIn('現在の状態を確認します', out.getvalue())
        self.assertIn('準備はできています。', out.getvalue())

    def test_r4_torn_down_environment_is_not_reported_as_ready(self):
        # 片付け後やリソース変更後に「準備はすべて完了」と言わせない。
        state = {'project_id': 'participant-project', 'completed': list(setup_module.STEPS)}
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'inspect_cloud') as inspect:
            inspect.return_value = {'stage_ready': True, 'data_ready': False,
                                    'observability_ready': False, 'ready': False}
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments())
        self.assertEqual(code, 1)
        self.assertIn('未完', out.getvalue())
        self.assertIn('不足があります', err.getvalue())

    def test_r4_progress_flag_does_not_hide_a_changed_local_setting(self):
        state = {'project_id': 'participant-project', 'completed': ['configure']}
        a, b, c = patched(state, {'project_id': 'participant-project', 'bigquery_location': 'US'})
        with a, b, c:
            with self.assertRaisesRegex(ValueError, '上書きしません|違います'):
                setup_module.run(arguments(bigquery_location='EU'))


class CostCollationPermissionTests(unittest.TestCase):
    def test_setup_does_not_depend_on_jobs_listall(self):
        for stage in (setup_module.BOOTSTRAP_STAGE, setup_module.DIAGNOSE_STAGE):
            _, permissions = stage_requirements(stage)
            self.assertNotIn('bigquery.jobs.listAll', permissions)
        self.assertNotIn('bigquery.jobs.listAll', PERMISSIONS)

    def test_planned_apis_cover_both_former_gcloud_commands(self):
        former = {'bigquery.googleapis.com', 'geminidataanalytics.googleapis.com',
                  'cloudaicompanion.googleapis.com', 'dataplex.googleapis.com',
                  'aiplatform.googleapis.com', 'cloudtrace.googleapis.com',
                  'monitoring.googleapis.com', 'logging.googleapis.com'}
        self.assertEqual(set(setup_module.planned_apis()), former | {'cloudresourcemanager.googleapis.com'})


class ObservabilityAndResumeRegressionTests(unittest.TestCase):
    """2 回目のレビューで検出された 3 件。"""

    def test_r5_disabled_settings_are_not_treated_as_ready(self):
        from workshop.observability import settings_enabled
        self.assertFalse(settings_enabled({'conversationalAnalyticsSetting':
                                           {'metricsEnabled': False, 'tracesEnabled': False}}))
        self.assertFalse(settings_enabled({'conversationalAnalyticsSetting': {'metricsEnabled': True}}))
        self.assertFalse(settings_enabled({}))
        self.assertFalse(settings_enabled(None))
        self.assertTrue(settings_enabled({'conversationalAnalyticsSetting':
                                          {'metricsEnabled': True, 'tracesEnabled': True}}))

    def test_r5_binding_must_target_this_project_and_product(self):
        from workshop.observability import binding_matches
        targets = {'projects/participant-project', 'projects/' + '9' * 11}  # Project番号の代わり（12桁は配布時の混入検査に触れる）
        self.assertTrue(binding_matches({'target': 'projects/participant-project',
                                         'product': 'GEMINI_IN_BIGQUERY'}, targets))
        self.assertFalse(binding_matches({'target': 'projects/other', 'product': 'GEMINI_IN_BIGQUERY'}, targets))
        self.assertFalse(binding_matches({'target': 'projects/participant-project',
                                          'product': 'SOMETHING_ELSE'}, targets))
        self.assertFalse(binding_matches(None, targets))

    def test_r5_present_but_disabled_settings_report_not_ready(self):
        state = {'project_id': 'participant-project', 'settings': {},
                 'completed': list(setup_module.STEPS)}
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'inspect_cloud') as inspect_cloud:
            inspect_cloud.return_value = {'stage_ready': True, 'data_ready': True,
                                          'observability_ready': False, 'ready': False,
                                          'observability_detail': {'setting_enabled': False,
                                                                   'binding_ready': False}}
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments())
        self.assertEqual(code, 1)
        self.assertIn('Trace・Metrics が有効になっていません', out.getvalue())
        self.assertIn('binding', out.getvalue())

    def test_r6_diagnosis_runs_again_on_resume(self):
        # 観測設定で失敗したあとの再開。診断済みでも省略しない。
        state = {'project_id': 'participant-project', 'settings': {},
                 'completed': ['configure', 'enable-apis', 'bootstrap', 'diagnose']}
        client = mock.MagicMock()
        client.project = 'participant-project'
        session = mock.MagicMock()
        session.return_value.__enter__.return_value = client
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'client_session', session), \
             mock.patch.object(setup_module, 'preflight', return_value={'can_attempt_stage': False}) as preflight, \
             mock.patch.object(setup_module, 'Observability') as observability, \
             mock.patch.object(setup_module, 'write_state'):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments(project=None))
        self.assertEqual(code, 2)
        preflight.assert_called()
        observability.assert_not_called()

    def test_r6_final_verification_runs_after_all_steps(self):
        state = {'project_id': 'participant-project', 'settings': {},
                 'completed': ['configure', 'enable-apis', 'bootstrap', 'diagnose']}
        client = mock.MagicMock()
        client.project = 'participant-project'
        session = mock.MagicMock()
        session.return_value.__enter__.return_value = client
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'client_session', session), \
             mock.patch.object(setup_module, 'preflight', return_value={'can_attempt_stage': True}), \
             mock.patch.object(setup_module, 'Observability'), \
             mock.patch.object(setup_module, 'exclusive_lock'), \
             mock.patch.object(setup_module, 'write_state'), \
             mock.patch.object(setup_module, 'inspect_cloud') as inspect_cloud:
            inspect_cloud.return_value = {'stage_ready': True, 'data_ready': False,
                                          'observability_ready': True, 'ready': False,
                                          'observability_detail': {}}
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = setup_module.run(arguments(project=None))
        # 工程は全部走ったが、最後の読み取りで不足が見つかれば成功にしない。
        inspect_cloud.assert_called_once()
        self.assertEqual(code, 1)

    def test_r7_state_records_target_settings(self):
        recorded = {}
        with mock.patch.object(setup_module, 'save', side_effect=lambda path, value: recorded.update(value)):
            setup_module.write_state('participant-project', ['configure'], {'bigquery_location': 'US'})
        self.assertEqual(recorded['settings'], {'bigquery_location': 'US'})
        self.assertEqual(recorded['project_id'], 'participant-project')

    def test_r7_changed_location_is_detected_without_arguments(self):
        # 配置引数を省略しても、進捗に残した設定と現在値の差を検出する。
        state = {'project_id': 'participant-project', 'settings': {'bigquery_location': 'US',
                 'data_agent_location': None, 'runtime_location': None, 'catalog_location': None},
                 'completed': ['configure']}
        existing = {'project_id': 'participant-project', 'bigquery_location': 'EU'}
        a, b, c = patched(state, existing)
        with a, b, c:
            with self.assertRaisesRegex(ValueError, '配置設定が違います'):
                setup_module.run(arguments(project=None))

    def test_r7_old_state_without_settings_is_re_checked(self):
        state = {'project_id': 'participant-project', 'completed': ['configure']}
        existing = {'project_id': 'participant-project'}
        a, b, c = patched(state, existing)
        self.assertIsNone(setup_module.compare_recorded_settings(state, existing))
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=False):
            out = io.StringIO()
            with redirect_stdout(out):
                setup_module.run(arguments(project=None))
        self.assertIn('省略せず状態を読み直します', out.getvalue())


class FreshProjectRegressionTests(unittest.TestCase):
    def test_invalid_id_stops_before_confirmation_and_state_write(self):
        a, b, c = patched()
        with a, b, c, mock.patch.object(setup_module, 'confirm') as confirm, \
             mock.patch.object(setup_module, 'write_state') as write, \
             mock.patch.object(setup_module, 'enable_apis') as enable:
            with self.assertRaisesRegex(ValueError, 'Project ID'):
                setup_module.run(arguments(project='YOUR_PROJECT_ID'))
        confirm.assert_not_called()
        write.assert_not_called()
        enable.assert_not_called()

    def test_old_api_completion_is_retried_and_failure_remains_retryable(self):
        state = {'project_id': 'participant-project', 'completed': ['configure', 'enable-apis'],
                 'enabled_apis': []}
        a, b, c = patched(state, {'project_id': 'participant-project'})
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'enable_apis', side_effect=CloudError('retry')) as enable, \
             mock.patch.object(setup_module, 'write_state') as write, \
             mock.patch.object(setup_module, 'bootstrap') as bootstrap, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(setup_module.run(arguments()), 2)
        enable.assert_called_once_with('participant-project', setup_module.planned_apis(), 'participant-account')
        self.assertEqual(write.call_args.args[1], ['configure'])
        bootstrap.assert_not_called()

    def test_legacy_api_state_resumes_after_enabling_new_api(self):
        state = {'project_id': 'participant-project', 'completed': ['configure', 'enable-apis']}
        a, b, c = patched({}, {'project_id': 'participant-project'})
        client = mock.MagicMock(project='participant-project')
        with a, b, c, mock.patch.object(setup_module, 'read_state', return_value=state), \
             mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'enable_apis', return_value=setup_module.planned_apis()) as enable, \
             mock.patch.object(setup_module, 'client_session') as session, \
             mock.patch.object(setup_module, 'bootstrap', side_effect=CloudError('stop after API')) as bootstrap, \
             mock.patch.object(setup_module, 'save') as save, redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            session.return_value.__enter__.return_value = client
            self.assertEqual(setup_module.run(arguments()), 2)
        enable.assert_called_once()
        bootstrap.assert_called_once()
        self.assertEqual(save.call_args.args[1]['enabled_apis'], setup_module.planned_apis())
        self.assertEqual(save.call_args.args[1]['completed'], ['configure', 'enable-apis'])

    def test_invalid_empty_state_is_preserved_and_corrected_without_manual_move(self):
        state = {'project_id': 'YOUR_PROJECT_ID', 'completed': [],
                 'settings': dict.fromkeys(setup_module.LOCATION_KEYS)}
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'state.json'
            original = json.dumps(state)
            path.write_text(original)
            with mock.patch.object(setup_module, 'STATE', path), \
                 mock.patch.object(setup_module, 'config', return_value={}), \
                 mock.patch.object(setup_module, 'current_account', return_value='participant-account'), \
                 mock.patch.object(setup_module, 'confirm', return_value=True), \
                 mock.patch.object(setup_module, 'configure_project'), \
                 mock.patch.object(setup_module, 'enable_apis', side_effect=CloudError('stop')), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(setup_module.run(arguments()), 2)
            self.assertEqual(json.loads(path.read_text())['project_id'], 'participant-project')
            backups = list(path.parent.glob('state.invalid-project-*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), original)

    def test_declining_recovery_preserves_state(self):
        state = {'project_id': 'YOUR_PROJECT_ID', 'completed': [],
                 'settings': dict.fromkeys(setup_module.LOCATION_KEYS)}
        a, b, c = patched(state)
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=False), \
             mock.patch.object(setup_module, 'STATE') as path, \
             mock.patch.object(setup_module, 'write_state') as write, redirect_stdout(io.StringIO()):
            self.assertEqual(setup_module.run(arguments()), 1)
        path.rename.assert_not_called()
        write.assert_not_called()

    def test_existing_work_and_valid_project_mismatches_remain_protected(self):
        for project, completed, existing in (
                ('YOUR_PROJECT_ID', ['configure'], {}),
                ('earlier-project', [], {}),
                ('YOUR_PROJECT_ID', [], {'project_id': 'earlier-project'})):
            state = {'project_id': project, 'completed': completed,
                     'settings': dict.fromkeys(setup_module.LOCATION_KEYS)}
            a, b, c = patched(state, existing)
            with a, b, c, mock.patch.object(setup_module, 'confirm') as confirm, \
                 mock.patch.object(setup_module, 'write_state') as write:
                with self.assertRaises(ValueError):
                    setup_module.run(arguments())
            confirm.assert_not_called()
            write.assert_not_called()


class VerifiedLocationDefaultTests(unittest.TestCase):
    """配置の既定値は新規の参加者だけに使い、既存の設定を上書きしない。"""

    def test_new_participant_gets_verified_locations(self):
        values = setup_module.requested_locations(arguments(), {'project_id': None})
        self.assertEqual(values, setup_module.VERIFIED_LOCATIONS)

    def test_explicit_location_wins_over_default(self):
        values = setup_module.requested_locations(arguments(bigquery_location='EU'), {})
        self.assertEqual(values['bigquery_location'], 'EU')

    def test_existing_project_gets_no_defaults(self):
        existing = {'project_id': 'participant-project', 'bigquery_location': 'US'}
        self.assertEqual(setup_module.requested_locations(arguments(), existing), {})
        self.assertFalse(setup_module.configure_needed(arguments(), existing, 'participant-project'))

    def test_new_participant_config_is_written_with_defaults(self):
        a, b, c = patched()
        with a, b, c, mock.patch.object(setup_module, 'confirm', return_value=True), \
             mock.patch.object(setup_module, 'configure_project') as configure, \
             mock.patch.object(setup_module, 'enable_apis', side_effect=CloudError('stop')), \
             mock.patch.object(setup_module, 'write_state'), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            setup_module.run(arguments())
        configure.assert_called_once_with('participant-project', **setup_module.VERIFIED_LOCATIONS)


class PlaceholderProjectTests(unittest.TestCase):
    def test_unedited_placeholder_says_what_to_replace(self):
        from workshop.settings import validate_project
        with self.assertRaisesRegex(ValueError, 'YOUR_PROJECT_ID を、自分の Project ID'):
            validate_project('YOUR_PROJECT_ID')


class DocumentationConsistencyTests(unittest.TestCase):
    """手順書の表示と実装の食い違いを防ぐ。

    固定するのは成功メッセージなど、参加者が成否を判断する文字列と、事実に反する記述の
    禁止だけ。説明文の言い回しは読みやすさのために自由に変えられるようにしておく。
    """

    @staticmethod
    def sources():
        root = pathlib.Path(__file__).resolve().parents[1]
        return ((root/'workshop/setup.py').read_text(encoding='utf-8'),
                (root/'docs/labs/preparation.md').read_text(encoding='utf-8'))

    def test_success_message_matches_the_document(self):
        code, doc = self.sources()
        for message in ('事前準備が完了しました。', '準備はできています。'):
            self.assertIn(message, code, message + ' が実装にない')
            self.assertIn(message, doc, message + ' が手順書にない')

    def test_document_does_not_claim_completed_steps_are_never_redone(self):
        """事実に反する記述だけを禁じる。言い回しは固定しない。

        診断と最終確認は毎回やり直すため、この一文は誤り。表現の改善を妨げないよう、
        肯定形での文言一致は検査しない。
        """
        _, doc = self.sources()
        self.assertNotIn('完了済みの工程はやり直しません', doc)

    def test_bootstrap_label_does_not_claim_catalog(self):
        # bootstrap() が作るのは BigQuery のテーブルと View のみ。
        self.assertNotIn('Catalog', setup_module.LABELS['bootstrap'])
        root = pathlib.Path(__file__).resolve().parents[1]
        body = (root/'workshop/cloud.py').read_text(encoding='utf-8')
        start = body.index('def bootstrap(')
        self.assertNotIn('catalog', body[start:body.index('def verify(', start)].lower())

    def test_document_does_not_claim_setup_creates_catalog_entries(self):
        """setup が Catalog Entry を作るという誤った記述を禁じる。説明の書き方は自由。"""
        _, doc = self.sources()
        head = doc[:doc.index('### BigQuery Console で作成結果を確認する')]
        self.assertNotIn('Catalog の EntryGroup・EntryType・Entry を作ります', head)


if __name__ == '__main__':
    unittest.main()

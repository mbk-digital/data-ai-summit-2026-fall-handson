"""事前準備をまとめて実行する。Project設定・API有効化・教材データ構築・診断・観測設定。

参加者が叩くコマンド数を減らすための集約で、各工程の中身は既存の実装をそのまま呼ぶ。
クラウドを変更する前に対象と実施内容を表示し、確認を取る。途中で失敗しても、完了済みの
工程と再開方法が分かるように状態を残す。

進捗ファイルは履歴であって現状の証明ではない。完了判定と再開判定では、進捗・現在の設定・
引数を照合し、クラウドの実際の状態を読み取って確認する。
"""
import argparse
import json
import subprocess
import sys
import uuid

from .cloud import (CloudError, bootstrap, client_session, current_account, enable_apis,
                    preflight, save, stage_requirements, timestamp, verify)
from .observability import Observability, exclusive_lock
from .settings import ROOT, config, configure_project, validate_project

STATE = ROOT/'artifacts/setup/state.json'
BOOTSTRAP_STAGE = 'lab1-catalog'
# #2 まで通す権限・APIの段階。モデル実行・Trace・Monitoringを含み、費用照合専用の
# bigquery.jobs.listAll は含まない。集約後もこの診断を本線に残す。
DIAGNOSE_STAGE = 'lab2'
LOCATION_KEYS = ('bigquery_location', 'data_agent_location', 'runtime_location', 'catalog_location')
# 教材を検証した配置。新規の参加者だけに使い、既存のProject設定には当てはめない。
VERIFIED_LOCATIONS = {'bigquery_location': 'US', 'data_agent_location': 'us',
                      'runtime_location': 'us-central1', 'catalog_location': 'us'}
STEPS = ('configure', 'enable-apis', 'bootstrap', 'diagnose', 'observability')
LABELS = {'configure': 'Project 設定をこのフォルダに保存（クラウドは変更しません）',
          'enable-apis': '必要な API を有効化',
          'bootstrap': '教材データを作成（BigQuery の 5 テーブルと View）',
          'diagnose': '実習 2 までに必要な権限を確認',
          'observability': 'AI の実行履歴を残す設定を有効化'}
# 実行中の表示と、失敗時の「完了した工程」に使う短い名前。
PROGRESS = {'configure': 'Project 設定を保存しています',
            'enable-apis': 'API を有効化しています',
            'bootstrap': '教材データを作成しています',
            'diagnose': '権限を確認しています',
            'observability': 'AI の実行履歴を残す設定を有効化しています'}
SHORT = {'configure': 'Project 設定の保存', 'enable-apis': 'API の有効化', 'bootstrap': '教材データの作成',
         'diagnose': '権限の確認', 'observability': '実行履歴の設定'}


def read_state():
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding='utf-8'))


def target_settings(existing):
    return {key: existing.get(key) for key in LOCATION_KEYS}


def write_state(project, done, settings):
    save(STATE, {'project_id': project, 'settings': settings, 'completed': list(done),
                 'enabled_apis': planned_apis() if 'enable-apis' in done else [],
                 'updated_at': timestamp()})


def compare_recorded_settings(state, existing):
    """進捗に残した対象設定と現在値を照合する。

    情報が無い進捗（旧形式）は「照合できない」として扱い、確認を省かない。
    """
    recorded = state.get('settings')
    if recorded is None:
        return None
    differences = [key + '：進捗=' + str(recorded.get(key)) + ' / 現在=' + str(existing.get(key))
                   for key in LOCATION_KEYS if recorded.get(key) != existing.get(key)]
    if differences:
        raise ValueError('前回の準備と配置設定が違います。クラウドは変更していません。\n  '
                         + '\n  '.join(differences))
    return True


def planned_apis():
    apis, _ = stage_requirements(DIAGNOSE_STAGE)
    # 権限診断の testIamPermissions が利用するAPIも、新規Projectでは明示的に有効化する。
    return sorted(set(apis) | {'cloudresourcemanager.googleapis.com'})


def requested_locations(args, existing=None):
    """引数で指定した配置。existing を渡すと、新規の参加者には検証済みの既定値を補う。"""
    values = {key: getattr(args, key) for key in LOCATION_KEYS if getattr(args, key)}
    if existing is not None and not existing.get('project_id'):
        for key, value in VERIFIED_LOCATIONS.items():
            if not existing.get(key):
                values.setdefault(key, value)
    return values


def resolve_target(args, state):
    """進捗・現在の設定・引数のProjectと配置を照合する。不一致なら変更前に止める。

    進捗ファイルが無くても既存設定は確認する。旧手順で準備した参加者を上書きしないため。
    """
    existing = config()
    current, recorded = existing.get('project_id'), state.get('project_id')
    project = args.project or recorded or current
    if project is None:
        raise ValueError('--project で自分のProject IDを指定してください。')
    validate_project(project)
    for label, value in (('進捗記録', recorded), ('現在のローカル設定', current)):
        if value and value != project:
            raise ValueError(label + 'のProject（' + value + '）と、今回の指定（' + project + '）が違います。'
                             'クラウドは変更していません。同じProjectで再開するか、新しい教材フォルダを使ってください。')
    conflicts = [key + '：既存=' + str(existing.get(key)) + ' / 指定=' + value
                 for key, value in requested_locations(args).items()
                 if existing.get(key) and existing[key] != value]
    if conflicts:
        raise ValueError('既存の配置設定と指定が違います。上書きしません。クラウドは変更していません。\n  '
                         + '\n  '.join(conflicts))
    return project, existing


def configure_needed(args, existing, project):
    if existing.get('project_id') != project:
        return True
    return any(existing.get(key) != value for key, value in requested_locations(args, existing).items())


def inspect_cloud(project, account):
    """クラウドの実際の状態を読み取る。リソースは変更しない。"""
    with client_session(account) as client:
        if client.project != project:
            raise CloudError('確認したProject（' + project + '）と接続先（' + client.project + '）が違います。'
                             '設定を確認してから実行し直してください。')
        report = preflight(client, DIAGNOSE_STAGE, quiet=True)
        ready = bool(report.get('can_attempt_stage'))
        try:
            verify(client, quiet=True)
            data_ready = True
        except CloudError as exc:
            data_ready, ready = False, False
            print('教材データを確認できません: ' + str(exc))
        # 設定が存在するだけでは有効とみなさない。有効フラグと対象Projectへのbindingを見る。
        observed = Observability(client.cfg, account).verify()
        return {'stage_ready': bool(report.get('can_attempt_stage')), 'data_ready': data_ready,
                'observability_ready': observed['ready'],
                'observability_detail': observed,
                'ready': ready and observed['ready']}


def console_links(project):
    return ('  BigQuery（テーブルと View）    https://console.cloud.google.com/bigquery?project=' + project,
            '  BigQuery Agents（Monitoring）  https://console.cloud.google.com/bigquery/agents_hub;agentsHubTab=Monitoring;project=' + project)


def report_state(project, account):
    """クラウドの実際の状態を読み取り、1 項目 1 行で表示する。進捗ファイルは完了の証明にしない。"""
    result = inspect_cloud(project, account)
    detail = result.get('observability_detail', {})
    rows = (('教材データ（5 テーブル・View）', result['data_ready']),
            ('実習 2 までの権限・API', result['stage_ready']),
            ('AI の実行履歴を残す設定', result.get('observability_ready')))
    for label, ok in rows:
        print(('  OK   ' if ok else '  未完 ') + label)
    if not result.get('observability_ready'):
        # 有効フラグと Project への binding のどちらが欠けているかを分けて示す。
        if not detail.get('setting_enabled'):
            print('         Trace・Metrics が有効になっていません')
        if not detail.get('binding_ready'):
            print('         対象 Project への適用（binding）がありません')
    if result['ready']:
        return 0
    return 1


def report_shortage():
    print('', file=sys.stderr)
    print('不足があります。`未完` の行と対象 Project を主催者へ伝えてください。', file=sys.stderr)


def show_plan(project, account, done, remaining):
    print('対象Project: ' + project)
    print('対象アカウント: ' + account)
    print('')
    if done:
        print('完了済みの工程（今回は実行しません）:')
        for step in STEPS:
            if step in done:
                print('  済 ' + LABELS[step])
    print('次の ' + str(len(remaining)) + ' つを行います:')
    for step in remaining:
        count = '（' + str(len(planned_apis())) + ' 件）' if step == 'enable-apis' else ''
        print('  ' + str(STEPS.index(step) + 1) + '. ' + LABELS[step] + count)
    print('')
    print('参加者の業務定義・既存設定は上書きしません。')
    print('Google Cloud の利用料金が発生する場合があります。')


def progress(step):
    """工程の開始を表示する。完了は同じ行に「完了」を続けて書く。"""
    extra = '（' + str(len(planned_apis())) + ' 件）' if step == 'enable-apis' else ''
    print('[' + str(STEPS.index(step) + 1) + '/' + str(len(STEPS)) + '] ' + PROGRESS[step] + extra + '…',
          end='', flush=True)


def confirm(assume_yes):
    if assume_yes:
        print('--yes が指定されているため、確認せず実行します。')
        return True
    if not sys.stdin.isatty():
        raise CloudError('確認を取れません。内容を確認したうえで --yes を付けて実行してください。')
    return input('この内容で実行しますか。実行する場合は y を入力してください [y/N]: ').strip().lower() == 'y'


def invalid_unstarted_state(state):
    """旧版が入力検証前に保存した、未着手の不正ID記録だけを救済する。"""
    try:
        validate_project(state.get('project_id'))
    except ValueError:
        return (isinstance(state.get('project_id'), str)
                and state.get('completed') == []
                and state.get('settings') == dict.fromkeys(LOCATION_KEYS))
    return False


def run(args):
    state = read_state()
    recover = invalid_unstarted_state(state) and not config().get('project_id')
    if recover:
        # 有効な指定がなければresolve_targetで停止し、元の記録は保持する。
        state = {}
    project, existing = resolve_target(args, state)
    compared = compare_recorded_settings(state, existing)
    account = current_account(args.account)
    done = [s for s in STEPS if s in set(state.get('completed', []))]
    if 'enable-apis' in done and not set(planned_apis()).issubset(state.get('enabled_apis', [])):
        done.remove('enable-apis')
        print('教材の必要APIが更新されたか、有効化したAPIの記録がありません。確認後にAPI有効化を再実行します。')
    remaining = [s for s in STEPS if s not in done]
    if 'configure' in done and configure_needed(args, existing, project):
        raise ValueError('進捗では設定済みですが、現在のローカル設定が指定と違います。'
                         'クラウドは変更していません。設定を確認してください。')
    if compared is None and done:
        print('前回の進捗に対象設定が記録されていません。省略せず状態を読み直します。')
    if not remaining:
        print('対象Project: ' + project)
        print('対象アカウント: ' + account)
        print('準備済みの記録があります。現在の状態を確認します。')
        code = report_state(project, account)
        if code == 0:
            print('')
            print('準備はできています。次の 2 つの画面で確認できます。')
            print('\n'.join(console_links(project)))
        else:
            report_shortage()
        return code

    show_plan(project, account, done, remaining)
    if not confirm(args.yes):
        print('実行を中止しました。クラウドは変更していません。')
        return 1
    print('')

    if recover and STATE.exists():
        backup = STATE.with_name('state.invalid-project-' + uuid.uuid4().hex + '.json')
        STATE.rename(backup)
        print('未着手の不正なProject記録を退避しました: ' + str(backup))

    completed = list(done)
    settings = target_settings(existing)
    try:
        if 'configure' in remaining:
            progress('configure')
            if configure_needed(args, existing, project):
                configure_project(project, **requested_locations(args, existing))
                print(' 完了')
            else:
                print(' 保存済みの設定をそのまま使います')
            settings = target_settings(config())
            completed.append('configure')
            write_state(project, completed, settings)

        if 'enable-apis' in remaining:
            progress('enable-apis')
            enable_apis(project, planned_apis(), account)
            print(' 完了')
            completed.append('enable-apis')
            write_state(project, completed, settings)

        with client_session(account) as client:
            if client.project != project:
                raise CloudError('確認したProject（' + project + '）と接続先（' + client.project + '）が違います。'
                                 'クラウドは変更していません。設定を確認してから実行し直してください。')
            if 'bootstrap' in remaining:
                progress('bootstrap')
                bootstrap(client, BOOTSTRAP_STAGE, quiet=True)
                print(' 完了（集計結果も確認しました）')
                completed.append('bootstrap')
                write_state(project, completed, settings)
            # 診断は読み取りだけなので、再開時も省略しない。前回の完了後に権限や
            # リソースが変わっている場合があるため。
            progress('diagnose')
            if not preflight(client, DIAGNOSE_STAGE, quiet=True).get('can_attempt_stage'):
                raise CloudError('実習 2 に必要な権限・API が不足しています。'
                                 '上に表示された不足を主催者へ伝えてください。')
            print(' 完了')
            if 'diagnose' in remaining:
                completed.append('diagnose')
                write_state(project, completed, settings)
            if 'observability' in remaining:
                progress('observability')
                with exclusive_lock():
                    Observability(client.cfg, account).enable(quiet=True)
                print(' 完了')
                completed.append('observability')
                write_state(project, completed, settings)
    except (CloudError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        write_state(project, completed, settings)
        print('', file=sys.stderr)
        print('エラー: ' + str(exc), file=sys.stderr)
        print('完了した工程: ' + ('、'.join(SHORT[s] for s in completed) or 'なし'), file=sys.stderr)
        print('記録: ' + str(STATE), file=sys.stderr)
        print('原因を解消してから同じコマンドを実行すると、残りの工程から再開します。', file=sys.stderr)
        return 2

    # 完了フラグではなく、最後に全体の状態を読み取って確認する。
    print('')
    print('最終確認:')
    code = report_state(project, account)
    if code == 0:
        print('')
        print('事前準備が完了しました。次の 2 つの画面を開いて確認してください。')
        print('\n'.join(console_links(project)))
    else:
        report_shortage()
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', help='自分のProject ID。2回目以降は省略できます')
    parser.add_argument('--account', help='省略時はgcloudの現在のアカウントを取得して使います')
    # 配置は省略時、新規の参加者に限り VERIFIED_LOCATIONS を使う。
    parser.add_argument('--bigquery-location')
    parser.add_argument('--data-agent-location')
    parser.add_argument('--runtime-location')
    parser.add_argument('--catalog-location')
    parser.add_argument('--yes', action='store_true', help='確認を省略して実行する')
    args = parser.parse_args()
    try:
        return run(args)
    except (CloudError, ValueError, OSError) as exc:
        print('エラー: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

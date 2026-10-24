"""Plan/delete only recorded, unchanged workshop BigQuery data (no project deletion)."""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from .cloud import Client, CloudError, dataset_path, ensure_owned_dataset, table_rows, save, timestamp, validate_cloud_config
from .bigquery import render, expected_view_query, expected_table_fields
from .data import fixture
from .settings import ROOT, config


def plan(client, root=ROOT, deferred_graphs=()):
    base = dataset_path(client)
    resource = client.request('GET', base, missing_ok=True)
    if resource is None:
        return []
    ensure_owned_dataset(resource, client.location)
    manifest_path = root/'artifacts/cloud-resources.json'
    if not manifest_path.exists():
        raise CloudError('削除台帳がありません。artifacts/のバックアップを戻すか、Cloud Consoleで対象を個別確認してください。ラベルから自動再構築・削除はしません。')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('project_id') != client.project or manifest.get('dataset_id') != client.cfg['dataset_id']:
        raise CloudError('台帳のProject/Datasetが一致しません。削除しません。')
    graphs = client.query('SELECT property_graph_name FROM `%s.%s.INFORMATION_SCHEMA.PROPERTY_GRAPHS`' % (client.project, client.cfg['dataset_id']))
    if any(g["property_graph_name"] not in deferred_graphs for g in graphs):
        raise CloudError('Property Graphがあります。先にGraphの保護付き片付けを行ってください。テーブルは削除しません。')
    allowed = set(manifest.get('tables', [])) | set(manifest.get('views', []))
    expected = set(fixture()) | {'v_revenue_events'}
    if not allowed.issubset(expected):
        raise CloudError('台帳に未対応のリソースがあります。削除しません。')
    listing = client.request('GET', base+'/tables?maxResults=1000')
    existing = {item['tableReference']['tableId'] for item in listing.get('tables', [])}
    if listing.get('nextPageToken') or not existing.issubset(allowed):
        raise CloudError('台帳外のテーブルがあります。追加データを保護するため削除しません。')
    ddl = (ROOT / 'data/bigquery_schema.sql').read_text(encoding='utf-8')
    for table, rows in fixture().items():
        if table not in existing:
            continue
        metadata = client.request('GET', base+'/tables/'+table)
        fields=expected_table_fields(table)
        if metadata.get('schema',{}).get('fields')!=fields or metadata.get('type')!='TABLE' or any(metadata.get(k) for k in ('labels','description','friendlyName','expirationTime','timePartitioning','rangePartitioning','clustering','encryptionConfiguration')):
            raise CloudError('変更されたテーブル定義を保護するため削除しません: '+table)
        if table_rows(client, table) != sorted(rows):
            raise CloudError('教材から変更されたデータがあります: '+table+'。必要な内容を退避して個別に管理してください。')
    if 'v_revenue_events' in existing:
        actual = client.request('GET', base+'/tables/v_revenue_events')
        expected_sql=expected_view_query(client.cfg)
        if actual.get('type') != 'VIEW' or actual.get('view',{}).get('query','').strip().rstrip(';').strip()!=expected_sql:
            raise CloudError('変更されたViewを保護するため削除しません。')
    return [base+'/tables/'+name for name in sorted(existing)] + [base]


def run(client, execute=False, root=ROOT, deferred_graphs=()):
    # Deferral is only for the unified read-only plan after graph ownership checks.
    paths = plan(client, root, () if execute else deferred_graphs)
    record = {'project_id': client.project, 'dataset_id': client.cfg['dataset_id'],
              'checked_at': timestamp(), 'planned': paths, 'deleted': []}
    save(root/'artifacts/bigquery-cleanup.json', record)
    for path in paths:
        print(('削除: ' if execute else '削除候補: ') + path, flush=True)
        if execute:
            # Re-check remaining resources; never defer a graph during deletion.
            if path not in plan(client, root):
                continue
            client.request('DELETE', path, missing_ok=True)
            record['deleted'].append(path)
            save(root/'artifacts/bigquery-cleanup.json', record)
    return paths


def main():
    parser=argparse.ArgumentParser(description='教材BigQueryの削除計画。既定では削除しない。')
    parser.add_argument('--account')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    try:
        cfg=config(require_project=True)
        validate_cloud_config(cfg)
        client=Client(cfg,args.account)
        print('対象: '+client.project+'.'+cfg['dataset_id'],flush=True)
        run(client, args.execute)
        print('Project、ローカル編集・回答記録は保持します。',flush=True)
        return 0
    except (CloudError, ValueError, OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':
    sys.exit(main())

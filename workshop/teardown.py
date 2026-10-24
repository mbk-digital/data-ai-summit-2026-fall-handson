"""One entry point for guarded workshop cleanup; read-only unless --execute."""
import argparse
from contextlib import ExitStack
import fcntl
import sys
from . import cleanup
from .catalog import Catalog, cleanup_catalog
from .cloud import Client, CloudError, save, timestamp, validate_cloud_config
from .data_agent import AgentClient
from .semantics_poc import Poc
from .settings import ROOT, config
from .observability import Observability


def orchestrate(poc, catalog, agent, client, execute=False, root=ROOT, observability=None):
    print('削除順: Graph → Catalog → Data Agent → BigQuery', flush=True)
    print('全対象を事前検査しています。この段階では削除しません。', flush=True)
    poc.cleanup(False)
    if catalog is not None:
        cleanup_catalog(catalog, False)
    agent_count = agent.teardown(False)
    if observability is not None:
        observability.cleanup(False)
    # Poc.cleanup has validated the exact resource and its current snapshot.
    graph_key = 'graph:' + client.project + '.' + client.cfg['dataset_id'] + '.ec_semantics_poc'
    record = poc.ledger.get(graph_key, {})
    deferred = ['ec_semantics_poc'] if record.get('state') in {'present', 'delete_requested'} else []
    paths = cleanup.run(client, False, root, deferred_graphs=deferred)
    report = {'project_id': client.project, 'dataset_id': client.cfg['dataset_id'],
              'checked_at': timestamp(), 'agent_count': agent_count, 'bigquery_paths': paths,
              'order': ['Graph', 'Catalog', 'Data Agent', 'BigQuery'],
              'state': 'planned', 'completed_stages': []}
    if observability is not None:
        report['order'].append('Data Agent Observability')
        print('BigQueryの後に教材のObservability設定とbindingを削除します。保存済みTraceは保持します。')
    destination = root/'artifacts/teardown.json'
    save(destination, report)
    if execute:
        report['state'] = 'executing'
        save(destination, report)
        try:
            # Each component revalidates before deleting; any error stops later stages.
            poc.cleanup(True)
            report['completed_stages'].append('Graph / PoC Catalog')
            save(destination, report)
            if catalog is not None:
                cleanup_catalog(catalog, True)
            report['completed_stages'].append('Catalog')
            save(destination, report)
            agent.teardown(True)
            report['completed_stages'].append('Data Agent')
            save(destination, report)
            cleanup.run(client, True, root)
            report['completed_stages'].append('BigQuery')
            if observability is not None:
                observability.cleanup(True)
                report['completed_stages'].append('Data Agent Observability')
            report['state'] = 'completed'
        except Exception as exc:
            report.update(state='interrupted', error=str(exc))
            raise
        finally:
            save(destination, report)
    print('Project、参加者の編集、回答記録は保持します。', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--account')
    args = parser.parse_args()
    try:
        cfg = config(require_project=True)
        validate_cloud_config(cfg)
        print('対象Project: ' + cfg['project_id'] + ' / Dataset: ' + cfg['dataset_id'], flush=True)
        with ExitStack() as stack:
            # Share the locks used by the Catalog and Graph commands.
            lock_path = ROOT/'artifacts/semantics-poc/.lock'
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(lock_path.open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            poc = Poc(cfg, args.account)
            catalog = None
            if cfg.get('catalog_location'):
                catalog = Catalog(cfg, args.account)
                stack.enter_context(catalog.locked())
            elif any((ROOT/'artifacts/catalog'/cfg['project_id']/cfg['dataset_id']).glob('*/resources.json')):
                raise CloudError('Catalogの台帳があります。catalog_locationを復元してから片付けてください。')
            obs_lock_path = ROOT/'artifacts/observability/.lock'
            obs_lock_path.parent.mkdir(parents=True, exist_ok=True)
            obs_lock = stack.enter_context(obs_lock_path.open('a'))
            fcntl.flock(obs_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observability = Observability(cfg, args.account)
            orchestrate(poc, catalog, AgentClient(cfg, args.account), Client(cfg, args.account), args.execute, observability=observability)
        return 0
    except (CloudError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

"""Stable Catalog entry, guarded updates, immutable retrieval receipts for lab1."""
import argparse
import copy
import contextlib
import fcntl
import hashlib
import json
import re
import uuid
from pathlib import Path
from .resource_state import compare as compare_resource, equivalent
from .cloud import Client, CloudError, LABELS, save, timestamp, validate_cloud_config
from .settings import ROOT, config
from .semantics_poc import API, Poc


def concept(document):
    """Parse the deliberately small workshop OKF header (not a general YAML parser)."""
    parts = document.split('\n---\n', 1)
    if not document.startswith('---\n') or len(parts) != 2:
        raise ValueError('OKFの先頭の---と末尾の---を保持してください。')
    fields = {}
    for line in parts[0][4:].splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(':')
        if not sep or key not in {'type', 'title', 'description'} or key in fields:
            raise ValueError('この教材のOKFヘッダーはtype/title/descriptionの単一行形式です。')
        fields[key] = value.strip()
    if fields.get('type') != 'Metric' or not fields.get('title') or not parts[1].strip():
        raise ValueError('OKFにはtype: Metric、titleと本文が必要です。')
    return fields, parts[1].strip()


def as_document(rules):
    # Legacy participant files are wrapped in memory, never overwritten.
    if rules.startswith('---\n'):
        concept(rules)
        return rules
    return '---\ntype: Metric\ntitle: EC売上・粗利益の業務定義\ndescription: ハンズオンの共通業務定義\n---\n\n' + rules


class Catalog(Poc):
    def __init__(self, cfg, account=None, root=ROOT):
        validate_cloud_config(cfg)
        location = cfg.get('catalog_location')
        if location not in {'us', 'eu', 'us-central1', 'asia-northeast1'}:
            raise ValueError('configure --catalog-location us などでCatalogの配置を明示してください。')
        self.cfg, self.account, self.root = cfg, account, Path(root)
        self.c = Client(cfg, account)
        self.parent = 'projects/%s/locations/%s' % (self.c.project, location)
        self.folder = self.root / 'artifacts/catalog' / self.c.project / cfg['dataset_id'] / location
        self.ledger_path = self.folder / 'resources.json'
        self.ledger = {}

    @contextlib.contextmanager
    def locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}
            yield

    def persist(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.ledger_path.with_suffix('.tmp')
        save(temp, self.ledger)
        temp.replace(self.ledger_path)

    def overview(self, entry):
        official = self.c.request('GET', API + 'projects/dataplex-types/locations/global/aspectTypes/overview')
        found = []
        for key, aspect in entry.get('aspects', {}).items():
            name = aspect.get('aspectType', '')
            if name.endswith('/locations/global/aspectTypes/overview'):
                kind = self.c.request('GET', API + name)
                if kind.get('uid') == official.get('uid'):
                    found.append((key, aspect))
        if len(found) != 1:
            raise CloudError('標準overviewを一意に確認できません。ローカル定義では代用しません。')
        return found[0]

    def prepare_console_definition(self, starter_document):
        """Create the editable entry, then accept only a Console edit of its overview."""
        starter_fields, _ = concept(starter_document)
        with self.locked():
            ident = 'ec-lab1-' + hashlib.sha256(self.cfg['dataset_id'].encode()).hexdigest()[:12]
            metadata = {'displayName': 'EC lab1 definitions', 'description': 'workshop_cli:summit_ec_v1:catalog', 'labels': LABELS}
            self.ensure(self.parent + '/entryGroups', ident, metadata, 'entryGroupId')
            kind = self.ensure(self.parent + '/entryTypes', ident, metadata, 'entryTypeId')
            name = self.parent + '/entryGroups/' + ident + '/entries/business-rules'

            def payload(document):
                return {'entryType': kind['name'],
                        'entrySource': {'displayName': starter_fields['title'], 'description': starter_fields.get('description', '')},
                        'aspects': {'dataplex-types.global.overview': {'data': {'content': document, 'contentType': 'MARKDOWN'}}}}

            current = self.get(name)
            previous_document = starter_document
            if current is None:
                current = self.ensure(self.parent + '/entryGroups/' + ident + '/entries', 'business-rules', payload(starter_document), 'entryId')
            else:
                record = self.ledger.get(name, {})
                if record.get('state') != 'present' or not record.get('snapshot'):
                    raise CloudError('Catalog台帳が不足しています。Checkpointで診断してください: ' + name)
                before = record['snapshot']
                before_key, before_aspect = self.overview(before)
                previous_document = before_aspect.get('data', {}).get('content', '')
                current_key, current_aspect = self.overview(current)
                if before_key != current_key:
                    raise CloudError('Catalogのoverview以外も変更されています。参加者の変更を上書きしません。')
                current_document = current_aspect.get('data', {}).get('content', '')
                current_fields, _ = concept(current_document)
                if current_fields != starter_fields:
                    raise CloudError('Catalogでは本文の2か所だけを編集してください。見出し情報の変更は取り込みません。')
                normalized = copy.deepcopy(current)
                normalized_aspect = normalized['aspects'][current_key]
                normalized_aspect['data']['content'] = before_aspect.get('data', {}).get('content', '')
                # Console saves add an empty links list; accept only that exact addition.
                if 'links' not in before_aspect.get('data', {}) and normalized_aspect['data'].get('links') == []:
                    normalized_aspect['data'].pop('links')
                for metadata_key in ('updateTime', 'etag'):
                    if metadata_key in before_aspect:
                        normalized_aspect[metadata_key] = before_aspect[metadata_key]
                    else:
                        normalized_aspect.pop(metadata_key, None)
                if not equivalent(before, normalized):
                    raise CloudError('Catalogのoverview本文以外も変更されています。参加者の変更を上書きしません。')
                if current_document != before_aspect.get('data', {}).get('content', ''):
                    save(self.folder / ('console-edit-' + uuid.uuid4().hex + '.json'),
                         {'entry': name, 'recorded_at': timestamp(), 'before': before, 'after': current})
                    self.ledger[name] = {'state': 'present', 'snapshot': current, 'payload': payload(current_document),
                                         'source': 'participant_console_edit'}
                    self.persist()

            key, aspect = self.overview(current)
            actual_document = aspect.get('data', {}).get('content', '')
            fields, body = concept(actual_document)
            receipt = {'entry': current['name'], 'update_time': current.get('updateTime'), 'retrieved_at': timestamp(),
                       'document_sha256': hashlib.sha256(actual_document.encode()).hexdigest(),
                       'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'document': actual_document,
                       'previous_document': previous_document,
                       'body': body, 'title': fields['title'],
                       'route': '参加者がConsoleで編集し、教材がCatalogから取得してglossaryTermsへ渡す',
                       'entry_response': current}
            save(self.folder / ('receipt-' + uuid.uuid4().hex + '.json'), receipt)
            return receipt

    def sync(self, document):
        fields, body = concept(document)
        with self.locked():
            ident = 'ec-lab1-' + hashlib.sha256(self.cfg['dataset_id'].encode()).hexdigest()[:12]
            metadata = {'displayName': 'EC lab1 definitions', 'description': 'workshop_cli:summit_ec_v1:catalog', 'labels': LABELS}
            self.ensure(self.parent + '/entryGroups', ident, metadata, 'entryGroupId')
            kind = self.ensure(self.parent + '/entryTypes', ident, metadata, 'entryTypeId')
            name = self.parent + '/entryGroups/' + ident + '/entries/business-rules'
            payload = {'entryType': kind['name'], 'entrySource': {'displayName': fields['title'], 'description': fields.get('description', '')},
                       'aspects': {'dataplex-types.global.overview': {'data': {'content': document, 'contentType': 'MARKDOWN'}}}}
            current = self.get(name)
            if current is None:
                current = self.ensure(self.parent + '/entryGroups/' + ident + '/entries', 'business-rules', payload, 'entryId')
            else:
                record = self.ledger.get(name, {})
                if record.get('state') != 'present' or not equivalent(record.get('snapshot'), current):
                    raise CloudError('Catalogが台帳の前回値と異なります。参加者の変更を上書きしません。')
                if record['payload'] != payload:
                    if current != record.get('snapshot'):
                        raise CloudError('定義更新前にworkshop.ledger_refreshで台帳のメタデータ差分を確認・更新してください。')
                    key, aspect = self.overview(current)
                    patch = {'aspects': {key: {'data': payload['aspects']['dataplex-types.global.overview']['data']}}, 'entrySource': payload['entrySource']}
                    self.ledger[name].update(state='update_requested', pending_payload=payload, before=current)
                    self.persist()
                    # API has no Entry etag: exact pre-read guard, not atomic compare-and-swap.
                    from urllib.parse import urlencode
                    response = self.c.request('PATCH', API + name + '?' + urlencode({'updateMask': 'aspects,entry_source', 'aspectKeys': key}), patch)
                    self.ledger[name]['update_response'] = response
                    self.persist()
                    current = self.get(name)
                    _, updated = self.overview(current)
                    if updated['data']['content'] != document:
                        raise CloudError('Catalogの更新結果が一致しません。')
                    self.ledger[name] = {'state': 'present', 'snapshot': current, 'payload': payload}
                    self.persist()
            key, aspect = self.overview(current)
            actual_document = aspect['data'].get('content', '')
            if actual_document != document:
                raise CloudError('Catalogから取得した定義が一致しません。')
            retrieved_fields, retrieved_body = concept(actual_document)
            receipt = {'entry': current['name'], 'update_time': current.get('updateTime'), 'retrieved_at': timestamp(),
                       'document_sha256': hashlib.sha256(actual_document.encode()).hexdigest(),
                       'body_sha256': hashlib.sha256(retrieved_body.encode()).hexdigest(), 'body': retrieved_body,
                       'title': retrieved_fields['title'], 'route': '教材がCatalogから取得しglossaryTermsへ渡す', 'entry_response': current}
            save(self.folder / ('receipt-' + uuid.uuid4().hex + '.json'), receipt)
            return receipt

    def cleanup(self, execute=False):
        with self.locked():
            return cleanup_catalog(self, execute)


def listed(poc, collection, key):
    result, token = [], None
    from urllib.parse import urlencode
    while True:
        response = poc.c.request('GET', API + collection + '?' + urlencode({'pageSize': 100, **({'pageToken': token} if token else {})}))
        result.extend(response.get(key, []))
        token = response.get('nextPageToken')
        if not token:
            return result


def group_children(poc, name, group):
    """Exclude only the verified system entry representing this EntryGroup."""
    children = listed(poc, name + '/entries', 'entries')
    result = []
    from urllib.parse import urlencode
    for child in children:
        links = poc.c.request('GET', API + poc.parent + ':lookupEntryLinks?' + urlencode({'entry': child['name'], 'pageSize': 10}))
        if links.get('entryLinks') or links.get('nextPageToken'):
            raise CloudError('Catalog Entryに関連リンクがあるため削除しません。')
        if child['name'].endswith('/entries/' + name.rsplit('/', 1)[1] + '_entry'):
            actual = poc.get(child['name'])
            kind = poc.c.request('GET', API + actual['entryType'])
            official = poc.c.request('GET', API + 'projects/dataplex-types/locations/global/entryTypes/entrygroup')
            source = actual.get('entrySource', {})
            same_resource = source.get('resource', '').split('/locations/', 1)[-1] == name.split('/locations/', 1)[-1]
            if (kind.get('uid') != official.get('uid') or not same_resource
                    or any(source.get(k) != group.get(k) for k in ('displayName', 'description', 'labels'))
                    or len(actual.get('aspects', {})) != 1
                    or any(a.get('data') for a in actual.get('aspects', {}).values())):
                raise CloudError('EntryGroupの自動生成Entryが変更されています。')
        else:
            result.append(child)
    return result


def cleanup_catalog(poc, execute=False):
    """Only owned entries/groups/types; reject foreign children and foreign type users."""
    names = [n for n in poc.ledger if not n.startswith('graph:') and poc.ledger[n].get('state') != 'deleted']
    present = {}
    for name in names:
        record, actual = poc.ledger[name], poc.get(name)
        if actual is None:
            if record.get('state') not in {'delete_requested', 'present'}:
                raise CloudError('Unconfirmed resource creation: ' + name)
            continue
        if actual != record.get('snapshot') or record.get('state') not in {'present', 'delete_requested'}:
            raise CloudError('Changed/unconfirmed Catalog resource: ' + name)
        present[name] = actual
    canonical = lambda n: n.split('/locations/', 1)[-1]
    allowed_entries = {canonical(v['name']) for k, v in present.items() if '/entries/' in k}
    for name in present:
        if '/entryGroups/' in name and '/entries/' not in name:
            children = group_children(poc, name, present[name])
            if any(canonical(c['name']) not in allowed_entries for c in children):
                raise CloudError('台帳外のCatalog Entry/Linkがあるため削除しません。')
    types = {v['name'].split('/locations/', 1)[1] for k, v in present.items() if '/entryTypes/' in k}
    if types:
        for group in listed(poc, poc.parent + '/entryGroups', 'entryGroups'):
            for entry in listed(poc, group['name'] + '/entries', 'entries'):
                if entry.get('entryType', '').split('/locations/', 1)[-1] in types and canonical(entry['name']) not in allowed_entries:
                    raise CloudError('Catalog型を他のEntryが利用しているため削除しません。')
    ordered = sorted(present, key=lambda n: 0 if '/entries/' in n else 1 if '/entryGroups/' in n else 2)
    save(poc.folder / 'cleanup-plan.json', {'checked_at': timestamp(), 'resources': ordered})
    for name in ordered:
        print(('削除: ' if execute else '削除候補: ') + name, flush=True)
        if execute:
            actual = poc.get(name)
            if actual != present[name]:
                raise CloudError('削除直前に変更されました。停止します。')
            if '/entryGroups/' in name and '/entries/' not in name:
                if group_children(poc, name, actual):
                    raise CloudError('EntryGroupは空でないため削除しません。')
            poc.ledger[name]['state'] = 'delete_requested'
            poc.persist()
            from urllib.parse import urlencode
            query = '?' + urlencode({'etag': actual['etag']}) if actual.get('etag') else ''
            poc.wait(poc.c.request('DELETE', API + name + query))
            poc.ledger[name]['state'] = 'deleted'
            poc.persist()
    for name in names:
        if name not in present and execute:
            poc.ledger[name]['state'] = 'deleted'
            poc.persist()
    return ordered


def main():
    parser = argparse.ArgumentParser(description='Catalogの保護付き片付け。既定では削除しません。')
    parser.add_argument('--account')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    Catalog(config(require_project=True), args.account).cleanup(args.execute)


if __name__ == '__main__':
    main()

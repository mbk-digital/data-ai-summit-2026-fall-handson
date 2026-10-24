"""Narrow read/reuse comparison; never a deletion or concurrency precondition."""
import copy
import re

# Only top-level metadata on these resource types may differ. Nested fields,
# unknown fields, createTime, uid, name and all BigQuery timestamps stay strict.
RESOURCE = re.compile(r"projects/[^/]+/locations/[^/]+/(?:dataAgents/[^/]+|entryTypes/[^/]+|entryGroups/[^/]+(?:/entries/[^/]+)?|gibqObservabilitySettings/[^/]+(?:/settingBindings/[^/]+)?)")
METADATA = frozenset(('updateTime', 'etag'))

# Fields the API started returning with a default value after the ledger was written.
# Only the listed resource type, field path and exact default are treated like an absent
# field; `true`, non-empty settings and every unlisted field stay strict. Add an entry only
# after confirming what the field means and its default for that resource, together with
# a regression test (tests/test_resource_state.py).
API_DEFAULTS = (
    # Knowledge Catalog entry groups (seen 2026-10-19).
    (re.compile(r"projects/[^/]+/locations/[^/]+/entryGroups/[^/]+"), ('changeRequestConfig',), {}),
    # Observability settings (seen 2026-10-19).
    (re.compile(r"projects/[^/]+/locations/[^/]+/gibqObservabilitySettings/[^/]+"),
     ('conversationalAnalyticsSetting', 'bqaaEnabled'), False),
)


def _defaults(resource):
    name = resource.get('name', '') if isinstance(resource, dict) else ''
    return [(path, value) for pattern, path, value in API_DEFAULTS if pattern.fullmatch(name)]


def _lookup(resource, path):
    for key in path[:-1]:
        resource = resource.get(key) if isinstance(resource, dict) else None
    if isinstance(resource, dict) and path[-1] in resource:
        return True, resource[path[-1]]
    return False, None


def _is_default(present, value, default):
    return present and type(value) is type(default) and value == default


def without_api_defaults(resource):
    """Copy of the resource with listed API default fields removed when they hold the default."""
    result = copy.deepcopy(resource)
    for path, default in _defaults(resource):
        parent = result
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and _is_default(path[-1] in parent, parent.get(path[-1]), default):
            parent.pop(path[-1])
    return result


def stable(resource):
    if not isinstance(resource, dict):
        return copy.deepcopy(resource)
    resource = without_api_defaults(resource)
    known = bool(RESOURCE.fullmatch(resource.get('name', '')))
    # Without creation identity we cannot safely ignore metadata differences.
    identified = bool(resource.get('uid') or resource.get('createTime'))
    return {k: copy.deepcopy(v) for k, v in resource.items()
            if not (known and identified and k in METADATA)}


def compare(expected, actual):
    present = isinstance(expected, dict) and isinstance(actual, dict) and bool(expected) and bool(actual)
    equivalent = bool(present and stable(expected) == stable(actual))
    changes = {}
    if equivalent:
        for key in METADATA:
            if (key in expected) != (key in actual) or expected.get(key) != actual.get(key):
                changes[key] = {'before_present': key in expected, 'before': expected.get(key),
                                'after_present': key in actual, 'after': actual.get(key)}
        # A listed default that appeared (or went away) is recorded so ledger_refresh can update the ledger.
        for path, _ in _defaults(actual):
            before_present, before = _lookup(expected, path)
            after_present, after = _lookup(actual, path)
            if before_present != after_present or before != after:
                changes['.'.join(path)] = {'before_present': before_present, 'before': before,
                                           'after_present': after_present, 'after': after}
    return {'equivalent': equivalent, 'metadata_changes': changes}


def equivalent(expected, actual):
    return compare(expected, actual)['equivalent']

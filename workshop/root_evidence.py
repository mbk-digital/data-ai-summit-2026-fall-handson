"""SDK-independent evidence boundary for the ADK workshop."""
import json
from decimal import Decimal, InvalidOperation


def strip_json_fence(text):
    """The model sometimes wraps its JSON answer in one Markdown code fence; the schema check still runs on the content."""
    body = text.strip()
    if body.startswith('```') and body.endswith('```') and body.count('```') == 2:
        first, _, rest = body[3:-3].partition('\n')
        if first.strip().lower() in ('', 'json'):
            return rest.strip()
    return text


def compact_response(response):
    """Do not expose progress/thought messages as analytical evidence."""
    result = {'status': response.get('status', 'ERROR'), 'final_text': [], 'sql': [], 'tables': []}
    if result['status'] != 'SUCCESS':
        result['error'] = response.get('error_details', 'Data Agent failed')
        return result
    for item in response.get('response', []):
        txt = item.get('text', {})
        if txt.get('textType') == 'FINAL_RESPONSE':
            result['final_text'].extend(txt.get('parts', []))
        if item.get('data', {}).get('generatedSql'):
            result['sql'].append(item['data']['generatedSql'])
        if 'Data Retrieved' in item:
            result['tables'].append(item['Data Retrieved'])
    if not result['final_text']:
        result.update(status='ERROR', error='No final answer returned')
    return result


def numeric_values(evidence):
    found = set()
    def walk(value):
        if isinstance(value, dict):
            for v in value.values(): walk(v)
        elif isinstance(value, list):
            for v in value: walk(v)
        elif not isinstance(value, bool):
            try:
                n = Decimal(str(value))
                if n.is_finite(): found.add(n)
            except (InvalidOperation, ValueError): pass
    for e in evidence:
        for table in e.get('tables', []): walk(table.get('rows', []))
    return found


def required_tool(evidence, calculations):
    if not evidence: return 'analyze_ec'
    if not calculations and any({'previous_gross_profit_jpy','current_gross_profit_jpy'}.issubset(t.get('headers', [])) for t in evidence[-1].get('tables', [])):
        return 'compute_change'
    return None


def calculate_change(evidence, table_index, row_index, previous_column, current_column):
    """Compute current-minus-previous from table coordinates, not model amounts."""
    if any(type(x) is not int or x < 0 for x in (table_index, row_index)):
        raise ValueError('Table and row indices must be nonnegative integers')
    tables = [t for e in evidence for t in e.get('tables', [])]
    table = tables[table_index]
    row = table['rows'][row_index]
    headers = table['headers']
    a, b = row[headers.index(previous_column)], row[headers.index(current_column)]
    if any(isinstance(x, bool) or not Decimal(str(x)).is_finite() for x in (a, b)):
        raise ValueError('Finite numeric table cells are required')
    delta = Decimal(str(b)) - Decimal(str(a))
    if delta != delta.to_integral_value(): raise ValueError('Integer JPY values required')
    return {'value': int(delta), 'operation': 'current_minus_previous', 'previous': a, 'current': b,
            'table_index': table_index, 'row_index': row_index,
            'previous_column': previous_column, 'current_column': current_column}


def finalize(answer, evidence, cfg, calculations=()):
    """Check structured amounts against returned table cells, not gold answers.

    This is not a proof of SQL, period, dimension or prose correctness.
    """
    result = dict(answer)
    result['definition_version'] = cfg['definition_version']
    measures = result.pop('measure_items', [])
    dimensions = result.pop('dimension_items', [])
    result['measures'] = {m['name']: {'value': m['value'], 'unit': m['unit']} for m in measures}
    result['dimensions'] = {d['name']: d['value'] for d in dimensions}
    result['data_sources'] = []
    checks = []
    if not evidence or any(e.get('status') != 'SUCCESS' for e in evidence):
        result.update(status='ERROR', reason_code='EVIDENCE_RETRIEVAL_FAILED', customer_message_ja='分析の根拠を取得できませんでした。実行記録を確認してください。')
    if result['status'] == 'OK':
        values = numeric_values(evidence) | {Decimal(str(c['value'])) for c in calculations}
        valid = bool(measures) and len(measures) == len(result['measures']) and len(dimensions) == len(result['dimensions'])
        for m in measures:
            match = not isinstance(m['value'], bool) and Decimal(str(m['value'])) in values
            if m['name'].endswith('_delta_jpy'):
                match = match and any(c['value'] == m['value'] for c in calculations)
            checks.append({'measure': m['name'], 'evidence_value_match': match})
            valid = valid and match
        has_sql = any(e.get('sql') for e in evidence)
        if not valid or not has_sql:
            result.update(status='ERROR', reason_code='EVIDENCE_VALIDATION_FAILED', customer_message_ja='回答と取得表の照合を完了できませんでした。SQL・期間・数値を確認してください。')
        else:
            # This is the configured source scope, not a general SQL lineage parser.
            result['data_sources'] = ['v_revenue_events']
    if result['status'] != 'OK': result['measures'] = {}
    result['validation'] = {'numeric_checks': checks, 'scope': 'table_cell_membership_or_recorded_calculation',
                            'sql_period_dimensions_and_prose_require_review': True}
    return result


def review_evidence(evidence, cfg):
    """Structural checks and arithmetic, not independent LLM voting.

    Literal date presence is only a review aid, not a SQL semantic validator.
    """
    tables = evidence.get('tables', [])
    checks = []
    differences = []
    for ti, table in enumerate(tables):
        headers, rows = table.get('headers', []), table.get('rows', [])
        shape = bool(headers) and len(headers) == len(set(headers)) and all(isinstance(r, list) and len(r) == len(headers) for r in rows)
        checks.append({'check':'table_shape', 'table_index':ti, 'passed':shape, 'rows':len(rows)})
        pair = ['previous_gross_profit_jpy', 'current_gross_profit_jpy']
        if any(h in headers for h in pair):
            complete = all(h in headers for h in pair)
            checks.append({'check':'comparison_columns', 'table_index':ti, 'passed':complete})
            if shape and complete:
                for ri in range(len(rows)):
                    try: differences.append(calculate_change([evidence], ti, ri, *pair))
                    except (ValueError, ArithmeticError, IndexError, TypeError):
                        checks.append({'check':'finite_comparison_amounts', 'table_index':ti, 'row_index':ri, 'passed':False})
    sql = '\n'.join(evidence.get('sql', []))
    boundaries = sorted({d for k in ('current_period','previous_period') for d in cfg.get(k, {}).values()})
    return {'structural_checks_passed':all(c['passed'] for c in checks) if tables else None,
            'checks':checks, 'calculated_differences':differences,
            'period_boundary_literals':{d:d in sql for d in boundaries},
            'period_check':'literal_presence_only_not_semantic_validation',
            'scope':'table_structure_and_arithmetic', 'human_review_required':True}


class RequestEvidence:
    """Request-local cache. Fault mode deliberately executes both consumers."""
    def __init__(self, cache_enabled, fetch, reviewer=None):
        self.cache_enabled = cache_enabled
        self.fetch = fetch
        self.cache = {}
        self.events = []
        self.results = []
        self.reviewer = reviewer
        self.reviews = []

    async def get(self, key, consumer):
        hit = self.cache_enabled and key in self.cache
        self.events.append({'consumer': consumer, 'cache_hit': hit})
        if hit: return self.cache[key]
        value = await self.fetch(key, consumer)
        self.results.append(value)
        if value.get('status') == 'SUCCESS': self.cache[key] = value
        return value

    async def both(self, key):
        first = await self.get(key, 'answer')
        if first.get('status') != 'SUCCESS': return first
        second = await self.get(key, 'evidence_review')
        if self.reviewer is not None:
            review = self.reviewer(second)
            review['retrieval_mode'] = 'shared_evidence' if self.events[-1]['cache_hit'] else 'separate_retrieval'
            self.reviews.append(review)
            if review['structural_checks_passed'] is False:
                second = dict(second, status='ERROR', error='Evidence table structure failed review')
                self.results[-1] = second
        return second

"""Render SQL locally. This module never contacts or mutates Google Cloud."""

import re
from pathlib import Path
from .data import ROOT, EVENT_SELECT


def render(cfg, output):
    project = cfg["project_id"]
    dataset = cfg["dataset_id"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Invalid project ID")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dataset):
        raise ValueError("Invalid dataset ID")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "data/bigquery_schema.sql").read_text(encoding="utf-8")
    replace = lambda text: text.replace("{{PROJECT_ID}}", project).replace("{{DATASET_ID}}", dataset)
    (output / "01_tables.sql").write_text(replace(template), encoding="utf-8")
    view = EVENT_SELECT
    for table in ("order_items", "orders", "products", "refunds"):
        view = re.sub(r"\b(FROM|JOIN) " + table + r"\b", lambda match: match.group(1) + " `" + project + "." + dataset + "." + table + "`", view)
    (output / "02_revenue_events.sql").write_text(
        "-- Rendered locally. Use workshop.cloud verify-bigquery to check the configured project.\n"
        "CREATE OR REPLACE VIEW `" + project + "." + dataset + ".v_revenue_events` AS\n" + view + ";\n", encoding="utf-8")
    query_output = output / "verified_queries"
    query_output.mkdir(exist_ok=True)
    for query in sorted((ROOT / "semantics/verified_queries").glob("*.sql")):
        (query_output / query.name).write_text(replace(query.read_text(encoding="utf-8")), encoding="utf-8")


def expected_view_query(cfg):
    """The same rendered SQL used by bootstrap and guarded cleanup."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        render(cfg,directory)
        return (Path(directory)/'02_revenue_events.sql').read_text().split(' AS\n',1)[1].strip().rstrip(';').strip()


def expected_table_fields(table):
    block=(ROOT/'data/bigquery_schema.sql').read_text().split('.'+table+'` (',1)[1].split('\n);',1)[0]
    fields=[]
    for field,kind,description in re.findall(r"(\w+) (STRING|DATE|INT64) NOT NULL(?: OPTIONS\(description='([^']*)'\))?",block):
        item={'name':field,'type':'INTEGER' if kind=='INT64' else kind,'mode':'REQUIRED'}
        if description:item['description']=description
        fields.append(item)
    return fields

"""Cloud Shell friendly BigQuery bootstrap, using gcloud auth and stdlib REST.

Tokens stay in memory. No Cloud SDK Python dependencies or service-account keys.
"""

import argparse
import contextlib
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .bigquery import render, expected_view_query
from .data import ROOT, FIELDS, config, connect, fixture
from .reference import all_answers, cases

BQ = "https://bigquery.googleapis.com/bigquery/v2"
LABELS = {"workshop": "summit_ec_v1", "managed_by": "workshop_cli"}
REQUIRED_APIS = ["bigquery.googleapis.com", "geminidataanalytics.googleapis.com",
                 "cloudaicompanion.googleapis.com", "aiplatform.googleapis.com",
                 "storage.googleapis.com", "cloudtrace.googleapis.com",
                 "logging.googleapis.com", "monitoring.googleapis.com"]
PERMISSIONS = ["bigquery.datasets.create", "bigquery.jobs.create", "bigquery.tables.create",
               "bigquery.tables.updateData", "bigquery.tables.getData",
               "serviceusage.services.enable", "geminidataanalytics.dataAgents.create",
               "geminidataanalytics.dataAgents.list", "aiplatform.reasoningEngines.create",
               "aiplatform.reasoningEngines.list"]

# この3権限だけはDataset/Tableへの実操作で確認する。将来追加する削除・Routine等の
# 権限まで自動で延期しない。変更時は診断範囲と実操作の両方をレビューする。
DEFERRED_TABLE_PERMISSIONS = (
    "bigquery.tables.create", "bigquery.tables.getData", "bigquery.tables.updateData",
)


class CloudError(RuntimeError):
    pass


def current_account(account=None):
    """実際に操作へ使うアカウント。表示と操作で同じ値を使うため、ここで確定させる。"""
    if account:
        return account
    value = gcloud(["config", "get-value", "account"]).strip()
    if not value or value == "(unset)":
        raise CloudError("gcloudのアカウントを取得できません。Cloud Shellの承認を済ませるか、--account で指定してください。")
    return value


def enable_apis(project, apis, account=None):
    """必要なAPIを有効化する。既に有効なものを渡しても失敗しない。"""
    if not apis:
        return []
    gcloud(["services", "enable", *sorted(set(apis)), "--project=" + project], account)
    return sorted(set(apis))


@contextlib.contextmanager
def client_session(account=None):
    """設定検証つきのClientを開き、終了時に必ず実行ジョブを記録する。

    main()に直接書いていた前処理・後処理を、他のコマンドからも落とさず使うため。
    """
    cfg = config(require_project=True)
    validate_cloud_config(cfg)
    client = Client(cfg, account)
    try:
        yield client
    finally:
        save(ROOT / "artifacts/cloud-last-jobs.json", {"project_id": client.project, "jobs": client.jobs})


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def trace_console_url(project, trace_id):
    """Trace Explorer で 1 つの Trace を直接開く URL（2026-10 時点の Console）。"""
    return ("https://console.cloud.google.com/traces/explorer;traceId=" + urllib.parse.quote(trace_id, safe="")
            + "?" + urllib.parse.urlencode({"project": project}))


def modern_trace_url(url):
    """旧形式（/traces/list?tid=）の記録を、今の Trace Explorer で開ける形に直す。"""
    if not isinstance(url, str):
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.netloc == "console.cloud.google.com" and parsed.path == "/traces/list" and query.get("tid") and query.get("project"):
        return trace_console_url(query["project"][0], query["tid"][0])
    return url


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def gcloud(arguments, account=None):
    command = ["gcloud"] + arguments + (["--account=" + account] if account else []) + ["--quiet"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise CloudError("gcloud failed: " + result.stderr.strip()[:2000])
    return result.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudError("Unexpected API redirect; request stopped")


class Client:
    def __init__(self, cfg, account=None):
        self.cfg = cfg
        self.project = cfg["project_id"]
        self.location = cfg["bigquery_location"]
        self.account = account
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect())
        self.jobs = []

    def request(self, method, url, payload=None, missing_ok=False):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".googleapis.com"):
            raise CloudError("Only Google Cloud HTTPS API endpoints are allowed")
        if self.token is None:
            self.token = gcloud(["auth", "print-access-token"], self.account)
        request = urllib.request.Request(
            url, method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json", "x-goog-user-project": self.project})
        try:
            with self.opener.open(request, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error", {}).get("message", body)
            except ValueError:
                message = body
            raise CloudError("HTTP %d %s: %s" % (exc.code, parsed.path, str(message)[:2000])) from None
        except urllib.error.URLError as exc:
            raise CloudError("Cloud API接続に失敗しました: " + str(exc.reason)) from None

    def query(self, sql, params=None):
        body = {"query": sql, "useLegacySql": False, "location": self.location,
                "maximumBytesBilled": str(100 * 1024 * 1024), "timeoutMs": 10000,
                "maxResults": 1000, "requestId": str(uuid.uuid4()), "labels": LABELS}
        if params:
            body.update(parameterMode="NAMED", queryParameters=params)
        start = time.monotonic()
        result = self.request("POST", BQ + "/projects/" + self.project + "/queries", body)
        job = result.get("jobReference", {})
        # Record immediately, so interrupted requests can be investigated without resubmission.
        record = {"job_id": job.get("jobId"), "location": self.location, "started_at": timestamp()}
        self.jobs.append(record)
        while not result.get("jobComplete", False):
            if time.monotonic() - start > 180:
                raise CloudError("Query still running; inspect job before retrying: " + str(job.get("jobId")))
            result = self.request("GET", BQ + "/projects/" + self.project + "/queries/" + job["jobId"]
                                  + "?" + urllib.parse.urlencode({"location": self.location, "timeoutMs": 10000, "maxResults": 1000}))
        if result.get("errors"):
            raise CloudError("BigQuery job error: " + json.dumps(result["errors"], ensure_ascii=False))
        if result.get("pageToken"):
            raise CloudError("Result exceeds this small-fixture tool's row limit")
        record.update(elapsed_ms=round((time.monotonic() - start) * 1000),
                      total_bytes_processed=result.get("totalBytesProcessed"),
                      cache_hit=result.get("cacheHit"))
        fields = result.get("schema", {}).get("fields", [])
        rows = []
        for row in result.get("rows", []):
            parsed = {}
            for field, cell in zip(fields, row["f"]):
                value = cell.get("v")
                if value is not None and field["type"] in ("INTEGER", "INT64"):
                    value = int(value)
                parsed[field["name"]] = value
            rows.append(parsed)
        return rows


def validate_cloud_config(cfg):
    if not isinstance(cfg.get("dataset_id"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg["dataset_id"]):
        raise CloudError("Invalid dataset_id")
    if cfg.get("bigquery_location") not in ("US", "EU", "us-central1", "asia-northeast1"):
        raise CloudError("BigQuery location must be explicitly configured (US/EU/us-central1/asia-northeast1)")


def dataset_path(client):
    return BQ + "/projects/" + client.project + "/datasets/" + client.cfg["dataset_id"]


def ensure_owned_dataset(resource, location):
    if resource.get("location", "").lower() != location.lower():
        raise CloudError("Existing dataset location differs from configured location; no changes made")
    if any(resource.get("labels", {}).get(key) != value for key, value in LABELS.items()):
        raise CloudError("Dataset is not marked as managed by this workshop; choose a different dataset_id")


def stage_requirements(stage):
    bq = [p for p in PERMISSIONS if p.startswith("bigquery.")]
    if stage == "bigquery":
        return ["bigquery.googleapis.com"], bq
    if stage == "lab1":
        # Agent get/chat can be granted on the agent after creation. Do not
        # require project-wide grants for those resource-level permissions.
        return ["bigquery.googleapis.com", "geminidataanalytics.googleapis.com", "cloudaicompanion.googleapis.com"], bq + ["geminidataanalytics.dataAgents.create"]
    if stage == 'lab1-catalog':
        apis, permissions = stage_requirements('lab1')
        return apis + ['dataplex.googleapis.com'], permissions + [
            'dataplex.entryGroups.create', 'dataplex.entryGroups.get',
            'dataplex.entryTypes.create', 'dataplex.entryTypes.get', 'dataplex.entryTypes.use',
            'dataplex.entries.create', 'dataplex.entries.get', 'dataplex.entries.update',
            'dataplex.aspectTypes.get', 'dataplex.entryGroups.useOverviewAspect']
    if stage == 'lab1-graph':
        apis, permissions = stage_requirements('lab1-catalog')
        return apis, permissions + ['bigquery.propertyGraphs.create', 'bigquery.propertyGraphs.get',
                                   'bigquery.propertyGraphs.list', 'bigquery.propertyGraphs.delete']
    if stage == 'lab2':
        apis, permissions = stage_requirements('lab1-catalog')
        return apis + ['aiplatform.googleapis.com', 'cloudtrace.googleapis.com', 'monitoring.googleapis.com', 'logging.googleapis.com'], permissions + [
            'aiplatform.endpoints.predict', 'cloudtrace.traces.patch', 'cloudtrace.traces.get',
            'cloudtrace.traces.list', 'monitoring.timeSeries.list', 'monitoring.metricDescriptors.list']
    if stage == 'lab3':
        apis, permissions = stage_requirements('lab2')
        return apis, permissions + ['bigquery.jobs.listAll']
    if stage == "all":
        return REQUIRED_APIS, PERMISSIONS
    raise ValueError("Unknown preflight stage")


def preflight(client, stage="all", quiet=False):
    """Project側の前提を診断する。実リソースの操作成功を保証するものではない。

    schema_version=2: ready_for_bigquery / prerequisites_verified は、各診断範囲の
    前提を通過し課金有効も確認できたことを表す。テーブル実操作は別扱いで、この
    読み取り診断では table_operations_verified を常に False とする。
    can_attempt_* は課金を取得できない場合も実操作を試せることを示す。
    """
    required_apis, required_permissions = stage_requirements(stage)
    project = json.loads(gcloud(["projects", "describe", client.project, "--format=json"], client.account))
    billing_enabled = "unknown"
    warnings = []
    try:
        billing = json.loads(gcloud(["billing", "projects", "describe", client.project,
                                    "--format=json(projectId,billingEnabled)"], client.account))
        if isinstance(billing.get("billingEnabled"), bool):
            billing_enabled = billing["billingEnabled"]
        else:
            warnings.append("課金情報に有効・無効の判定がありません。課金状態は未確認です。")
    except (CloudError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        warnings.append("課金情報を取得できませんでした。診断を続け、実操作のエラーを確認します: " + str(exc))
    enabled = gcloud(["services", "list", "--enabled", "--project=" + client.project,
                      "--format=value(config.name)"], client.account).splitlines()
    # Projectの基本ロールは、Dataset作成時の既定アクセスを通してテーブル操作を
    # 許可することがある。ProjectでのtestIamPermissionsの不返却を不足と扱わない。
    # これらはbootstrapのDataset作成後、実際のDDL・読み取り・INSERTで確認する。
    deferred_permissions = sorted(p for p in required_permissions if p in DEFERRED_TABLE_PERMISSIONS)
    project_permissions = [p for p in required_permissions if p not in deferred_permissions]
    permissions = client.request("POST", "https://cloudresourcemanager.googleapis.com/v1/projects/"
                                 + client.project + ":testIamPermissions", {"permissions": project_permissions}).get("permissions", [])
    resource = client.request("GET", dataset_path(client), missing_ok=True) if "bigquery.googleapis.com" in enabled else None
    bq_permissions = [p for p in stage_requirements("bigquery")[1] if p not in deferred_permissions]
    missing_apis = sorted(set(required_apis) - set(enabled))
    missing_permissions = sorted(set(project_permissions) - set(permissions))
    active = project.get("lifecycleState") == "ACTIVE"
    can_attempt_bq = active and billing_enabled is not False and "bigquery.googleapis.com" in enabled and set(bq_permissions).issubset(permissions)
    can_attempt_stage = active and billing_enabled is not False and not missing_apis and not missing_permissions
    report = {"schema_version": 2, "checked_at": timestamp(), "project_id": client.project, "stage": stage,
              "permission_check_scope": "project_preconditions",
              "checked_permissions": project_permissions,
              "project_state": project.get("lifecycleState"), "billing_enabled": billing_enabled,
              "warnings": warnings,
              "enabled_required_apis": sorted(set(enabled) & set(required_apis)),
              "missing_apis": missing_apis, "granted_permissions": permissions,
              "missing_permissions": missing_permissions, "bigquery_location": client.location,
              "deferred_permissions": deferred_permissions,
              "table_operations_verified": False,
              "dataset_exists": resource is not None if "bigquery.googleapis.com" in enabled else None,
              "ready_for_bigquery": can_attempt_bq and billing_enabled is True,
              "can_attempt_bigquery": can_attempt_bq,
              "prerequisites_verified": can_attempt_stage and billing_enabled is True,
              "can_attempt_stage": can_attempt_stage,
              "agent_resource_access_verified": False,
              "runtime_deployment_verified": False, "model_access_verified": False,
              "quota_load_test_verified": False}
    if resource:
        ensure_owned_dataset(resource, client.location)
    save(ROOT / "artifacts/cloud-preflight.json", report)
    # quiet: setup から呼ぶ場合。問題があるときだけ表示し、詳細は保存した report に残す。
    if not quiet:
        print("事前診断 [" + stage + "]: Project=" + client.project + ", 課金=" + str(billing_enabled), flush=True)
    elif billing_enabled is False:
        # False は実操作の前に停止する。unknown は診断を続ける。対処が違うため表示を分ける。
        print("  課金が無効です。「お支払い」画面で、対象 Project に有効な請求先アカウントが紐付いていることを確認してください。", flush=True)
    elif billing_enabled is not True:
        print("  課金が有効か確認できませんでした。診断は続けます。", flush=True)
    for warning in warnings:
        print("警告: " + warning, flush=True)
    if missing_apis:
        print("この段階で必要なAPIが未有効: " + ", ".join(missing_apis), flush=True)
    if missing_permissions:
        print("この段階の確認対象権限が不足: " + ", ".join(missing_permissions), flush=True)
    if quiet:
        return report
    if deferred_permissions:
        print("テーブル操作権限はProject単位では判定しません。対象Dataset・Tableへの実操作で確認します: "
              + ", ".join(deferred_permissions), flush=True)
    print("実操作へ進めます。実際の利用成功はまだ未確認です。" if can_attempt_stage else "実操作の前に上記の状態・課金・API・権限を確認してください。", flush=True)
    return report


def check_agent_endpoints(client):
    """Read-only list checks; does not claim deploy or inference compatibility."""
    ca_location = client.cfg.get("data_agent_location")
    hosts = {"global": "geminidataanalytics.googleapis.com", "us": "geminidataanalytics.us.rep.googleapis.com",
             "eu": "geminidataanalytics.eu.rep.googleapis.com", "us-east4": "geminidataanalytics-us-east4.googleapis.com"}
    runtime_location = client.cfg.get("runtime_location")
    if ca_location not in hosts or not isinstance(runtime_location, str) or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", runtime_location):
        raise CloudError("Data Agent/Runtimeの候補リージョンをconfigureで指定してください。")
    checks = []
    paths = [
        ("data_agent", ca_location, "https://" + hosts[ca_location] + "/v1/projects/" + client.project + "/locations/" + ca_location + "/dataAgents?pageSize=1"),
        ("runtime", runtime_location, "https://" + runtime_location + "-aiplatform.googleapis.com/v1/projects/" + client.project
         + "/locations/" + runtime_location + "/reasoningEngines?pageSize=1"),
    ]
    for service, location, url in paths:
        try:
            client.request("GET", url)
            checks.append({"service": service, "location": location, "list_access_verified": True})
        except CloudError as exc:
            checks.append({"service": service, "location": location, "list_access_verified": False, "error": str(exc)})
    report = {"project_id": client.project, "checked_at": timestamp(), "checks": checks,
              "deployment_verified": False, "inference_verified": False}
    save(ROOT / "artifacts/cloud-endpoints.json", report)
    for check in checks:
        print(check["service"] + " / " + check["location"] + ": " + ("一覧取得OK" if check["list_access_verified"] else check["error"]), flush=True)
    return all(check["list_access_verified"] for check in checks)


def seed_insert_sql(qualified, table, records):
    params = []
    value_rows = []
    for row_number, row in enumerate(records):
        names = []
        for column_number, (column, value) in enumerate(zip(FIELDS[table], row)):
            name = "r%dc%d" % (row_number, column_number)
            names.append("@" + name)
            kind = "INT64" if isinstance(value, int) else "DATE" if column.endswith("_date") else "STRING"
            params.append({"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": str(value)}})
        value_rows.append("(" + ",".join(names) + ")")
    sql = ("BEGIN TRANSACTION;\nASSERT (SELECT COUNT(*) FROM " + qualified + ") = 0 AS 'Refusing to append to a nonempty seed table';\n"
           + "INSERT INTO " + qualified + " (" + ",".join(FIELDS[table]) + ") VALUES " + ",\n".join(value_rows)
           + ";\nCOMMIT TRANSACTION;")
    return sql, params


def table_rows(client, table):
    qualified = "`" + client.project + "." + client.cfg["dataset_id"] + "." + table + "`"
    rows = client.query("SELECT " + ",".join(FIELDS[table]) + " FROM " + qualified + " ORDER BY " + FIELDS[table][0])
    return [tuple(row[field] for field in FIELDS[table]) for row in rows]


def bootstrap(client, stage="bigquery", quiet=False):
    report = preflight(client, stage, quiet)
    if not report["can_attempt_bigquery"] or (stage != "bigquery" and not report["can_attempt_stage"]):
        raise CloudError("BigQueryの事前確認が未完了です。artifacts/cloud-preflight.jsonを確認してください。")
    connect().close()  # Check fixture integrity before any resource write.
    resource = client.request("GET", dataset_path(client), missing_ok=True)
    if resource is None:
        resource = client.request("POST", BQ + "/projects/" + client.project + "/datasets", {
            "datasetReference": {"projectId": client.project, "datasetId": client.cfg["dataset_id"]},
            "location": client.location, "labels": LABELS,
            "description": "Synthetic EC workshop data. Managed by workshop CLI; no real customer data."})
    ensure_owned_dataset(resource, client.location)
    if not quiet:
        print("Datasetを確認しました。テーブルの作成・読み取り・書き込みを実操作で確認します。", flush=True)
    render(client.cfg, ROOT / "artifacts/bigquery")
    client.query((ROOT / "artifacts/bigquery/01_tables.sql").read_text(encoding="utf-8"))
    resource_report = {"project_id": client.project, "dataset_id": client.cfg["dataset_id"],
                       "location": client.location, "managed_labels": LABELS, "tables": [], "checked_at": timestamp()}
    # Persist IDs as resources become known. There is no teardown command in this stage.
    save(ROOT / "artifacts/cloud-resources.json", resource_report)
    for table, records in fixture().items():
        existing = table_rows(client, table)
        expected = sorted(records)
        action = "reused"
        if existing:
            if existing != expected:
                raise CloudError("Existing data differs in " + table + "; stopped without replacing participant data")
        else:
            sql, params = seed_insert_sql("`" + client.project + "." + client.cfg["dataset_id"] + "." + table + "`", table, records)
            client.query(sql, params)
            action = "seeded"
        resource_report["tables"].append(table)
        save(ROOT / "artifacts/cloud-resources.json", resource_report)
        if not quiet:
            print("  " + table + ": " + action + " (%d rows)" % len(records), flush=True)
    view_sql = (ROOT / "artifacts/bigquery/02_revenue_events.sql").read_text(encoding="utf-8")
    view_query = expected_view_query(client.cfg)
    existing_view = client.request("GET", dataset_path(client) + "/tables/v_revenue_events", missing_ok=True)
    if existing_view:
        if existing_view.get("type") != "VIEW" or existing_view.get("view", {}).get("query", "").strip().rstrip(";").strip() != view_query:
            raise CloudError("Existing view differs; stopped without overwriting participant changes")
    else:
        client.query(view_sql.replace("CREATE OR REPLACE VIEW", "CREATE VIEW", 1))
    resource_report["views"] = ["v_revenue_events"]
    save(ROOT / "artifacts/cloud-resources.json", resource_report)
    return verify(client, quiet)


def verification_sql(case, cfg):
    source = "`" + cfg["project_id"] + "." + cfg["dataset_id"] + ".v_revenue_events`"
    start, end = case["period"]["start"], case["period"]["end_exclusive"]
    params = [{"name": "start", "parameterType": {"type": "DATE"}, "parameterValue": {"value": start}},
              {"name": "end", "parameterType": {"type": "DATE"}, "parameterValue": {"value": end}}]
    clause = " FROM " + source + " WHERE event_date >= @start AND event_date < @end"
    metric = case.get("metric")
    if case["kind"] == "total":
        if metric not in {"net_sales_jpy", "gross_profit_jpy", "refund_jpy", "cogs_jpy"}:
            raise CloudError("Unsupported verification metric")
        sql = "SELECT COALESCE(SUM(" + metric + "),0) AS " + metric + clause
    elif case["kind"] == "top_product":
        if metric not in {"net_sales_jpy", "sold_quantity"}:
            raise CloudError("Unsupported ranking metric")
        sql = "SELECT product_id, SUM(" + metric + ") AS " + metric + clause + " GROUP BY product_id ORDER BY " + metric + " DESC, product_id LIMIT 1"
    elif case["kind"] == "largest_profit_decline":
        previous = cfg["previous_period"]
        params.extend([{"name": name, "parameterType": {"type": "DATE"}, "parameterValue": {"value": previous[key]}}
                       for name, key in (("previous_start", "start"), ("previous_end", "end_exclusive"))])
        sql = ("SELECT category, SUM(IF(event_date >= @start AND event_date < @end, gross_profit_jpy, 0))"
               " - SUM(IF(event_date >= @previous_start AND event_date < @previous_end, gross_profit_jpy, 0)) AS gross_profit_delta_jpy"
               " FROM " + source + " GROUP BY category ORDER BY gross_profit_delta_jpy ASC, category LIMIT 1")
    else:
        raise CloudError("Case is not a numeric SQL verification")
    return sql, params


def verify(client, quiet=False):
    resource = client.request("GET", dataset_path(client))
    ensure_owned_dataset(resource, client.location)
    checks = []
    for table, records in fixture().items():
        actual = table_rows(client, table)
        checks.append({"check": "seed:" + table, "success": actual == sorted(records), "row_count": len(actual)})
    gold = {answer["case_id"]: answer for answer in all_answers()}
    answers = []
    for case in cases():
        if case["kind"] == "unanswerable":
            continue  # SQL cannot test whether an LLM refrains from a future guarantee.
        sql, params = verification_sql(case, client.cfg)
        rows = client.query(sql, params)
        expected = dict(gold[case["case_id"]]["dimensions"])
        expected.update({key: value["value"] for key, value in gold[case["case_id"]]["measures"].items()})
        checks.append({"check": case["case_id"], "success": rows == [expected], "expected": expected, "actual": rows})
        answers.append({"case_id": case["case_id"], "sql_result": rows})
        if not quiet or rows != [expected]:
            print("  " + case["case_id"] + ": " + ("一致" if rows == [expected] else "不一致"), flush=True)
    # Execute authored SQL examples as well; compare key outputs, not just SQL syntax.
    for name, expected in (("monthly_metrics.sql", {"gross_sales_jpy": 420000, "discount_jpy": 65000, "refund_jpy": 115000,
                                                      "net_sales_jpy": 240000, "cogs_jpy": 180000, "gross_profit_jpy": 60000}),
                           ("top_product.sql", {"product_id": "P-B", "net_sales_jpy": 135000})):
        template = (ROOT / "semantics/verified_queries" / name).read_text(encoding="utf-8")
        sql = template.replace("{{PROJECT_ID}}", client.project).replace("{{DATASET_ID}}", client.cfg["dataset_id"])
        rows = client.query(sql)
        checks.append({"check": "verified_query:" + name, "success": rows == [expected], "actual": rows, "expected": expected})
    report = {"checked_at": timestamp(), "project_id": client.project, "dataset_id": client.cfg["dataset_id"],
              "bigquery_location": client.location, "definition_version": client.cfg["definition_version"],
              "checks": checks, "all_passed": all(check["success"] for check in checks),
              "sql_numeric_cases_verified": 7, "llm_cases_verified": 0,
              "excluded_case": "Q08 requires a real Agent response; not verified by SQL",
              "agent_cost_measured": False, "jobs": client.jobs}
    save(ROOT / "artifacts/cloud-verification.json", report)
    lines = ["# BigQuery実環境の検証結果", "", "- Project: `" + client.project + "`",
             "- Dataset: `" + client.cfg["dataset_id"] + "` / " + client.location,
             "- 確認日時: " + report["checked_at"], "", "| 検証 | 結果 |", "|---|---|"]
    lines.extend("| " + check["check"] + " | " + ("一致" if check["success"] else "不一致") + " |" for check in checks)
    lines += ["", "5テーブル全行、数値問題7問、SQL例2本を検証。Q08の将来予測への対応はAgent未実行のため未検証。",
              "", "これはBigQueryの実集計結果です。Data Agent/Geminiの品質・費用・応答時間の検証ではありません。", ""]
    (ROOT / "artifacts/cloud-report.md").write_text("\n".join(lines), encoding="utf-8")
    if not report["all_passed"]:
        raise CloudError("BigQuery集計に不一致があります。cloud-verification.jsonを確認してください。")
    if quiet:
        return report
    print("BigQuery検証完了: 14項目一致。Agentは未実行。", flush=True)
    print("次はBigQuery Consoleで作成結果を確認します:", flush=True)
    print("https://console.cloud.google.com/bigquery?project=" + client.project, flush=True)
    print("確認対象: " + client.project + "." + client.cfg["dataset_id"]
          + " / tables=products,campaigns,orders,order_items,refunds / view=v_revenue_events", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="参加者ProjectでのBigQuery準備・検証（Cloud操作あり）")
    parser.add_argument("command", choices=["preflight", "bootstrap-bigquery", "verify-bigquery", "check-agent-endpoints"])
    parser.add_argument("--stage", choices=["bigquery", "lab1", "lab1-catalog", "lab1-graph", "lab2", "lab3", "all"], help="事前診断対象。bootstrapの既定はBigQuery、preflightの既定はall")
    parser.add_argument("--account", help="省略時はgcloudの現在のアカウント。Cloud Shellでは通常省略")
    args = parser.parse_args()
    try:
        with client_session(args.account) as client:
            cfg = client.cfg
            print("対象: " + cfg["project_id"] + "." + cfg["dataset_id"] + " / " + cfg["bigquery_location"], flush=True)
            if args.command == "preflight":
                return 0 if preflight(client, args.stage or "all")["can_attempt_stage"] else 1
            if args.command == "check-agent-endpoints":
                return 0 if check_agent_endpoints(client) else 1
            if args.command == "bootstrap-bigquery":
                bootstrap(client, args.stage or "bigquery")
            else:
                verify(client)
            return 0
    except (CloudError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("エラー: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

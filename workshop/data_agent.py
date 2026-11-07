"""Small, resumable Data Agent PoC. Cloud Shell + gcloud; no SDK/key files.

Does not score prose by searching for expected numbers. Review final text and SQL
results together; raw API messages are retained as evidence, not as gold answers.
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request

from .resource_state import compare as compare_resource, equivalent
from .cloud import Client, CloudError, gcloud, save, timestamp
from .settings import ROOT, config
from .reference import cases

HOSTS = {"us": "geminidataanalytics.us.rep.googleapis.com",
         "eu": "geminidataanalytics.eu.rep.googleapis.com",
         "global": "geminidataanalytics.googleapis.com",
         "us-east4": "geminidataanalytics-us-east4.googleapis.com"}
VARIANTS = ("raw", "defined", "view")
# Console のエージェント カタログに出る名前。ID を照合しなくても参加者が見分けられるようにする。
# 作成時だけ設定し、所有の照合（ID・description・context）には使わない。
DISPLAY_NAMES = {"raw": "EC分析 ① 業務定義なし", "defined": "EC分析 ② 業務定義あり",
                 "view": "EC分析 ③ 業務定義あり＋集計View"}


# 削除した Agent は 30 日間同じ ID で残る。その ID と衝突したときだけ次の番号を使う
# （docs/maintainers/drafts/deleted-agent-reuse.md）。元の ID を最優先にする。
CANDIDATES = 5
AGENT_ID = re.compile(r"ec-poc-(?:raw|defined|view)-[0-9a-f]{16}(?:-r[2-5])?")


def candidate_ids(variant, digest):
    base = "ec-poc-%s-%s" % (variant, digest)
    return [base] + ["%s-r%d" % (base, number) for number in range(2, CANDIDATES + 1)]


def deletion_state(resource):
    """Only a non-empty deleteTime marks a deleted agent (seen on get, 2026-10-31)."""
    if resource is None:
        return "absent"
    delete_time = resource.get("deleteTime")
    if isinstance(delete_time, str) and delete_time:
        return "deleted"
    # Any other shape of the deletion fields is not one we have seen; never guess.
    return "unknown" if "deleteTime" in resource or "purgeTime" in resource else "usable"


def context(cfg, variant):
    if variant not in VARIANTS:
        raise ValueError("Unknown variant")
    minimal = (ROOT / "semantics/baseline.txt").read_text(encoding="utf-8").rstrip("\n")
    instruction = minimal
    if variant != "raw":
        instruction += "\n\n" + (ROOT / "semantics/business-rules.txt").read_text(encoding="utf-8").rstrip("\n")
    tables = ["v_revenue_events"] if variant == "view" else ["products", "campaigns", "orders", "order_items", "refunds"]
    return {"datasourceReferences": {"bq": {"tableReferences": [
        {"projectId": cfg["project_id"], "datasetId": cfg["dataset_id"], "tableId": table} for table in tables]}},
        "systemInstruction": instruction,
        "options": {"datasource": {"bigQueryMaxBilledBytes": "104857600"}}}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def extract(messages):
    """Keep final text distinct from progress/thoughts and tabular evidence."""
    result = {"texts": [], "sql": [], "data": [], "errors": [], "usage_fields": []}
    def usage(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                current = path + "/" + key
                if key in {"usageMetadata", "usage", "tokenUsage", "totalTokenCount", "promptTokenCount", "candidatesTokenCount", "inputTokens", "outputTokens", "billableUnits"}:
                    result["usage_fields"].append({"path": current, "value": item})
                else:
                    usage(item, current)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                usage(item, path + "/" + str(index))
    for message in messages:
        if "error" in message:
            result["errors"].append(message["error"])
        system = message.get("systemMessage", {})
        if "text" in system:
            result["texts"].append(system["text"])
        data = system.get("data", {})
        if "generatedSql" in data:
            result["sql"].append(data["generatedSql"])
        if "result" in data:
            result["data"].append(data["result"])
    usage(messages)
    return result


def published_context(resource):
    """Normalize only the service-added default model, without API mutations."""
    actual=json.loads(json.dumps(resource.get('dataAnalyticsAgent',{}).get('publishedContext',{})))
    if actual.get('options',{}).get('model')=='MODEL_UNSPECIFIED':actual['options'].pop('model')
    return actual


def validate_published(resource,marker,ctx,name):
    actual=published_context(resource)
    if resource.get('description')!=marker or actual!=ctx:
        raise CloudError('Existing Agent changed; refusing to overwrite: '+name)


class AgentClient(Client):
    def __init__(self, cfg, account=None):
        super().__init__(cfg, account)
        location = cfg.get("data_agent_location")
        if location not in HOSTS:
            raise CloudError("Configure a supported data_agent_location first")
        self.parent = "projects/%s/locations/%s" % (self.project, location)
        self.base = "https://" + HOSTS[location] + "/v1/"
        self.folder = ROOT / "artifacts/data-agent" / self.project / cfg["dataset_id"] / location

    def resolve(self, variant, ctx=None, ledger=None, get=None):
        """Choose the agent ID for this definition. Reads only; never creates or edits ledgers.

        ledger(path) returns the ledger text or None, get(name) the agent or None (404).
        Returns action reuse / create / stop, with the reason and a message for a stop.
        """
        if variant not in VARIANTS:
            raise ValueError("Unknown variant")
        ctx = context(self.cfg, variant) if ctx is None else ctx
        ledger = ledger or (lambda path: path.read_text() if path.is_file() else None)
        get = get or (lambda name: self.request("GET", self.base + name, missing_ok=True))
        digest = fingerprint(ctx)
        marker = "workshop_cli:summit_ec_v1:" + digest
        ids = candidate_ids(variant, digest)
        names = [self.parent + "/dataAgents/" + agent_id for agent_id in ids]
        paths = [self.folder / (agent_id + ".resource.json") for agent_id in ids]
        records = []
        for path in paths:
            text = ledger(path)
            records.append(None if text is None else json.loads(text))
        result = {"variant": variant, "marker": marker, "context": ctx, "probed": {}, "usable": []}

        def decide(index, action, reason="", message="", actual=None):
            result.update(action=action, reason=reason, message=message, agent_id=ids[index], name=names[index],
                          path=paths[index], record=records[index], actual=actual)
            return result

        def matches(actual):
            try:
                validate_published(actual, marker, ctx, actual.get("name", ""))
                return True
            except CloudError:
                return False

        # A ledger that is not "deleted" pins the ID: never move on or create while it exists.
        active = [i for i, record in enumerate(records) if record is not None and record.get("state") != "deleted"]
        if len(active) > 1:
            return decide(active[0], "stop", "multiple_ledgers", "Agent台帳が複数あります。変更せず停止しました。Checkpointで診断してください: "
                          + ", ".join(names[i] for i in active))
        if active:
            i = active[0]
            record = records[i]
            actual = get(names[i])
            result["probed"][names[i]] = actual
            state = deletion_state(actual)
            if state == "usable":
                result["usable"].append(names[i])
            if record.get("state") == "creation_requested":
                return decide(i, "stop", "creation_unconfirmed", "作成の途中で止まったAgentの記録があります。新しく作らずに停止しました。Checkpointで確認してください: " + names[i], actual)
            if state == "absent":
                return decide(i, "stop", "recorded_missing", "記録済みAgentが見つかりません。Checkpointで確認してください: " + names[i], actual)
            if state == "deleted":
                return decide(i, "stop", "recorded_deleted", "記録済みAgentが教材の片付け以外で削除されています。台帳は変更せず停止しました。講師に相談してください: " + names[i], actual)
            if state == "unknown":
                return decide(i, "stop", "unknown_shape", "Agentの削除状態を判定できません。変更せず停止しました: " + names[i], actual)
            if not matches(actual):
                return decide(i, "stop", "changed", "Existing Agent changed; refusing to overwrite: " + names[i], actual)
            if record.get("state") != "present" or record.get("name") != names[i] or record.get("description") != marker or not equivalent(record.get("resource"), actual):
                return decide(i, "stop", "ledger_mismatch", "Agent台帳の不足・内容または識別情報の不一致。Checkpointで診断してください: " + names[i], actual)
            return decide(i, "reuse", actual=actual)
        # No pinned ledger (first run, after the cleanup, or a lost ledger): look at every candidate.
        states = []
        for name in names:
            actual = get(name)
            result["probed"][name] = actual
            states.append(deletion_state(actual))
        result["usable"] = [names[i] for i, state in enumerate(states) if state == "usable"]
        if "unknown" in states:
            i = states.index("unknown")
            return decide(i, "stop", "unknown_shape", "Agentの削除状態を判定できません。変更せず停止しました: " + names[i], result["probed"][names[i]])
        live = [i for i, state in enumerate(states) if state == "usable"]
        if len(live) > 1:
            return decide(live[0], "stop", "multiple_live", "台帳のないAgentが複数あります。変更せず停止しました。バックアップを復元するか、Checkpointで診断してください: "
                          + ", ".join(names[i] for i in live), result["probed"][names[live[0]]])
        if live:
            i = live[0]
            actual = result["probed"][names[i]]
            if records[i] is not None:
                return decide(i, "stop", "deleted_ledger_but_usable", "Agent台帳では削除済みですが、Agentが使える状態です。変更せず停止しました。Checkpointで診断してください: " + names[i], actual)
            if not matches(actual):
                return decide(i, "stop", "changed", "Existing Agent changed; refusing to overwrite: " + names[i], actual)
            # Lost ledger: only Checkpoint may offer to rebuild it, with explicit approval.
            result["adoptable"] = True
            return decide(i, "stop", "ledger_missing", "Agent台帳の不足・内容または識別情報の不一致。Checkpointで診断してください: " + names[i], actual)
        free = [i for i, state in enumerate(states) if state == "absent"]
        if not free:
            return decide(0, "stop", "limit", "削除済みのAgentと重ならないIDが残っていません（候補%d個）。新しいProjectで準備するか、削除から30日後にやり直してください: %s" % (CANDIDATES, names[0]))
        return decide(free[0], "create")

    def ensure(self, variant, ctx=None):
        decision = self.resolve(variant, ctx)
        name, path, ctx = decision["name"], decision["path"], decision["context"]
        if decision["action"] == "stop":
            raise CloudError(decision["message"])
        if decision["action"] == "reuse":
            # Reuse must not silently refresh the ownership snapshot.
            self.last_created = False
            return name
        marker = decision["marker"]
        payload = {"name": name, "displayName": DISPLAY_NAMES[variant], "description": marker,
                   "dataAnalyticsAgent": {"publishedContext": ctx}}
        save(path, {"name": name, "description": marker, "state": "creation_requested", "requested_at": timestamp()})
        existing = self.request("POST", self.base + self.parent + "/dataAgents:createSync?dataAgentId=" + decision["agent_id"], payload)
        save(path, {"name": name, "description": marker, "state": "present", "checked_at": timestamp(), "resource": existing})
        save(self.folder / (decision["agent_id"] + ".config.json"), payload)
        self.last_created = True
        return name

    def report(self):
        records = []
        lines = ["# Data Agent実行証拠", "", "採点結果ではなく、APIの最終回答と取得データ・SQLを保存したもの。", ""]
        for path in sorted(self.folder.glob("*.json")):
            record = json.loads(path.read_text())
            if "question" not in record:
                continue
            evidence = extract(record.get("messages", []))
            records.append({"file": path.name, "state": record["state"],
                            "elapsed_seconds": record.get("elapsed_seconds"),
                            "sql_messages": len(evidence["sql"]),
                            "usage_fields": evidence["usage_fields"]})
            lines += ["## " + path.name, "", record["question"], "",
                      "State: %s / elapsed: %s seconds" % (record["state"], record.get("elapsed_seconds")), ""]
            for item in evidence["texts"]:
                if item.get("textType") == "FINAL_RESPONSE":
                    lines += ["\n".join(item.get("parts", [])), ""]
            if evidence["errors"] or record.get("error"):
                lines += ["```json", json.dumps(evidence["errors"] or record.get("error"), ensure_ascii=False, indent=2), "```", ""]
            for sql in evidence["sql"]:
                lines += ["```sql", sql, "```", ""]
            for data in evidence["data"]:
                lines += ["```json", json.dumps(data.get("data", data), ensure_ascii=False, indent=2), "```", ""]
        save(self.folder / "summary.json", {"generated_at": timestamp(), "attempts": records})
        (self.folder / "evidence.md").write_text("\n".join(lines), encoding="utf-8")
        print("Evidence: " + str(self.folder / "evidence.md"), flush=True)
        return records

    def teardown(self, execute=False):
        """Only exact resources recorded locally; default is a read-only plan."""
        plans = []
        for path in sorted(self.folder.glob("*.resource.json")):
            record = json.loads(path.read_text())
            if record.get("state") == "deleted":
                continue
            name = record.get("name", "")
            expected_prefix = self.parent + "/dataAgents/"
            if not name.startswith(expected_prefix) or not AGENT_ID.fullmatch(name[len(expected_prefix):]):
                raise CloudError("Invalid resource inventory: " + str(path))
            existing = self.request("GET", self.base + name, missing_ok=True)
            if existing is None:
                print("Already absent: " + name, flush=True)
                continue
            saved_resource = record.get("resource")
            if saved_resource is None or existing.get("description") != record.get("description") or existing.get("dataAnalyticsAgent") != saved_resource.get("dataAnalyticsAgent"):
                raise CloudError("Agent changed or creation is unconfirmed; review before deletion: " + name)
            plans.append((path, record, name))
        for path, record, name in plans:
            print(("Deleting: " if execute else "Would delete: ") + name, flush=True)
            if execute:
                self.request("DELETE", self.base + name + ":deleteSync")
                record.update(state="deleted", deleted_at=timestamp())
                save(path, record)
        return len(plans)

    def chat(self, name, question, output):
        payload = {"parent": self.parent, "messages": [{"userMessage": {"text": question}}],
                   "dataAgentContext": {"dataAgent": name}}
        if self.token is None:
            self.token = gcloud(["auth", "print-access-token"], self.account)
        request = urllib.request.Request(self.base + self.parent + ":chat", method="POST",
            data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + self.token,
            "Content-Type": "application/json", "x-goog-user-project": self.project, "x-server-timeout": "300"})
        record = {"agent": name, "question": question, "started_at": timestamp(), "state": "started", "request": payload}
        save(output, record)
        start = time.monotonic()
        try:
            with self.opener.open(request, timeout=330) as response:
                raw = response.read().decode("utf-8")
                record["content_type"] = response.headers.get("Content-Type")
            # The REST streaming representation is one JSON array when read to EOF.
            output.with_suffix(".response.txt").write_text(raw, encoding="utf-8")
            messages = json.loads(raw)
            if not isinstance(messages, list):
                messages = [messages]
            record.update(state="completed", messages=messages, evidence=extract(messages))
            if record["evidence"]["errors"]:
                record["state"] = "api_error"
        except urllib.error.HTTPError as exc:
            record.update(state="http_error", http_status=exc.code, error=exc.read().decode("utf-8")[:4000])
        except (OSError, ValueError) as exc:
            record.update(state="transport_or_parse_error", error=str(exc))
        record["elapsed_seconds"] = round(time.monotonic() - start, 3)
        save(output, record)
        return record


def main():
    parser = argparse.ArgumentParser(description="Data Agent PoC (creates agents and performs billable inference)")
    parser.add_argument("command", choices=["create", "run", "report", "teardown"])
    parser.add_argument("--account")
    parser.add_argument("--execute", action="store_true", help="Apply the teardown plan; only recorded, unchanged PoC agents")
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument("--cases", nargs="+", default=["Q01", "Q03", "Q04", "Q08"])
    parser.add_argument("--run-id", default="initial", help="New label for a deliberate repeat; existing attempts are never overwritten")
    parser.add_argument("--language", choices=["ja", "en"], default="en")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", args.run_id):
            raise ValueError("run-id must be 1-40 lowercase letters, digits or hyphens")
        selected = {case["case_id"]: case for case in cases()}
        if set(args.cases) - set(selected):
            raise ValueError("Unknown case ID")
        client = AgentClient(config(require_project=True), args.account)
        print("Target: " + client.parent, flush=True)
        if args.command == "report":
            client.report()
            return 0
        if args.command == "teardown":
            client.teardown(args.execute)
            return 0
        if args.execute:
            raise ValueError("--execute is only for teardown")
        failures = 0
        for variant in args.variants:
            name = client.ensure(variant)
            print("Agent ready: " + name, flush=True)
            if args.command == "create":
                continue
            for case_id in args.cases:
                path = client.folder / (name.rsplit("/", 1)[1] + "." + args.language + "." + case_id + ("" if args.run_id == "initial" else "." + args.run_id) + ".json")
                if path.exists():
                    previous = json.loads(path.read_text())
                    if previous.get("question") != selected[case_id]["question_" + args.language]:
                        raise CloudError("Question changed; use a new --run-id to retain both attempts")
                    print("Retained existing attempt: " + path.name + " (" + previous["state"] + ")", flush=True)
                    failures += previous["state"] != "completed"
                    continue
                record = client.chat(name, selected[case_id]["question_" + args.language], path)
                print("%s %s: %s (%.1fs)" % (variant, case_id, record["state"], record["elapsed_seconds"]), flush=True)
                failures += record["state"] != "completed"
                if record["state"] in ("http_error", "transport_or_parse_error"):
                    print(record.get("error", ""), flush=True)
                    return 2  # Do not fan out a setup or ambiguous transport failure.
        return 1 if failures else 0
    except (CloudError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Participant #1: edit Catalog in Console and compare three Agents."""
import argparse
import html
import fcntl
import json
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cloud import CloudError, save, timestamp, validate_cloud_config
from .data_agent import DISPLAY_NAMES, AgentClient, context, extract, fingerprint
from .settings import ROOT, config
from .reference import cases
from .catalog import Catalog, as_document

LABELS = {"raw": "① 業務定義なし", "defined": "② 業務定義あり", "view": "③ 業務定義あり＋集計View"}
DISPLAY = "日本語で回答する。表示する期間は開始日と最終日を明記する（例：8月1日〜8月31日）。SQLは終了日を含まない条件を使用する。"
AGENTS_URL = "https://console.cloud.google.com/bigquery/agents_hub?project="
CATALOG_URL = "https://console.cloud.google.com/dataplex/dp-search?project="


def initialize(root=ROOT):
    path = root / "participant/semantics.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write((root / "semantics/lab1-starter.txt").read_text(encoding="utf-8"))
    return path


def make_contexts(cfg, rules, receipt=None):
    ready = bool(rules.strip()) and "[[" not in rules and "]]" not in rules
    raw = context(cfg, "raw")
    raw["systemInstruction"] += "\n\n" + DISPLAY
    result = {"raw": raw}
    if ready:
        for variant in ("defined", "view"):
            ctx = context(cfg, variant)
            ctx["systemInstruction"] = raw["systemInstruction"]
            if receipt is None:
                raise ValueError('定義あり構成にはCatalogの取得証拠が必要です。')
            ctx['glossaryTerms'] = [{'displayName': receipt['title'], 'description': receipt['body']}]
            result[variant] = ctx
    return result


class StepNotReady(ValueError):
    """参加者の手順がまだ済んでいないだけの停止。設定・権限の問題として扱わない。"""


def prepare_console(root=ROOT, account=None, selected_ids=("Q01", "Q04"), client_factory=AgentClient, catalog_factory=Catalog,
                    apply=False):
    """Prepare the agents used by the Console exercise without asking questions.

    apply=True は手順 3 の apply-rules。処理は同じで、手順 2 の保存が済んでいなければ
    ②③を作らずに止まり、表示をこの手順で起きたことに絞る。
    """
    cfg = config(require_project=True, root=root)
    validate_cloud_config(cfg)
    path = initialize(root)
    starter = (root / "semantics/lab1-starter.txt").read_text(encoding="utf-8").strip()
    if len(starter) > 30000:
        raise ValueError("定義が長すぎます。participant/semantics.txtを確認してください。")
    if apply:
        print("手順 2 で保存したルールを Knowledge Catalog から読み取り、エージェント②③に渡します。ここでは質問を送信しません。", flush=True)
    else:
        print("#1 で使うエントリとエージェントを準備します。ここでは質問を送信しません。", flush=True)
    catalog = catalog_factory(cfg, account, root=root)
    receipt = catalog.prepare_console_definition(as_document(starter))
    rules = receipt["document"].strip()
    complete = bool(rules) and "[[" not in rules and "]]" not in rules
    if apply and not complete:
        raise StepNotReady("手順 2 の編集がまだ保存されていません。Knowledge Catalog のエントリの［概要］で "
                         "[[...]] の 2 か所を置き換えて保存してから、もう一度実行してください。")
    if complete:
        local = path.read_text(encoding="utf-8").strip()
        if local != rules:
            if local not in {starter, receipt.get("previous_document", "").strip()}:
                raise ValueError("Cloud Shellの定義とCatalogの定義が両方変更されています。どちらも上書きせず停止しました。")
            backup = root / "artifacts/lab1" / ("semantics-before-catalog-import-" + uuid.uuid4().hex + ".txt")
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(local + "\n", encoding="utf-8")
            path.write_text(rules + "\n", encoding="utf-8")
            if not apply:
                print("Catalogから取得した完成定義をparticipant/semantics.txtへ保存しました。", flush=True)
    contexts = make_contexts(cfg, rules, receipt)
    client = client_factory(cfg, account)
    print("対象Project: " + cfg["project_id"], flush=True)
    if apply:
        print("ルールを読み取りました: 「" + receipt["title"] + "」", flush=True)
    agents = {}
    for variant, ctx in contexts.items():
        print(LABELS[variant] + "のエージェントを準備しています…", end="", flush=True)
        name = client.ensure(variant, ctx)
        agents[variant] = name
        created = getattr(client, "last_created", None)
        print({True: " 新しく作りました", False: " 作成済みのものをそのまま使います（変更なし）"}.get(created, " 完了"), flush=True)
        print(LABELS[variant] + "：エージェント カタログの「" + DISPLAY_NAMES[variant] + "」（ID: "
              + name.rsplit("/", 1)[1] + "）", flush=True)
    selected = {case["case_id"]: case for case in cases()}
    questions = ["   " + case_id + ": " + selected[case_id]["question_ja"] for case_id in selected_ids]
    print("", flush=True)
    # 次の操作を手順書と同じ順に番号付きで示す。URL はその操作の直下に置く。
    if apply:
        print("1. BigQuery Agents で「" + DISPLAY_NAMES["defined"] + "」を開き、［エージェントを編集］の"
              "［用語集］に手順 2 のルールが入っていることを確認します", flush=True)
        print("   " + AGENTS_URL + cfg["project_id"], flush=True)
        print("2. ②と③に、①と同じ次の 2 問を送ります", flush=True)
        print("\n".join(questions), flush=True)
    elif complete:
        print("1. BigQuery Agents で①②③を順に開き、次の 2 問を送ります", flush=True)
        print("   " + AGENTS_URL + cfg["project_id"], flush=True)
        print("\n".join(questions), flush=True)
        print("定義を確認する場合は Knowledge Catalog で「EC売上・粗利益の業務定義」を開きます", flush=True)
        print("   " + CATALOG_URL + cfg["project_id"], flush=True)
    else:
        print("1. Knowledge Catalog で「EC売上・粗利益の業務定義」を開き、空欄の場所を確認します（まだ編集しません）", flush=True)
        print("   " + CATALOG_URL + cfg["project_id"], flush=True)
        print("2. BigQuery Agents で「" + DISPLAY_NAMES["raw"] + "」に次の 2 問を送ります", flush=True)
        print("   " + AGENTS_URL + cfg["project_id"], flush=True)
        print("\n".join(questions), flush=True)
        print("3. そのあと手順 2 でエントリを編集し、手順 3 で apply-rules を実行します", flush=True)
    return agents


def text(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return html.escape(value, quote=True)


def answer_html(value):
    escaped = text(value)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"(?m)^#{1,6} (.+)$", r"<strong>\1</strong>", escaped)


def result_card(variant, record, pending=False):
    if record is None:
        return '<article><h3>' + LABELS[variant] + '</h3><p class="muted">' + ("実行待ちです。ターミナルの処理が終わってからページを更新してください。" if pending else "定義ファイルの 2 か所を編集して、同じコマンドをもう一度実行してください。") + '</p></article>'
    evidence = extract(record.get("messages", []))
    final = "\n\n".join("\n".join(t.get("parts", [])) for t in evidence["texts"] if t.get("textType") == "FINAL_RESPONSE")
    good = record.get("state") == "completed" and bool(final) and not evidence["errors"]
    card = '<article><h3>' + LABELS[variant] + '</h3><p class="badge">' + ("回答取得済み・正誤は要確認" if good else "未完了・要確認") + '</p>'
    card += '<p class="muted">' + text(record.get("started_at", "")) + ' ｜ ' + text(record.get("elapsed_seconds", "未取得")) + ' 秒</p>'
    if final:
        card += '<div class="answer">' + answer_html(final) + '</div>'
    if not good:
        card += '<p class="error">取得に失敗した場合は正解・不正解に数えず、エラーを確認してください。</p><pre>' + text(record.get("error") or evidence["errors"] or record.get("state")) + '</pre>'
    for result in evidence["data"]:
        rows = result.get("data", [])
        if rows and all(isinstance(row, dict) for row in rows):
            keys = list(dict.fromkeys(key for row in rows for key in row))
            card += '<div class="table"><table><thead><tr>' + ''.join('<th>' + text(k) + '</th>' for k in keys) + '</tr></thead><tbody>'
            card += ''.join('<tr>' + ''.join('<td>' + text(row.get(key, "")) + '</td>' for key in keys) + '</tr>' for row in rows) + '</tbody></table></div>'
    card += '<details><summary>生成 SQL を確認する（' + str(len(evidence["sql"])) + ' 件取得）</summary>'
    card += ''.join('<pre>' + text(sql) + '</pre>' for sql in evidence["sql"]) or '<p>SQL メッセージはありません。</p>'
    receipt = record.get('lab', {}).get('catalog_receipt')
    if receipt:
        card += '</details><details><summary>Catalog から取得した定義</summary><p>教材コードが取得し、Agent の用語定義欄へ渡しました。</p><pre>' + text({k: receipt.get(k) for k in ('entry', 'update_time', 'retrieved_at', 'body_sha256')}) + '</pre><pre>' + text(receipt.get('body', '')) + '</pre>'
    card += '</details><details><summary>実行した設定</summary><pre>' + text({k: v for k, v in record.get("lab", {}).items() if k != 'catalog_receipt'}) + '</pre></details></article>'
    return card


def render(cfg, selected, records, rules, run_id, recorded=False):
    sections = []
    for case in selected:
        cards = ''.join(result_card(v, records.get((v, case["case_id"])), bool(rules.strip()) and "[[" not in rules) for v in LABELS)
        sections.append('<section><h2>' + text(case["case_id"] + '　' + case["question_ja"]) + '</h2><div class="cards">' + cards + '</div></section>')
    page = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>#1 売上とは何か</title>
<style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f5f7fb;color:#202d48;font:16px/1.7 system-ui,sans-serif}main{max-width:1500px;margin:auto;padding:36px 24px}header{background:#2B356B;color:white;padding:30px;border-radius:18px}h1{font-size:32px;line-height:1.4;margin:8px 0}h2{font-size:21px;margin-top:38px}h3{font-size:20px;margin:0 0 12px}.eyebrow{color:#b5ecdf}header p{margin:8px 0}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}article,.guide{background:white;border:1px solid #d8e1ec;border-radius:14px;padding:22px;margin-top:16px;min-width:0}.badge{font-size:12px;color:#13655d;background:#eaf5f5;border-radius:6px;padding:5px 10px;display:inline-block}.muted{font-size:12px;color:#58667a;overflow-wrap:anywhere}.answer{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}pre{font:12px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f9;padding:14px;border-radius:8px}details{border-top:1px solid #dde3ec;margin-top:18px;padding-top:12px}summary{cursor:pointer;font-weight:600}.table{overflow:auto;margin-top:18px}table{border-collapse:collapse;font-size:12px}th,td{padding:8px;border:1px solid #d8e1ec;text-align:left;white-space:nowrap}th{background:#eef3f8}.error{color:#a33333}code{background:#edf2f6;padding:2px 5px;border-radius:4px}li{margin:8px 0}a{color:#33578E}article{border-top:3px solid #3FA4AC}:focus-visible{outline:3px solid #33578E;outline-offset:4px}summary{min-height:48px}@media(max-width:1000px){.cards{grid-template-columns:1fr}main{padding:18px}h1{font-size:26px}}@media print{details{display:block}.cards{display:block}article{break-inside:avoid}}
</style><main><header><div class="eyebrow">SEMANTICS · LAB 01</div><h1>その「売上」、同じ意味で話していますか？</h1><p>業務定義 → 回答 → 根拠をたどり、意思決定に使える数字か確認しましょう。</p><p>対象：''' + text(cfg["project_id"]) + ' / ' + text(cfg["dataset_id"]) + '''</p></header>
<div class="guide"><strong>進め方</strong><ol><li>①の回答を読み、「売上」と返金の扱いを確認する。</li><li><code>participant/semantics.txt</code>の 2 か所を、返金日と実際に回収した原価の列名に置き換える。</li><li><code>uv run --locked python3 -m workshop.lab1</code>を再実行し、このページを更新する。</li></ol><p>数値の一致だけでは十分ではありません。説明・対象期間・SQL も確認してください。</p><p>集計対象：2026 年 8 月 1 日〜8 月 31 日。前月比較：7 月 1 日〜7 月 31 日。SQL では終了日を含みません。</p><p class="muted">同じ設定で再実行した場合は保存済み回答を表示します。自動採点ではありません。費用は未計測です。比較版：''' + text(run_id) + '</p></div>' + ''.join(sections) + '''
<div class="guide"><h2>答え合わせと振り返り</h2><details><summary>公開問題の基準値を開く</summary><p>Q01：純売上 240,000 円＝注文総額 420,000 円−値引 65,000 円−返金 115,000 円。</p><p>Q04：Electronics、8 月−24,000 円−7 月 40,000 円＝−64,000 円。</p><p>この値は回答を確認するための基準です。Agent の指示には含めていません。</p></details><ul><li>①が正答した場合も、他の集計基準を併記していないか確認する。</li><li>②が誤答した場合は、業務定義の不足と SQL の実装ミスを分けて考える。</li><li>③でも生成 SQL の正しさは保証されない。View が引き受けた計算は何か？</li></ul><details><summary>今回のあなたの業務定義</summary><pre>''' + text(rules) + '''</pre></details><details><summary>完了チェック</summary><ul><li>売上と返金をどの月に計上するか説明できる。</li><li>定義の編集前後で、同じ質問の回答・表・SQL を確認した。</li><li>自社では誰が定義を決め、どこに実装し、誰が検証するか考えた。</li></ul></details></div></main></html>'''
    if recorded:
        page = page.replace('<div class="eyebrow">SEMANTICS · LAB 01</div>', '<div class="eyebrow">過去の実測記録 · ライブ実行ではありません</div>')
        start = page.index('<div class="guide"><strong>進め方</strong>')
        end = page.index('<section>', start)
        page = page[:start] + '<div class="guide"><strong>Catalog 統合版の実測記録を再生しています</strong><p>①は 2026 年 9 月 5 日、②③は 9 月 6 日の記録です。識別子を匿名化しています。あなたの Project への接続・推論は行っていません。ここでは定義を編集しても回答は変わりません。質問と 3 構成の違いを読み、基準値・説明・SQL を比較してください。</p></div>' + page[end:]
    return page


def example(root=ROOT):
    bundle = json.loads((root / "examples/lab1-recorded.json").read_text(encoding="utf-8"))
    records = {(r["variant"], r["case_id"]): r["record"] for r in bundle["records"]}
    selected = [c for c in cases() if c["case_id"] in ("Q01", "Q04")]
    page = render({"project_id": "過去の検証環境（匿名化）", "dataset_id": "ec_workshop"}, selected, records, bundle["rules"], bundle["recording_id"], recorded=True)
    out = root / "artifacts/lab1/example.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print("過去の実測記録: artifacts/lab1/example.html\n表示: uv run --locked python3 -m workshop.lab1 serve --example")
    return out


def run(root=ROOT, account=None, repeat=False, selected_ids=("Q01", "Q04"), client_factory=AgentClient, catalog_factory=Catalog):
    cfg = config(require_project=True, root=root)
    validate_cloud_config(cfg)
    path = initialize(root)
    rules = path.read_text(encoding="utf-8").strip()
    if len(rules) > 30000:
        raise ValueError("定義が長すぎます。participant/semantics.txtを確認してください。")
    receipt = None
    if rules.strip() and '[[' not in rules and ']]' not in rules:
        print('Catalogの定義を同期・取得しています…', flush=True)
        receipt = catalog_factory(cfg, account, root=root).sync(as_document(rules))
    contexts = make_contexts(cfg, rules, receipt)
    selected = [case for case in cases() if case["case_id"] in selected_ids]
    client = client_factory(cfg, account)
    root_folder = root / "artifacts/lab1" / cfg["project_id"] / cfg["dataset_id"] / cfg["data_agent_location"]
    comparison = fingerprint({"contexts": contexts, "questions": selected, "data_version": cfg["definition_version"]})
    run_id = comparison + ("-" + uuid.uuid4().hex[:8] if repeat else "")
    folder = root_folder / run_id
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = {"created_at": timestamp(), "config": cfg, "contexts": contexts, "rules": rules, "questions": selected}
    if not (folder / "snapshot.json").exists():
        save(folder / "snapshot.json", snapshot)
    records = {}
    failures = 0
    print("対象Project: " + cfg["project_id"] + " / " + cfg["dataset_id"], flush=True)
    print("編集ファイル: participant/semantics.txt", flush=True)
    print("1問に数十秒かかります。過去のPoCでは最大約46秒。今回の上限を保証する値ではありません。", flush=True)
    def publish():
        page = render(cfg, selected, records, rules, run_id)
        (folder / "index.html").write_text(page, encoding="utf-8")
        public = root / "artifacts/lab1/index.html"
        public.write_text(page, encoding="utf-8")
    publish()
    for variant, ctx in contexts.items():
        # Cache per context/question: editing rules keeps the original baseline.
        identity = fingerprint({"context": ctx, "questions": selected, "data_version": cfg["definition_version"]})
        cache = root_folder / (identity + ("-" + run_id if repeat else ""))
        cache.mkdir(parents=True, exist_ok=True)
        name = None
        for case in selected:
            out = cache / (variant + "." + case["case_id"] + ".json")
            if out.exists():
                record = json.loads(out.read_text(encoding="utf-8"))
                print(LABELS[variant] + " " + case["case_id"] + "：保存済みの記録を表示", flush=True)
            else:
                if name is None:
                    name = client.ensure(variant, ctx)
                print(LABELS[variant] + " " + case["case_id"] + "：Data Agentへ質問を送信します", flush=True)
                print("Agent: " + name, flush=True)
                print("質問: " + case["question_ja"], flush=True)
                print("Data AgentがBigQueryを参照し、必要なSQLを生成・実行します。回答を待っています…", flush=True)
                record = client.chat(name, case["question_ja"], out)
                record["lab"] = {"context_hash": fingerprint(ctx), "definition_version": cfg["definition_version"], "variant": variant}
                if variant != 'raw':
                    record['lab']['catalog_receipt'] = receipt
                save(out, record)
            records[(variant, case["case_id"])] = record
            final = [t for t in extract(record.get("messages", []))["texts"] if t.get("textType") == "FINAL_RESPONSE"]
            good = record["state"] == "completed" and bool(final)
            failures += not good
            save(folder / (variant + "." + case["case_id"] + ".json"), record)
            publish()
            if not good:
                print("未完了の記録を保持しました。エラーを確認し、解決後は --repeat で新しく実行してください。", flush=True)
                return 1
    if len(contexts) == 1:
        print("次：participant/semantics.txt の [[...]] 2か所を編集し、同じコマンドを再実行してください。", flush=True)
    else:
        print("3構成の比較を保存しました。数値・説明・SQLを確認して振り返りに進んでください。", flush=True)
    print("結果: artifacts/lab1/index.html\nブラウザ表示: uv run --locked python3 -m workshop.lab1 serve（Cloud Shellのウェブプレビュー → 8080）", flush=True)
    return 1 if failures else 0


def serve(root, port, recorded=False):
    page = root / ("artifacts/lab1/example.html" if recorded else "artifacts/lab1/index.html")
    if recorded and not page.exists():
        example(root)  # 再生例は同梱の記録から作れるので、事前の example 実行を求めない
    if not page.exists():
        raise ValueError("先に uv run --locked python3 -m workshop.lab1 を実行してください。")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print("http://localhost:%d — Cloud Shellではウェブプレビューを開きます。終了はCtrl+C。" % port, flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="#1：Knowledge Catalogで定義を編集し、同じ質問の回答を比較する")
    parser.add_argument("action", nargs="?", choices=["run", "prepare", "apply-rules", "serve", "init", "example"], default="run")
    parser.add_argument("--example", action="store_true", help="serveで過去の実測記録を表示")
    parser.add_argument("--account", help="Cloud Shellでは通常不要")
    parser.add_argument("--repeat", action="store_true", help="過去の記録を保持して再測定（新たな推論が発生）")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--cases", nargs="+", choices=["Q01", "Q04"], default=["Q01", "Q04"])
    args = parser.parse_args()
    try:
        if args.action == "example":
            example()
            return 0
        if args.action == "init":
            print(initialize())
            return 0
        if args.action == "serve":
            serve(ROOT, args.port, args.example)
            return 0
        lock_path = ROOT / "artifacts/lab1/.run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("別の #1実行が進行中です。元のターミナルの完了を待ってください。")
            if args.action in ("prepare", "apply-rules"):
                prepare_console(account=args.account, selected_ids=args.cases, apply=args.action == "apply-rules")
                return 0
            return run(account=args.account, repeat=args.repeat, selected_ids=args.cases)
    except KeyboardInterrupt:
        print("\n停止しました。保存済みの記録と編集は残ります。")
        return 130
    except StepNotReady as exc:
        print("まだ実行できません: " + str(exc), file=sys.stderr)
        return 2
    except (CloudError, ValueError, OSError) as exc:
        print("確認が必要です: " + str(exc), file=sys.stderr)
        print("設定・権限を確認してください。participant/ と artifacts/ の記録は削除しないでください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

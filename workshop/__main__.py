import argparse
import hashlib
import json
import sys
from pathlib import Path

from .bigquery import render
from .data import ROOT, config, connect, export_seed
from .evaluation import evaluate
from .reference import all_answers, illustrative_baseline
from .settings import configure_project


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    path.write_text("".join(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n" for value in values), encoding="utf-8")


def read_jsonl(path):
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON value: " + value))))
        except ValueError as exc:
            raise ValueError("Invalid JSON at line %d: %s" % (number, exc)) from exc
    return result


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    cfg = config(require_project=True)
    counts = export_seed(output / "seed")
    answers = all_answers()
    baseline = illustrative_baseline(answers)
    write_jsonl(output / "reference_answers.jsonl", answers)
    write_jsonl(output / "illustrative_baseline.jsonl", baseline)
    reference_report = evaluate(answers, answers)
    baseline_report = evaluate(baseline, answers)
    write_json(output / "reference_evaluation.json", reference_report)
    write_json(output / "illustrative_baseline_evaluation.json", baseline_report)
    render(cfg, output / "bigquery")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((output / "seed").glob("*.csv"))}
    write_json(output / "manifest.json", {
        "backend": "local_sqlite_reference", "cloud_verified": False,
        "project_id": cfg["project_id"], "definition_version": cfg["definition_version"],
        "table_rows": counts, "seed_sha256": hashes,
        "public_cases_sha256": hashlib.sha256((ROOT / "data/public_cases.jsonl").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
    })
    db = connect()
    try:
        month_rows = db.execute("""SELECT substr(event_date, 1, 7) AS month,
            SUM(gross_sales_jpy) AS gross, SUM(discount_jpy) AS discount,
            SUM(refund_jpy) AS refund, SUM(net_sales_jpy) AS net,
            SUM(cogs_jpy) AS cogs, SUM(gross_profit_jpy) AS profit
            FROM revenue_events WHERE event_date < ? GROUP BY month ORDER BY month""", (cfg["as_of_date"],)).fetchall()
    finally:
        db.close()
    lines = [
        "# ローカル準備結果", "",
        "Google Cloud・Geminiは呼び出していません。以下は合成データの基準集計と採点器の動作確認です。", "",
        "- 設定したProject: `" + cfg["project_id"] + "`（接続状態は未確認）",
        "- 定義: `" + cfg["definition_version"] + "`",
        "- 公開問題: %d問" % len(answers), "",
        "| 月 | 注文総額 | 値引き | 返金 | 純売上 | 売上原価 | 粗利益 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in month_rows:
        lines.append("| " + row["month"] + " | " + " | ".join(format(row[key], ",") for key in ("gross", "discount", "refund", "net", "cogs", "profit")) + " |")
    lines += ["", "金額はJPY。キャンセル注文は除外。返金は返金日で計上。", "",
              "| 入力 | 合格問題数 | 意味 |", "|---|---:|---|",
              "| 基準集計の回答 | %d / %d | 採点器との整合確認 |" % (reference_report["successful_cases"], len(answers)),
              "| 意図的な誤答サンプル | %d / %d | 金額・期間などの誤りを検出できるかの確認 |" % (baseline_report["successful_cases"], len(answers)),
              "", "**この差はLLMの改善効果を示すものではありません。実際のBefore / Afterはクラウド承認後に検証します。**", "",
              "## クラウド承認後に確認すること", "",
              "- 課金、API、IAM、Quota、リージョン・モデルの組み合わせ",
              "- CSV投入とBigQueryでのSQL実行結果がローカル基準値と一致すること",
              "- Data AgentのBaseline / 完成版の実測比較",
              "- ADK接続、Agent Runtimeへのデプロイ、Trace、費用の帰属",
              "", "費用・応答時間は未測定です。", ""]
    (output / "local-report.md").write_text("\n".join(lines), encoding="utf-8")
    print("ローカル準備完了: " + str(output.resolve() / "local-report.md"))
    print("公開問題 %d問 / 基準回答 %d問合格 / 意図的な誤答サンプル %d問合格" % (len(answers), reference_report["successful_cases"], baseline_report["successful_cases"]))
    print("Google Cloud接続なし。モデル品質・費用・応答時間は未検証。")


def main():
    parser = argparse.ArgumentParser(description="Cloud接続不要のEC分析Agent教材準備ツール")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure", help="参加者ごとのProject ID・リージョンをローカル設定へ保存。Cloud接続なし")
    setup.add_argument("--project", required=True)
    setup.add_argument("--bigquery-location")
    setup.add_argument("--data-agent-location")
    setup.add_argument("--runtime-location")
    setup.add_argument("--catalog-location")
    prep = sub.add_parser("prepare", help="合成CSV、正解、誤答サンプル、BigQuery SQL、レポートを生成")
    prep.add_argument("--output", type=Path, default=ROOT / "artifacts")
    scoring = sub.add_parser("evaluate", help="JSONL回答を公開問題の正解と比較")
    scoring.add_argument("--answers", type=Path, required=True)
    scoring.add_argument("--output", type=Path, default=ROOT / "artifacts/evaluation.json")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            path = configure_project(args.project, bigquery_location=args.bigquery_location,
                                     data_agent_location=args.data_agent_location,
                                     runtime_location=args.runtime_location, catalog_location=args.catalog_location)
            print("Project設定を保存しました: " + str(path))
            print("Google Cloudへの接続・権限確認はまだ行っていません。")
        elif args.command == "prepare":
            prepare(args.output)
        else:
            report = evaluate(read_jsonl(args.answers), all_answers())
            write_json(args.output, report)
            print("%d / %d 問合格: %s" % (report["successful_cases"], report["total_cases"], args.output.resolve()))
            return 0 if report["successful_cases"] == report["total_cases"] else 1
    except (OSError, ValueError) as exc:
        print("エラー: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

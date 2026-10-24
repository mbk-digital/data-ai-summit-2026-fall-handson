"""Shared teaching defaults and untracked participant-specific Cloud settings."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_KEYS = {
    "project_id", "dataset_id", "metrics_dataset_id", "bigquery_location",
    "data_agent_location", "runtime_location", "catalog_location", "model_ids",
}


PLACEHOLDER_PROJECT = "YOUR_PROJECT_ID"


def validate_project(project):
    if project == PLACEHOLDER_PROJECT:
        # 手順書のコマンドをそのまま貼った場合。書式の説明より、直す場所を示す。
        raise ValueError("YOUR_PROJECT_ID を、自分の Project ID（例：my-project-123）に書き換えてから実行してください。")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Project IDは6〜30文字の英小文字・数字・ハイフンで指定してください。")


def local_settings(root):
    path = root / "config/workshop.local.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) - LOCAL_KEYS:
        raise ValueError("参加者設定に未対応の項目があります。業務定義は共通設定で管理します。")
    return value


def config(require_project=False, root=None):
    root = ROOT if root is None else Path(root)
    value = json.loads((root / "config/workshop.json").read_text(encoding="utf-8"))
    value.update(local_settings(root))
    project = value.get("project_id")
    if project is not None:
        validate_project(project)
    if require_project and project is None:
        raise ValueError("先に uv run --locked python3 -m workshop configure --project YOUR_PROJECT_ID で自分のProject IDを設定してください。")
    return value


def configure_project(project, root=None, **overrides):
    validate_project(project)
    root = ROOT if root is None else Path(root)
    value = local_settings(root)
    value["project_id"] = project
    for key, item in overrides.items():
        if key not in {"bigquery_location", "data_agent_location", "runtime_location", "catalog_location"}:
            raise ValueError("未対応の環境設定です: " + key)
        if item is not None:
            if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", item):
                raise ValueError("Invalid location: " + str(item))
            value[key] = item
    path = root / "config/workshop.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

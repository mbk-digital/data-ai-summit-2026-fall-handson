"""Build a clean GitHub source tree without local state. No cloud/network access."""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Explicit source roots: never recursively export the developer checkout.
FILES = ['.gitignore', 'README.md', 'pyproject.toml', 'uv.lock', 'requirements-adk.txt', 'config/workshop.json']
DIRS = ['workshop', 'tests', 'data', 'semantics', 'examples', 'docs', 'infra', 'agent', 'scripts']
ALLOWED_SUFFIXES = {'.py', '.md', '.json', '.jsonl', '.sql', '.txt'}
# Catch common accidentally embedded credentials and developer context.
# Shared with tests so the check and its subject cannot drift apart.
PATTERNS = [r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'"private_key"\s*:',
            r'[/]Users[/][A-Za-z0-9._-]+/', r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
            r'(?<![A-Za-z0-9_])\d{12}(?![A-Za-z0-9_])']
# Project IDs and other names the patterns cannot recognize. One per line, kept out of Git
# (see .gitignore); absent on participants' machines, where the list is simply empty.
DENYLIST = 'config/private-denylist.txt'


DISTRIBUTION_FILES = ['.gitignore', 'LICENSE', 'README.md', 'agent/root-instructions.md', 'config/workshop.json', 'data/bigquery_schema.sql', 'data/evaluation-cases.json', 'data/pricing-2026-10-07.json', 'data/public_cases.jsonl', 'docs/business-definitions.md', 'docs/cloud-shell-workflow.md', 'docs/labs/backup.md', 'docs/labs/cleanup.md', 'docs/labs/decision-sheet.md', 'docs/labs/lab1.md', 'docs/labs/lab2.md', 'docs/labs/map.md', 'docs/labs/pre-event.md', 'docs/labs/preparation.md', 'examples/lab1-recorded.json', 'pyproject.toml', 'requirements-adk.txt', 'scripts/export_distribution.py', 'semantics/baseline-instructions.md', 'semantics/baseline.txt', 'semantics/business-rules.txt', 'semantics/complete-instructions.md', 'semantics/graph/create.sql', 'semantics/graph/inspect-expand.sql', 'semantics/graph/inspect-gql.sql', 'semantics/graph/multiple-refunds.sql', 'semantics/lab1-starter.txt', 'semantics/okf/index.md', 'semantics/okf/net-revenue.md', 'semantics/verified_queries/monthly_metrics.sql', 'semantics/verified_queries/top_product.sql', 'tests/test_benchmark.py', 'tests/test_catalog.py', 'tests/test_checkpoint.py', 'tests/test_cleanup.py', 'tests/test_cloud.py', 'tests/test_comparison.py', 'tests/test_data_agent.py', 'tests/test_data_agent_display_name.py', 'tests/test_dependency_sync.py', 'tests/test_distribution_scan.py', 'tests/test_economics.py', 'tests/test_lab1.py', 'tests/test_observability.py', 'tests/test_provenance.py', 'tests/test_resource_state.py', 'tests/test_root_evidence.py', 'tests/test_routes.py', 'tests/test_semantics_poc.py', 'tests/test_setup.py', 'tests/test_teardown.py', 'tests/test_trace_url.py', 'tests/test_web.py', 'tests/test_workshop.py', 'uv.lock', 'workshop/__init__.py', 'workshop/__main__.py', 'workshop/benchmark.py', 'workshop/benchmark_report.py', 'workshop/bigquery.py', 'workshop/catalog.py', 'workshop/checkpoint.py', 'workshop/cleanup.py', 'workshop/cloud.py', 'workshop/data.py', 'workshop/data_agent.py', 'workshop/economics.py', 'workshop/evaluation.py', 'workshop/lab1.py', 'workshop/ledger_refresh.py', 'workshop/observability.py', 'workshop/provenance.py', 'workshop/reference.py', 'workshop/resource_state.py', 'workshop/root_agent.py', 'workshop/root_evidence.py', 'workshop/routes.py', 'workshop/semantics_poc.py', 'workshop/settings.py', 'workshop/setup.py', 'workshop/teardown.py', 'workshop/web.py', 'workshop/web_assets/app.js', 'workshop/web_assets/index.html', 'workshop/web_assets/styles.css']

def distributed_paths(root=ROOT):
    """Every file the distribution would contain. Shared with tests."""
    return [root / name for name in DISTRIBUTION_FILES]


def denylist(root=ROOT):
    """Strings that must never be distributed, read from the local, untracked list."""
    path = root / DENYLIST
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding='utf-8').splitlines())
    return [line for line in lines if line and not line.startswith('#')]


def private_content(content, denied=None):
    """The first matching pattern or denied string, or None. Shared with tests."""
    for pattern in PATTERNS:
        found = re.search(pattern, content)
        if found:
            return found.group(0)
    for value in denylist() if denied is None else denied:
        if value in content:
            return value
    return None


def export(output):
    contents = {}
    denied = denylist()
    for path in distributed_paths():
        if path.is_symlink():
            raise ValueError('Symlinks cannot be distributed: ' + str(path))
        content = path.read_text(encoding='utf-8')
        if private_content(content, denied) is not None:
            raise ValueError('Review potentially private content: ' + str(path.relative_to(ROOT)))
        contents[path.relative_to(ROOT)] = content
    if output.exists():
        raise ValueError('Output already exists; choose a new folder: ' + str(output))
    output.mkdir(parents=True)
    for relative, content in contents.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    print('Exported {} source files to {}'.format(len(contents), output))
    print('Local configuration, participant edits, artifacts and Git history are excluded.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/github-distribution')
    args = parser.parse_args()
    try:
        export(args.output.resolve())
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')

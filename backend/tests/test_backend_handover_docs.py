"""Read repository text only; no application import, database or subprocess."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / 'docs/backend'


def test_handover_links_resolve():
    for name in ('BACKEND_HANDOVER.md', 'API.md', 'DEPLOYMENT.md',
                 'STUDENT_NFC.md', 'SCANNER_PROVISIONING.md'):
        text = (DOCS / name).read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if '://' not in target and not target.startswith('#'):
                assert (DOCS / target.split('#')[0]).exists(), (name, target)


def test_documented_application_head_matches_migration_graph():
    revisions = {}
    for folder in ('versions', 'application_versions'):
        for path in (ROOT / 'backend/alembic' / folder).glob('*.py'):
            values = {}
            for node in ast.parse(path.read_text(encoding='utf-8')).body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {'revision', 'down_revision'}:
                            values[target.id] = ast.literal_eval(node.value)
            if 'revision' in values:
                revisions[values['revision']] = values.get('down_revision')
    parents = set()
    for parent in revisions.values():
        parents.update(parent if isinstance(parent, tuple) else [parent])
    heads = set(revisions) - parents
    assert heads == {'f6c1a8d420be'}
    for name in ('BACKEND_HANDOVER.md', 'API.md', 'DEPLOYMENT.md',
                 'STUDENT_NFC.md', 'SCANNER_PROVISIONING.md'):
        assert next(iter(heads)) in (DOCS / name).read_text(encoding='utf-8')

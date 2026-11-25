"""Create an isolated demo data folder filled with entirely synthetic records.

Everything here is fictional: the institution, modules, people, assessments, emails
and documents. Dates are relative to the day the demo is built, so the dashboard
always has upcoming, overdue and completed work to explore.

Safety: the builder only writes inside a folder that is empty or already carries the
``.planner-demo`` marker. It refuses to touch any folder holding other data, so it
cannot overwrite a personal database.

Run ``python demo_data.py --reset`` to rebuild the demo from scratch.
"""

import argparse
import hashlib
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

MARKER = '.planner-demo'
BASE = 'https://moodle.example.invalid'
MODULES = [
    ('101', 'DEMO101 Software Engineering Fundamentals'),
    ('102', 'DEMO102 Database Systems'),
    ('103', 'DEMO103 Web Development'),
    ('104', 'DEMO104 Professional Practice'),
]


class DemoFolderError(RuntimeError):
    """The target folder contains data that is not demo data."""


def _day(offset: int) -> date:
    """The date ``offset`` days from today."""
    return date.today() + timedelta(days=offset)


def _long(value: date) -> str:
    """Format a date like "Friday 11 December 2026"."""
    return f'{value:%A} {value.day} {value:%B %Y}'


ASSIGNMENTS = [
    # activity id, module id, title, due offset, weighting, progress, priority, brief type
    ('201', '101', 'CA1 Requirements Specification', -20, '20%', 100, 'Medium', 'pdf'),
    ('202', '101', 'CA2 Group Project Report', 9, '40%', 35, 'High', 'pdf'),
    ('203', '102', 'CA1 Entity Relationship Design', -4, '25%', 60, 'High', 'docx'),
    ('204', '102', 'CA2 SQL Query Portfolio', 16, '35%', 0, 'Medium', 'docx'),
    ('205', '103', 'CA1 Accessible Landing Page', 2, '30%', 80, 'High', 'pdf'),
    ('206', '103', 'CA2 JavaScript Application', 30, '50%', 10, 'Medium', None),
    ('207', '104', 'Reflective Learning Journal', 5, '15%', 50, 'Low', 'docx'),
    ('208', '104', 'Career Development Plan', -35, '10%', 100, 'Low', None),
]


def brief_lines(activity: str, module: str, title: str, due: date, weighting: str) -> list[str]:
    """Synthetic assessment brief text that exercises the extraction rules."""
    code, name = module.split(' ', 1)
    issue = due - timedelta(days=28)
    return [
        'Example Institute of Technology (fictional)',
        f'Module: {code} {name}',
        f'Assessment Title: {title}',
        f'Weighting: {weighting} of the overall module mark',
        f'Issue date: {issue.day} {issue:%B %Y}',
        f'Submission deadline: {_long(due)} at 17:00',
        f'Presentations will take place on {(due + timedelta(days=3)):%d/%m/%Y}.',
        f'Feedback will be returned by {(due + timedelta(days=20)).day} '
        f'{(due + timedelta(days=20)):%B %Y}.',
        'Word count: 1,500 - 2,000 words',
        '',
        'Deliverables:',
        '- A report in PDF format',
        '- A link to the shared project repository',
        '',
        'Marking criteria:',
        '1. Analysis and understanding 30%',
        '2. Design quality 40%',
        '3. Presentation and referencing 30%',
        '',
        'Important: Submit through the module page. Keep a copy of your work.',
        f'Reference: demo assessment {activity}. This document is synthetic demo data.',
    ]


def brief_blocks(lines: list[str]) -> list[dict]:
    """Turn brief lines into Word blocks: headings, list items and a table."""
    blocks = []
    for line in lines:
        if not line:
            continue
        if line.endswith(':'):
            blocks.append({'type': 'heading', 'level': 2, 'text': line.rstrip(':')})
        elif line.startswith(('- ', '1. ', '2. ', '3. ')):
            blocks.append({'type': 'list_item', 'text': line.split(' ', 1)[1]})
        else:
            blocks.append({'type': 'paragraph', 'text': line})
    blocks.insert(
        1,
        {
            'type': 'table',
            'rows': [
                ['Item', 'Detail'],
                ['Format', 'Individual submission'],
                ['Late work', '10% per day'],
            ],
        },
    )
    return blocks


def ensure_demo_folder(root: Path, reset: bool) -> bool:
    """Return True when the demo must be (re)built. Never touches non-demo folders."""
    # start.ps1 may create the log folder before the demo is built.
    runtime = {'planner_logs', '.planner-server.lock', '.planner-server.json'}
    existing = (
        [item for item in root.iterdir() if item.name not in runtime] if root.exists() else []
    )
    if existing and not (root / MARKER).is_file():
        raise DemoFolderError(
            f'{root} already contains files that are not demo data. Choose an empty folder.'
        )
    if (root / MARKER).is_file() and (root / 'deadlines.db').is_file() and not reset:
        return False
    if (root / MARKER).is_file() and reset:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text('Synthetic Deadline Planner demo data.\n', encoding='utf-8')
    return True


def build_demo(root: Path, reset: bool = False) -> bool:
    """Create the demo database and documents. Returns True if anything was written."""
    root = Path(root).resolve()
    if not ensure_demo_folder(root, reset):
        return False

    from database import initialize_database, open_database, save_imports, update_document_registry
    from document_processing import process_document
    from document_store import publish_documents
    from local_files import write_atomic
    from planner_data import AppPreferences, SyncSettings
    from synthetic_documents import build_docx, build_pdf

    initialize_database(root)
    modules = {key: name for key, name in MODULES}
    rows, documents = [], {}
    for activity, module_id, title, offset, weighting, _, _, brief in ASSIGNMENTS:
        due = _day(offset)
        link = f'{BASE}/mod/assign/view.php?id={activity}'
        rows.append(
            dict(
                key=f'demo-{activity}',
                title=title,
                due=due.isoformat(),
                category=modules[module_id],
                notes=f'Synthetic assignment description for {title}.',
                link=link,
                module_id=module_id,
                weighting=weighting,
            )
        )
        if not brief:
            continue
        lines = brief_lines(activity, modules[module_id], title, due, weighting)
        data = build_pdf(lines) if brief == 'pdf' else build_docx(brief_blocks(lines))
        processed = process_document(data, lambda: None)
        digest = hashlib.sha256(data).hexdigest()
        folder = root / 'assessment_pdfs'
        folder.mkdir(exist_ok=True)
        write_atomic(folder / f'{digest}.{processed["file_type"]}', data)
        key = f'assignment:{activity}'
        documents[digest] = dict(
            id=digest,
            sha256=digest,
            title=f'{title} brief.{processed["file_type"]}',
            file_type=processed['file_type'],
            module=modules[module_id],
            module_id=module_id,
            module_ids=[module_id],
            text=processed['text'],
            assignment=title,
            assignment_key=key,
            assignment_keys=[key],
            document_date=due.isoformat(),
            added_at=f'{_day(offset - 28).isoformat()}T09:00:00+00:00',
            resource_keys=[f'{module_id}:activity:{activity}'],
            resource_versions={},
            source='Demo',
            **processed['flags'],
        )
    catalog = {'modules': [{'id': key, 'name': name} for key, name in MODULES], 'assignments': []}
    save_imports(root, 'Moodle', rows, catalog)
    emails = [
        dict(
            key='demo-mail-1:0',
            identity='message:demo-mail-1',
            title='Reminder: lab report deadline moved',
            due=_day(12).isoformat(),
            notes='Synthetic email: the deadline is now next week.',
            link='',
            review=1,
        )
    ]
    save_imports(root, 'Outlook', emails, {})
    gmail = [
        dict(
            key='demo-mail-2:0',
            identity='message:demo-mail-2',
            title='Study group: submit slides by Friday',
            due=_day(6).isoformat(),
            notes='Synthetic email from a fictional study group.',
            link='',
            review=1,
        )
    ]
    save_imports(root, 'Gmail', gmail, {})
    publish_documents(root, documents, observed={})

    with open_database(root) as connection:
        for activity, *_, progress, priority, _ in ASSIGNMENTS:
            connection.execute(
                'UPDATE tasks SET progress=?, priority=? WHERE external=?',
                (progress, priority, f'Moodle:demo-{activity}'),
            )
        connection.executemany(
            'INSERT INTO tasks(title,due,category,notes,priority,progress,source) VALUES(?,?,?,?,?,?,?)',
            [
                (
                    'Book a meeting with project supervisor',
                    _day(1).isoformat(),
                    'DEMO101 Software Engineering Fundamentals',
                    '',
                    'Medium',
                    0,
                    'Manual',
                ),
                (
                    'Revise normalisation notes',
                    _day(-1).isoformat(),
                    'DEMO102 Database Systems',
                    'Overdue example.',
                    'Low',
                    20,
                    'Manual',
                ),
                (
                    'Submit placement form',
                    _day(-10).isoformat(),
                    'DEMO104 Professional Practice',
                    '',
                    'High',
                    100,
                    'Manual',
                ),
            ],
        )
        connection.execute(
            'INSERT INTO planner_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
            (SyncSettings(automatic=False, sources=[]).model_dump_json(),),
        )
        connection.execute(
            'INSERT INTO app_preferences VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
            (AppPreferences(setup_complete=True, reminder_days=[7, 1]).model_dump_json(),),
        )
        connection.execute(
            'INSERT INTO sync_history VALUES(?,?,?,?,?,?,?)',
            (
                'demo-history-1',
                f'{_day(-1).isoformat()}T09:00:00+00:00',
                f'{_day(-1).isoformat()}T09:02:00+00:00',
                'sync',
                'Complete',
                json.dumps({'modules': 4, 'assignments': 8, 'new_pdfs_downloaded': 5}),
                '[]',
            ),
        )
        update_document_registry(connection, root)
    return True


def main(argv=None) -> int:
    """Command-line entry point for building or rebuilding the demo folder."""
    parser = argparse.ArgumentParser(description='Build the synthetic demo data folder.')
    parser.add_argument(
        '--output', type=Path, default=Path(__file__).resolve().parent / 'demo-data'
    )
    parser.add_argument('--reset', action='store_true', help='rebuild an existing demo folder')
    args = parser.parse_args(argv)
    try:
        built = build_demo(args.output, args.reset)
    except DemoFolderError as error:
        parser.exit(2, f'{error}\n')
    print(('Built' if built else 'Kept existing') + f' demo data in {args.output.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

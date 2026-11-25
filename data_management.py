"""Confirmed maintenance with an SQLite snapshot and recoverable file moves."""

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from database import refresh_task_projection, update_document_registry
from local_files import write_atomic
from planner_data import (
    DOCUMENT_TYPES,
    PDF_ID,
    combine_documents,
    document_filename,
    group_documents,
    read_documents,
)
from task_identity import combined_task, email_identity, task_groups

ACTIONS = {
    'moodle': 'Clear imported Moodle deadlines',
    'pdfs': 'Clear downloaded assessment documents',
    'outlook': 'Clear Outlook imports',
    'gmail': 'Clear Gmail imports',
    'duplicates': 'Remove duplicate records',
    'completed': 'Clear completed imported assignments',
    'imported': 'Reset all imported data',
    'full': 'Full application reset',
}


def backup_database(root, folder=None):
    """Copy the database with SQLite's backup API into a new backup folder."""
    folder = folder or root / 'planner_backups' / uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    from contextlib import closing

    with (
        closing(sqlite3.connect(root / 'deadlines.db')) as source,
        closing(sqlite3.connect(folder / 'deadlines.db')) as target,
    ):
        source.backup(target)
    return folder


def plan(connection, root, action, include_manual):
    """Work out what an action would change and a token that detects later edits."""
    rows = [dict(row) for row in connection.execute('SELECT * FROM tasks')]
    folder = root / 'assessment_pdfs'
    documents = None
    files = []
    groups = []
    undated = []
    modules = []
    history = []
    old_documents = []
    chosen = []
    manual = sum(row.get('source') not in ('Moodle', 'Outlook', 'Gmail') for row in rows)
    if action in ('moodle', 'outlook', 'gmail'):
        chosen = [r for r in rows if r.get('source', '') == action.capitalize()]
    elif action == 'completed':
        completed = {
            r['id']
            for r in rows
            if r.get('source') in ('Moodle', 'Outlook', 'Gmail') and r['progress'] == 100
        }
        for group in task_groups(rows):
            if any(r['id'] in completed for r in group):
                completed.update(r['id'] for r in group)
        chosen = [r for r in rows if r['id'] in completed]
    elif action in ('imported', 'full'):
        chosen = [
            r
            for r in rows
            if r.get('source') in ('Moodle', 'Outlook', 'Gmail')
            or (action == 'full' and include_manual)
        ]
    if action in ('moodle', 'imported', 'full'):
        undated = [row['key'] for row in connection.execute('SELECT key FROM moodle_undated')]
        modules = [row['id'] for row in connection.execute('SELECT id FROM moodle_modules')]
    if action == 'full':
        history = [row['id'] for row in connection.execute('SELECT id FROM sync_history')]
    if action in ('pdfs', 'duplicates', 'imported', 'full'):
        old_documents = read_documents(root)
        if action == 'duplicates':
            documents = []
            for digest, copies in group_documents(root, old_documents, verify=True).items():
                first, *others = copies
                documents.append(combine_documents(first, others, digest))
                files.extend(
                    folder / document_filename(doc) for doc in others if doc['id'] != first['id']
                )
            groups = task_groups(rows)
            chosen = [
                r for group in groups for r in group if r['id'] != min(x['id'] for x in group)
            ]
        else:
            documents = []
            files = (
                [
                    path
                    for path in folder.iterdir()
                    if PDF_ID.fullmatch(path.stem) and path.suffix[1:] in DOCUMENT_TYPES
                ]
                if folder.is_dir()
                else []
            )
    files = sorted({path for path in files if path.is_file()})
    counts = dict(
        tasks=len(chosen),
        undated_assignments=len(undated),
        modules=len(modules),
        documents=len(old_documents) - len(documents or []),
        files=len(files),
        history=len(history),
        manual_tasks=sum(r.get('source') not in ('Moodle', 'Outlook', 'Gmail') for r in chosen),
    )
    signature = json.dumps(
        [
            action,
            include_manual,
            chosen,
            groups,
            undated,
            modules,
            history,
            old_documents,
            documents,
            [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files],
        ],
        sort_keys=True,
    )
    return dict(
        counts=counts,
        token=hashlib.sha256(signature.encode()).hexdigest(),
        chosen=chosen,
        groups=groups,
        files=files,
        docs=documents,
        manual=manual,
    )


def recover_pending(connection, root):
    """Restore file moves when SQLite rolled back an interrupted maintenance operation."""
    for journal in (root / 'planner_backups').glob('*/pending.json'):
        backup = journal.parent
        if not connection.execute(
            'SELECT 1 FROM completed_maintenance WHERE id=?', (backup.name,)
        ).fetchone():
            state = json.loads(journal.read_text(encoding='utf-8'))
            directory = root / 'assessment_pdfs'
            directory.mkdir(exist_ok=True)
            for name in state['files']:
                if (
                    not PDF_ID.fullmatch(Path(name).stem)
                    or Path(name).name != name
                    or Path(name).suffix[1:] not in DOCUMENT_TYPES
                ):
                    raise ValueError('Invalid maintenance path')
                saved = backup / 'assessment_pdfs' / name
                if saved.is_file():
                    saved.replace(directory / name)
            for target, old in (
                (directory / 'index.json', backup / 'assessment_pdfs' / 'index.json'),
                (root / 'assessment_index.json', backup / 'assessment_index.json'),
            ):
                if old.is_file():
                    write_atomic(target, old.read_bytes())
                elif target.name == 'index.json' and not state['index_existed']:
                    target.unlink(missing_ok=True)
        journal.unlink()


def apply(connection, root, action, selection):
    """Back up, then apply a previewed action to the database and document files."""
    backup = backup_database(root)
    folder = root / 'assessment_pdfs'
    index = folder / 'index.json'
    legacy = root / 'assessment_index.json'
    previous = {}
    moved = []
    journal = backup / 'pending.json'
    try:
        for group in selection['groups']:
            row = combined_task(group)
            connection.execute(
                """
                UPDATE tasks SET title=:title, due=:due, category=:category, notes=:notes, link=:link,
                   module_id=:module_id, weighting=:weighting, priority=:priority, progress=:progress,
                   review=:review WHERE id=:id""",
                row,
            )
            from planner_data import assignment_id

            aid = assignment_id(row['link']) if row['source'] == 'Moodle' else None
            identity = (
                f'activity:{aid}'
                if aid
                else email_identity(row)
                if row['source'] != 'Moodle'
                else None
            )
            keys = [identity] if identity else []
            for original in group:
                connection.execute(
                    'UPDATE import_keys SET task_id=? WHERE task_id=?', (row['id'], original['id'])
                )
                if original['external']:
                    keys.append(original['external'].split(':', 1)[1])
            for key in keys:
                connection.execute(
                    'INSERT INTO import_keys VALUES(?,?,?) ON CONFLICT(source,key) DO UPDATE SET task_id=excluded.task_id',
                    (row['source'], key, row['id']),
                )
        for row in selection['chosen']:
            connection.execute('DELETE FROM import_keys WHERE task_id=?', (row['id'],))
            connection.execute('DELETE FROM tasks WHERE id=?', (row['id'],))
        if action in ('moodle', 'imported', 'full'):
            connection.execute('DELETE FROM moodle_undated')
            connection.execute('DELETE FROM moodle_modules')
        if action == 'full':
            connection.execute('DELETE FROM sync_history')
            connection.execute(
                'INSERT INTO planner_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                ('{"automatic":false}',),
            )
        if selection['docs'] is not None:
            folder.mkdir(exist_ok=True)
            backup_files = backup / 'assessment_pdfs'
            backup_files.mkdir()
            for existing, target in (
                (index, backup_files / 'index.json'),
                (legacy, backup / 'assessment_index.json'),
            ):
                if existing.exists():
                    previous[existing] = existing.read_bytes()
                    target.write_bytes(previous[existing])
            write_atomic(
                journal,
                json.dumps(
                    dict(
                        files=[p.name for p in selection['files']], index_existed=index in previous
                    )
                ).encode('utf-8'),
            )
            for path in selection['files']:
                target = backup_files / path.name
                path.replace(target)
                moved.append((path, target))
            write_atomic(index, json.dumps(selection['docs'], ensure_ascii=False).encode('utf-8'))
            if legacy in previous and json.loads(previous[legacy].decode('utf-8-sig')):
                write_atomic(legacy, b'[]')
            update_document_registry(connection, root, selection['docs'])
            connection.execute('INSERT INTO completed_maintenance VALUES(?)', (backup.name,))
        if selection['chosen']:
            refresh_task_projection(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        for path, target in reversed(moved):
            target.replace(path)
        for path, contents in previous.items():
            write_atomic(path, contents)
        if selection['docs'] is not None and index not in previous:
            index.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
        raise
    try:
        journal.unlink(missing_ok=True)
    except OSError:
        # Startup can discard the journal after seeing the committed SQLite marker.
        pass
    return backup.name

"""SQLite boundaries, additive migrations and transactional imports."""

import json
import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Protocol

from planner_data import (
    assignment_id,
    combine_documents,
    document_filename,
    group_documents,
    read_documents,
)
from search_index import configure_search
from sync_runtime import ProcessGate
from task_identity import combined_task, email_identity, task_groups

logger = logging.getLogger('deadline_planner.database')


def initialize_management(db):
    """Create the settings, history, import key and document tables."""
    db.execute(
        'CREATE TABLE IF NOT EXISTS planner_settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)'
    )
    db.execute("""CREATE TABLE IF NOT EXISTS sync_history(
               id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT NOT NULL,
               mode TEXT NOT NULL, outcome TEXT NOT NULL, metrics TEXT NOT NULL, errors TEXT NOT NULL
               )""")
    db.execute("""CREATE TABLE IF NOT EXISTS import_keys(
               source TEXT NOT NULL, key TEXT NOT NULL, task_id INTEGER NOT NULL,
               PRIMARY KEY(source,key)
               )""")
    db.execute('CREATE INDEX IF NOT EXISTS import_keys_task ON import_keys(task_id)')
    db.execute("""CREATE TABLE IF NOT EXISTS assessment_documents(
               id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL
               )""")
    db.execute("""CREATE TABLE IF NOT EXISTS assessment_resources(
               resource_key TEXT PRIMARY KEY, document_id TEXT NOT NULL
               )""")
    db.execute('CREATE INDEX IF NOT EXISTS moodle_undated_link ON moodle_undated(link)')
    db.execute('CREATE TABLE IF NOT EXISTS completed_maintenance(id TEXT PRIMARY KEY)')


def update_document_registry(connection: sqlite3.Connection, root: Path, docs=None) -> None:
    """Reconcile the SQLite search catalog without rewriting unchanged rows."""
    groups = group_documents(root, read_documents(root) if docs is None else docs)
    desired, resources, modules, assignments = {}, set(), set(), set()
    for digest, group in groups.items():
        doc = combine_documents(group[0], group[1:], digest)
        key = doc['id']
        file = root / 'assessment_pdfs' / document_filename(doc)
        if not doc.get('added_at') and file.is_file():
            from datetime import datetime

            doc['added_at'] = datetime.fromtimestamp(file.stat().st_mtime).astimezone().isoformat()
        desired[key] = digest, json.dumps(doc, sort_keys=True)
        resources.update((resource, key) for resource in doc['resource_keys'])
        modules.update((key, module) for module in doc['module_ids'])
        assignments.update((key, assignment) for assignment in doc['assignment_keys'])
    existing = {
        row['id']: (row['sha256'], row['metadata'])
        for row in connection.execute('SELECT id,sha256,metadata FROM assessment_documents')
    }
    # Remove prior IDs first where the same SHA now belongs to another canonical file.
    removed = [
        key for key, value in existing.items() if key not in desired or value[0] != desired[key][0]
    ]
    connection.executemany(
        'DELETE FROM assessment_documents WHERE id=?', ((key,) for key in removed)
    )
    connection.executemany(
        'INSERT INTO assessment_documents VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata',
        ((key, *record) for key, record in desired.items() if existing.get(key) != record),
    )
    # At most one document owns each Moodle resource.
    relations = {
        'assessment_resources': set(dict(sorted(resources)).items()),
        'assessment_document_modules': modules,
        'assessment_document_assignments': assignments,
    }
    for table, wanted in relations.items():
        existing_rows = {tuple(row) for row in connection.execute(f'SELECT * FROM {table}')}
        fields = (
            ('resource_key', 'document_id')
            if table == 'assessment_resources'
            else (
                'document_id',
                'module_id' if table == 'assessment_document_modules' else 'assignment_key',
            )
        )
        connection.executemany(
            f'DELETE FROM {table} WHERE {fields[0]}=? AND {fields[1]}=?', existing_rows - wanted
        )
        connection.executemany(f'INSERT INTO {table} VALUES(?,?)', wanted - existing_rows)


@contextmanager
def open_database(root: Path):
    """Open deadlines.db with row access by name and Unicode-aware search."""
    connection = sqlite3.connect(root / 'deadlines.db', timeout=20)
    connection.row_factory = sqlite3.Row
    # SQLite's built-in lower() only handles ASCII. Course titles can contain accented letters.
    connection.create_function(
        'planner_casefold', 1, lambda value: (value or '').casefold(), deterministic=True
    )
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(root: Path) -> None:
    """Reconcile the durable file index before starting background imports."""
    from data_management import recover_pending

    gate = ProcessGate()
    can_recover = gate.acquire(root)
    try:
        with open_database(root) as connection:
            connection.execute('BEGIN IMMEDIATE')
            migrate_schema(connection, root)
            if can_recover:
                recover_pending(connection, root)
                # The index may have been published immediately before a previous process stopped.
                try:
                    update_document_registry(connection, root)
                except (ValueError, OSError):
                    logger.warning(
                        'Document catalogue refresh deferred until the index is readable'
                    )
    finally:
        if can_recover:
            gate.release()


def migrate_schema(connection: sqlite3.Connection, root: Path) -> None:
    """Apply additive schema changes, backing up an older database first."""
    migration = connection.execute('PRAGMA user_version').fetchone()[0] < 1
    if (
        migration
        and connection.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone()
    ):
        from data_management import backup_database

        snapshot = backup_database(root)
        for relative in ('assessment_index.json', 'assessment_pdfs/index.json'):
            path = root / relative
            if path.is_file():
                backup_index = snapshot / relative
                backup_index.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, backup_index)
    connection.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY, external TEXT UNIQUE, title TEXT NOT NULL,
        due TEXT NOT NULL, source TEXT, category TEXT, notes TEXT, link TEXT,
        priority TEXT DEFAULT 'Medium', progress INTEGER DEFAULT 0, review INTEGER DEFAULT 0
    )""")
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
    if 'module_id' not in columns:
        connection.execute('ALTER TABLE tasks ADD COLUMN module_id TEXT')
    if 'weighting' not in columns:
        connection.execute('ALTER TABLE tasks ADD COLUMN weighting TEXT')
    connection.execute(
        'CREATE TABLE IF NOT EXISTS moodle_modules(id TEXT PRIMARY KEY, name TEXT NOT NULL)'
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS moodle_undated(
        key TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT, notes TEXT,
        link TEXT, module_id TEXT, weighting TEXT
    )""")
    initialize_management(connection)
    if migration:
        initialize_listing(connection)
        refresh_task_projection(connection)
        connection.execute('PRAGMA user_version=1')
    initialize_preferences(connection)
    # Full-text search is optional: builds of SQLite without FTS5 keep substring search.
    configure_search(connection)


def initialize_preferences(connection: sqlite3.Connection) -> None:
    """Store first-run and reminder preferences separately from sync settings."""
    connection.execute(
        'CREATE TABLE IF NOT EXISTS app_preferences(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)'
    )


def persist_import_row(connection, source, external, row, before) -> tuple[int, bool]:
    """Update published fields while keeping the planner user's progress and priority."""
    values = {field: row.get(field, '') for field in ('title', 'due', 'category', 'notes', 'link')}
    values.update(module_id=row.get('module_id'), weighting=row.get('weighting'))
    if before is None:
        fresh = connection.execute(
            """INSERT INTO tasks(external,source,review,title,due,category,notes,link,module_id,weighting)
               VALUES(:external,:source,:review,:title,:due,:category,:notes,:link,:module_id,:weighting)
               RETURNING id""",
            dict(values, external=external, source=source, review=row.get('review', 0)),
        ).fetchone()
        return fresh['id'], True
    # A feed-only refresh must preserve descriptions recovered from authenticated Moodle pages.
    if before['module_id'] is not None:
        if values['module_id'] is None:
            values['category'] = before['category']
        if values['module_id'] is None or values['link'] == '':
            values['notes'], values['link'] = before['notes'], before['link']
    for field in ('module_id', 'weighting'):
        if values[field] is None:
            values[field] = before[field]
    changed = any(before[key] != value for key, value in values.items())
    if changed:
        connection.execute(
            """UPDATE tasks SET title=:title,due=:due,category=:category,notes=:notes,
               link=:link,module_id=:module_id,weighting=:weighting WHERE id=:id""",
            dict(values, id=before['id']),
        )
    return before['id'], changed


def find_import_row(connection, source, row):
    """Find the existing task for an imported row using its stable identities."""
    external = f'{source}:{row["key"]}'
    aid = assignment_id(row.get('link')) if source == 'Moodle' else None
    identity = f'activity:{aid}' if aid else row.get('identity')
    email = source in ('Gmail', 'Outlook') and bool(identity)
    keys = [identity] if email else [identity, row['key']] if identity else [row['key']]
    for key in keys:
        existing = connection.execute(
            'SELECT t.* FROM import_keys k JOIN tasks t ON t.id=k.task_id WHERE k.source=? AND k.key=?',
            (source, key),
        ).fetchone()
        if existing is not None and (not email or existing['due'] == row['due']):
            return existing['external'], keys, existing
    if email:
        # Positional email keys may shift as extraction improves. Recover legacy IDs by message and date.
        prefix = external.rsplit(':', 1)[0] + ':'
        candidates = connection.execute(
            'SELECT * FROM tasks WHERE source=? AND due=? AND substr(external,1,length(?))=? ORDER BY id',
            (source, row['due'], prefix, prefix),
        )
        for existing in candidates:
            if email_identity(dict(existing)) == identity:
                return existing['external'], keys, existing
        external = f'{source}:{identity}'
    existing = connection.execute('SELECT * FROM tasks WHERE external=?', (external,)).fetchone()
    if existing is None and aid:
        existing = connection.execute(
            'SELECT * FROM tasks WHERE source=? AND link=? ORDER BY id LIMIT 1',
            (source, row['link']),
        ).fetchone()
    return existing['external'] if existing is not None else external, keys, existing


def save_imports(root: Path, source: str, rows: list[dict], catalog: dict) -> tuple[int, int]:
    """Insert or update imported rows in one transaction; returns (updated, duplicates)."""
    updated = duplicates = 0
    projection_changed = False
    with open_database(root) as connection:
        connection.execute('BEGIN IMMEDIATE')
        for row in rows:
            external, keys, before = find_import_row(connection, source, row)
            duplicates += int(before is not None and external != f'{source}:{row["key"]}')
            task_id, changed = persist_import_row(connection, source, external, row, before)
            projection_changed |= changed
            updated += int(before is not None and changed)
            connection.executemany(
                """INSERT INTO import_keys VALUES(?,?,?)
                   ON CONFLICT(source,key) DO UPDATE SET task_id=excluded.task_id
                   WHERE import_keys.task_id IS NOT excluded.task_id""",
                ((source, key, task_id) for key in keys),
            )
        connection.executemany(
            """INSERT INTO moodle_modules(id,name) VALUES(:id,:name)
                                  ON CONFLICT(id) DO UPDATE SET name=excluded.name WHERE name IS NOT excluded.name""",
            catalog.get('modules', []),
        )
        current_links = {row.get('link') for row in rows}
        for assignment in catalog.get('assignments', []):
            if assignment['due'] or assignment['link'] in current_links:
                connection.execute('DELETE FROM moodle_undated WHERE key=?', (assignment['key'],))
            elif catalog.get('calendar_complete', True) and not assignment.get('due_unparsed'):
                connection.execute(
                    """
                    INSERT INTO moodle_undated(key,title,category,notes,link,module_id,weighting)
                    VALUES(:key,:title,:category,:notes,:link,:module_id,:weighting)
                    ON CONFLICT(key) DO UPDATE SET title=excluded.title,category=excluded.category,
                        notes=excluded.notes,link=excluded.link,module_id=excluded.module_id,
                        weighting=COALESCE(excluded.weighting,moodle_undated.weighting)
                """,
                    assignment,
                )
        if projection_changed:
            refresh_task_projection(connection)
    return updated, duplicates


def initialize_listing(connection: sqlite3.Connection) -> None:
    """Store only legacy duplicate overrides so normal lists can page in SQLite."""
    connection.execute(
        'CREATE TABLE IF NOT EXISTS task_projection(task_id INTEGER PRIMARY KEY, record TEXT)'
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS assessment_document_modules(
        document_id TEXT NOT NULL, module_id TEXT NOT NULL, PRIMARY KEY(document_id,module_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS assessment_document_assignments(
        document_id TEXT NOT NULL, assignment_key TEXT NOT NULL, PRIMARY KEY(document_id,assignment_key))""")
    for sql in (
        'CREATE INDEX IF NOT EXISTS documents_by_module ON assessment_document_modules(module_id,document_id)',
        'CREATE INDEX IF NOT EXISTS documents_by_assignment ON assessment_document_assignments(assignment_key,document_id)',
        'CREATE INDEX IF NOT EXISTS tasks_due ON tasks(due,id)',
        'CREATE INDEX IF NOT EXISTS tasks_module_due ON tasks(module_id,due,id)',
        'CREATE INDEX IF NOT EXISTS tasks_source_link ON tasks(source,link)',
        'CREATE INDEX IF NOT EXISTS sync_history_started ON sync_history(started DESC)',
    ):
        connection.execute(sql)

    fields = (
        'title',
        'due',
        'category',
        'notes',
        'link',
        'priority',
        'progress',
        'review',
        'module_id',
        'weighting',
    )
    projected = ', '.join(
        f"CASE WHEN p.record IS NULL THEN t.{field} ELSE json_extract(p.record,'$.{field}') END AS {field}"
        for field in fields
    )
    connection.execute(f"""
        CREATE VIEW IF NOT EXISTS planner_tasks AS
        SELECT t.id, NULL AS key, t.source, {projected}
        FROM tasks t LEFT JOIN task_projection p ON p.task_id=t.id
        WHERE (p.task_id IS NULL OR p.record IS NOT NULL)
          AND NOT EXISTS (
            SELECT 1 FROM moodle_undated u
            WHERE t.source='Moodle' AND u.link=
                CASE WHEN p.record IS NULL THEN t.link ELSE json_extract(p.record,'$.link') END
          )
        UNION ALL
        SELECT NULL, key, 'Moodle', title, NULL, category, notes, link,
               NULL, 0, 0, module_id, weighting
        FROM moodle_undated
    """)


def refresh_task_projection(connection: sqlite3.Connection) -> None:
    """Reconcile legacy aliases only when imports or maintenance change their rows."""
    records = (
        dict(row)
        for row in connection.execute(
            "SELECT * FROM tasks WHERE source IN ('Moodle','Outlook','Gmail')"
        )
    )
    expected = {}
    for group in task_groups(records):
        primary = combined_task(group)
        expected.update({row['id']: None for row in group})
        expected[primary['id']] = json.dumps(primary, sort_keys=True)
    current = dict(connection.execute('SELECT task_id,record FROM task_projection'))
    connection.executemany(
        'DELETE FROM task_projection WHERE task_id=?',
        ((key,) for key in current.keys() - expected.keys()),
    )
    connection.executemany(
        """INSERT INTO task_projection VALUES(?,?)
           ON CONFLICT(task_id) DO UPDATE SET record=excluded.record""",
        (
            (key, record)
            for key, record in expected.items()
            if key not in current or current[key] != record
        ),
    )


class TaskInput(Protocol):
    """The task fields that add and update operations need."""

    title: str
    due: date
    category: str
    notes: str
    priority: str
    progress: int


class TaskNotFound(LookupError):
    """The requested task does not exist."""

    pass


def add_task(root: Path, task: TaskInput) -> int:
    """Insert a manually entered (or user-confirmed) deadline and return its ID."""
    with open_database(root) as connection:
        cursor = connection.execute(
            'INSERT INTO tasks(title,due,category,notes,priority,progress,source,weighting)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (
                task.title,
                str(task.due),
                task.category,
                task.notes,
                task.priority,
                task.progress,
                'Manual',
                getattr(task, 'weighting', '') or None,
            ),
        )
        return cursor.lastrowid


def update_task(root: Path, task_id: int, task: TaskInput) -> None:
    """Save edits to a task and to any duplicate rows merged with it."""
    with open_database(root) as connection:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute('SELECT source FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not existing:
            raise TaskNotFound('Deadline not found')
        group = []
        projected = connection.execute(
            'SELECT 1 FROM task_projection WHERE task_id=?', (task_id,)
        ).fetchone()
        if projected:
            rows = [
                dict(row)
                for row in connection.execute(
                    'SELECT * FROM tasks WHERE source=?', (existing['source'],)
                )
            ]
            group = next(
                (
                    items
                    for items in task_groups(rows)
                    if any(row['id'] == task_id for row in items)
                ),
                [],
            )
        if group:
            canonical = combined_task(group)
            connection.executemany(
                'UPDATE tasks SET link=?,module_id=? WHERE id=?',
                ((canonical['link'], canonical['module_id'], row['id']) for row in group),
            )
        connection.executemany(
            'UPDATE tasks SET title=?,due=?,category=?,notes=?,priority=?,progress=?,review=0 WHERE id=?',
            (
                (
                    task.title,
                    str(task.due),
                    task.category,
                    task.notes,
                    task.priority,
                    task.progress,
                    task_id,
                )
                for task_id in ([r['id'] for r in group] if group else [task_id])
            ),
        )
        if group:
            refresh_task_projection(connection)


def delete_manual_task(root: Path, task_id: int) -> None:
    """Delete one manual task after a backup. Imported tasks are refused."""
    from data_management import backup_database

    with open_database(root) as connection:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute('SELECT source FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not existing:
            raise TaskNotFound('Deadline not found')
        if existing['source'] != 'Manual':
            raise ValueError('Use Data management to clear imported deadlines')
        backup_database(root)
        connection.execute('DELETE FROM tasks WHERE id=?', (task_id,))

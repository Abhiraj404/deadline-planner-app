"""Maintain optional FTS5 indexes beside the planner's authoritative SQLite tables."""

import re
import sqlite3
from functools import cache

TASK_FTS = 'planner_task_fts'
DOCUMENT_FTS = 'planner_document_fts'


@cache
def supports_fts():
    """Detect the module without assuming how the user's Python was built."""
    from contextlib import closing

    with closing(sqlite3.connect(':memory:')) as connection:
        try:
            connection.execute('CREATE VIRTUAL TABLE probe USING fts5(content)')
        except sqlite3.OperationalError:
            return False
    return True


def enabled(connection, table):
    """True when FTS5 is available and the index table exists."""
    return supports_fts() and bool(
        connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone()
    )


def match_query(query):
    """Convert user input to safe FTS literals, with explicit phrases and prefix words."""
    parts = []
    for match in re.finditer(r'"([^"]+)"|([^\s"]+)', query):
        words = re.findall(r'[^\W_]+', match[1] or match[2])
        if match[1] and words:
            parts.append('"' + ' '.join(words) + '"')
        else:
            parts.extend(f'"{word}"*' for word in words)
    return ' AND '.join(parts)


def snippet_expression(table):
    """SQL for a short excerpt with matches wrapped in control markers."""
    # The UI escapes this text before turning the two control markers into mark elements.
    return f"snippet({table}, -1, char(1), char(2), ' … ', 22)"


# Search IDs are positive for dated tasks and negative for undated Moodle work.
INDEXED_TASKS = """
    SELECT t.id, {title}, {category}, {notes}
    FROM tasks t LEFT JOIN task_projection p ON p.task_id=t.id
    WHERE (p.task_id IS NULL OR p.record IS NOT NULL)
""".format(
    **{
        field: f"CASE WHEN p.record IS NULL THEN t.{field} ELSE json_extract(p.record,'$.{field}') END"
        for field in ('title', 'category', 'notes')
    }
)
INDEXED_WORDS = 'SELECT -rowid,title,category,notes FROM moodle_undated'
INDEXED_DOCUMENTS = """
    SELECT rowid,json_extract(metadata,'$.title'),
           coalesce(json_extract(metadata,'$.module'),'') || ' ' ||
               coalesce(json_extract(metadata,'$.assignment'),''),
           json_extract(metadata,'$.text')
    FROM assessment_documents
"""
TRIGGER_SOURCES = (
    ('tasks', TASK_FTS, INDEXED_TASKS, 't.id', 'id', '', ' OF title,category,notes'),
    ('task_projection', TASK_FTS, INDEXED_TASKS, 't.id', 'task_id', '', ''),
    ('moodle_undated', TASK_FTS, INDEXED_WORDS, 'rowid', 'rowid', '-', ' OF title,category,notes'),
    ('assessment_documents', DOCUMENT_FTS, INDEXED_DOCUMENTS, 'rowid', 'rowid', '', ' OF metadata'),
)


def configure_search(connection):
    """Install triggers and reconcile once, also allowing a database to move between builds."""
    triggers = {
        f'planner_search_{source}_{event}'
        for source, *_ in TRIGGER_SOURCES
        for event in ('insert', 'update', 'delete')
    }
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'planner_search_*'"
        )
    }
    if not supports_fts():
        # Ordinary writes must still work if a database was created with a different SQLite build.
        for name in existing & triggers:
            connection.execute(f'DROP TRIGGER {name}')
        return
    if existing == triggers and enabled(connection, TASK_FTS) and enabled(connection, DOCUMENT_FTS):
        return

    for table in (TASK_FTS, DOCUMENT_FTS):
        connection.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {table}
            USING fts5(headline, grouping, content, tokenize='unicode61 remove_diacritics 2')
        """)
        connection.execute(f'DELETE FROM {table}')
    for source, table, values, column, key, sign, update_cols in TRIGGER_SOURCES:
        condition = ' AND ' if source in ('tasks', 'task_projection') else ' WHERE '
        for event in ('insert', 'update', 'delete'):
            name = f'planner_search_{source}_{event}'
            statements = []
            anchor = 'old' if event == 'delete' else 'new'
            statements.append(f'DELETE FROM {table} WHERE rowid={sign}{anchor}.{key};')
            # A deleted projection reveals the base task again, so it must be reindexed.
            if event != 'delete' or source == 'task_projection':
                statements.append(
                    f'INSERT INTO {table}(rowid,headline,grouping,content) '
                    f'{values}{condition}{column}={anchor}.{key};'
                )
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {event}'
                f'{update_cols if event == "update" else ""} ON {source} '
                f'BEGIN {" ".join(statements)} END'
            )
    connection.execute(f'INSERT INTO {TASK_FTS}(rowid,headline,grouping,content) {INDEXED_TASKS}')
    connection.execute(f'INSERT INTO {TASK_FTS}(rowid,headline,grouping,content) {INDEXED_WORDS}')
    connection.execute(
        f'INSERT INTO {DOCUMENT_FTS}(rowid,headline,grouping,content) {INDEXED_DOCUMENTS}'
    )

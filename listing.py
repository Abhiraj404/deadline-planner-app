"""Bounded, parameterised queries for planner pages and navigation."""

import json
import stat
from datetime import date, datetime
from os import stat_result
from pathlib import Path

from planner_data import PDF_ID, assignment_id, document_type
from search_index import DOCUMENT_FTS, TASK_FTS, enabled, match_query, snippet_expression


def paginate(
    connection, source: str, parameters: list, ordering: str, page: int, size: int
) -> dict:
    """Return one page of rows and the total match count from the same snapshot."""
    # Freeze the count and rows together in case a sync commits between the two queries.
    if not connection.in_transaction:
        connection.execute('BEGIN')
    # Source and ordering fragments come from this module, never from request values.
    total = connection.execute(f'SELECT count(*) FROM ({source})', parameters).fetchone()[0]
    statement = f'{source} ORDER BY {ordering} LIMIT ? OFFSET ?'
    rows = connection.execute(statement, [*parameters, size, (page - 1) * size])
    return {'items': [dict(row) for row in rows], 'total': total, 'page': page, 'size': size}


def task_conditions(q, module, source, status, start, end, fts=False):
    """Build the SQL conditions and parameters for the task filters."""
    conditions, parameters = [], []
    expression = match_query(q) if q and fts else ''
    if expression:
        # Dated tasks use their own ID as the FTS row ID. Undated Moodle work uses -rowid.
        conditions.append(f"""(id IN (SELECT rowid FROM {TASK_FTS} WHERE {TASK_FTS} MATCH ?)
            OR key IN (SELECT u.key FROM moodle_undated u WHERE -u.rowid IN (
                SELECT rowid FROM {TASK_FTS} WHERE {TASK_FTS} MATCH ? AND rowid < 0)))""")
        parameters.extend((expression, expression))
    elif q:
        conditions.append(
            "instr(planner_casefold(title || ' ' || coalesce(category,'') || ' ' || coalesce(notes,'')), planner_casefold(?)) > 0"
        )
        parameters.append(q)
    if module.startswith('id:'):
        conditions.append('module_id = ?')
        parameters.append(module[3:])
    elif module.startswith('name:'):
        conditions.append("(module_id IS NULL OR module_id='') AND category = ?")
        parameters.append(module[5:])
    if source:
        conditions.append('source = ?')
        parameters.append(source)
    today = date.today().isoformat()
    if status in ('Upcoming', 'Overdue'):
        comparison = '>=' if status == 'Upcoming' else '<'
        conditions.append(f'coalesce(progress,0) < 100 AND due {comparison} ?')
        parameters.append(today)
    elif status == 'Completed':
        conditions.append('progress = 100')
    elif status == 'Needs review':
        conditions.append('review > 0')
    elif status == 'Undated':
        conditions.append('due IS NULL')
    add_dates(conditions, parameters, 'due', start, end)
    return conditions, parameters


def add_dates(conditions, parameters, field, start, end):
    """Add inclusive start and end date conditions for ``field``."""
    if start:
        conditions.append(f'{field} >= ?')
        parameters.append(start.isoformat())
    if end:
        conditions.append(f"NULLIF({field}, '') <= ?")
        parameters.append(end.isoformat())


def where(conditions):
    """Join conditions into a WHERE clause, or return an empty string."""
    return ' WHERE ' + ' AND '.join(conditions) if conditions else ''


TASK_SORTS = {
    'due': 'due IS NULL,due,id,key',
    'latest': 'due IS NULL,due DESC,id,key',
    'title': 'title COLLATE NOCASE,id,key',
}


def task_page(
    connection,
    root,
    page,
    size,
    q='',
    module='',
    source='',
    status='All',
    start=None,
    end=None,
    sort='due',
):
    """One page of deadlines with their linked documents."""
    fts = bool(q) and enabled(connection, TASK_FTS)
    conditions, parameters = task_conditions(q, module, source, status, start, end, fts)
    query = 'SELECT * FROM planner_tasks' + where(conditions)
    result = paginate(connection, query, parameters, TASK_SORTS[sort], page, size)
    attach_documents(connection, root, result['items'])
    return result


def attach_documents(connection, root, tasks):
    """Add each task's related documents with a single query."""
    references = {}
    for row in tasks:
        aid = assignment_id(row['link'])
        keys = [row['key'] or (f'assignment:{aid}' if aid else None)]
        if row['id']:
            # Deadlines confirmed from an uploaded brief link to it as task:<id>.
            keys.append(f'task:{row["id"]}')
        for key in filter(None, keys):
            references.setdefault(key, []).append(row)
    if not references:
        return
    parameters = list(references)
    placeholders = ','.join('?' for _ in parameters)
    rows = connection.execute(
        f"""
        SELECT d.id, json_extract(d.metadata,'$.title') AS title, json_extract(d.metadata,'$.file_type') AS file_type, a.assignment_key
        FROM assessment_document_assignments a JOIN assessment_documents d ON d.id=a.document_id
        WHERE a.assignment_key IN ({placeholders}) ORDER BY title COLLATE NOCASE,d.id
    """,
        parameters,
    )
    folder = pdf_folder(root)
    for record in rows:
        link = dict(
            id=record['id'],
            title=record['title'],
            file_type=document_type(record['file_type']),
            pdf_available=pdf_file(folder, record['id'], record['file_type']) is not None,
        )
        for task in references[record['assignment_key']]:
            task.setdefault('related_documents', []).append(link)


def pdf_folder(root: Path) -> Path | None:
    """The document folder, only if it is inside the data folder."""
    folder = (root / 'assessment_pdfs').resolve()
    return folder if folder.is_relative_to(root.resolve()) else None


def pdf_file(
    folder: Path | None, key: str, file_type: str | None = None
) -> tuple[Path, stat_result] | None:
    """Accept only regular, non-linked files with generated document IDs."""
    if folder is None or not PDF_ID.fullmatch(key):
        return None
    path = folder / f'{key}.{document_type(file_type)}'
    try:
        info = path.stat(follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (path, info) if stat.S_ISREG(info.st_mode) else None


DOC_DATE = "json_extract(d.metadata,'$.document_date')"
DOC_SORTS = {
    'group': 'module COLLATE NOCASE,assignment COLLATE NOCASE,title COLLATE NOCASE,id',
    'title': 'title COLLATE NOCASE,id',
    'recent': "coalesce(nullif(document_date,''),nullif(added_at,''),'') DESC,id",
}


def document_page(
    connection, root, page, size, q='', module='', start=None, end=None, dated='all', sort='group'
):
    """One page of documents with filters, FTS snippets and extraction status."""
    filters, parameters = [], []
    expression = match_query(q) if q and enabled(connection, DOCUMENT_FTS) else ''
    snippet = "'' AS snippet"
    snippet_parameters = []
    if expression:
        # FTS5 matches whole words and word prefixes, and returns a short highlighted excerpt.
        filters.append(
            f'd.rowid IN (SELECT rowid FROM {DOCUMENT_FTS} WHERE {DOCUMENT_FTS} MATCH ?)'
        )
        parameters.append(expression)
        snippet = (
            f'(SELECT {snippet_expression(DOCUMENT_FTS)} FROM {DOCUMENT_FTS} '
            f'WHERE {DOCUMENT_FTS} MATCH ? AND rowid=d.rowid) AS snippet'
        )
        snippet_parameters.append(expression)
    elif q:
        # Search stored text inside SQLite. Only metadata from this page leaves the server.
        filters.append("""instr(planner_casefold(coalesce(json_extract(d.metadata,'$.title'),'') || ' ' ||
                       coalesce(json_extract(d.metadata,'$.module'),'') || ' ' ||
                       coalesce(json_extract(d.metadata,'$.assignment'),'') || ' ' ||
                       coalesce(json_extract(d.metadata,'$.text'),'')), planner_casefold(?)) > 0""")
        parameters.append(q)
    if module.startswith('id:'):
        filters.append(
            'EXISTS (SELECT 1 FROM assessment_document_modules m WHERE m.document_id=d.id AND m.module_id=?)'
        )
        parameters.append(module[3:])
    elif module.startswith('name:'):
        filters.append("""json_extract(d.metadata,'$.module') = ? AND NOT EXISTS (
            SELECT 1 FROM assessment_document_modules m WHERE m.document_id=d.id
        )""")
        parameters.append(module[5:])
    if dated == 'dated':
        filters.append(f"coalesce({DOC_DATE},'') <> ''")
    elif dated == 'undated':
        filters.append(f"coalesce({DOC_DATE},'') = ''")
    add_dates(filters, parameters, DOC_DATE, start, end)
    result = paginate(
        connection,
        """
        SELECT d.id,d.sha256,
            json_extract(d.metadata,'$.title') AS title,
            json_extract(d.metadata,'$.file_type') AS file_type,
            json_extract(d.metadata,'$.module') AS module,
            json_extract(d.metadata,'$.module_id') AS module_id,
            json_extract(d.metadata,'$.assignment') AS assignment,
            json_extract(d.metadata,'$.assignment_keys') AS assignment_keys,
            json_extract(d.metadata,'$.module_ids') AS module_ids,
            json_extract(d.metadata,'$.added_at') AS added_at,
            json_extract(d.metadata,'$.document_date') AS document_date,
            json_extract(d.metadata,'$.source') AS source,
            json_extract(d.metadata,'$.text_incomplete') AS text_incomplete,
            json_extract(d.metadata,'$.extraction_error') AS extraction_error,
            json_extract(d.metadata,'$.ocr_used') AS ocr_used,
            length(coalesce(json_extract(d.metadata,'$.text'),'')) AS text_length,
            {snippet}
        FROM assessment_documents d
    """.replace('{snippet}', snippet)
        + where(filters),
        [*snippet_parameters, *parameters],
        DOC_SORTS[sort],
        page,
        size,
    )
    folder = pdf_folder(root)
    for row in result['items']:
        row['assignment_keys'] = json.loads(row['assignment_keys'] or '[]')
        row['module_ids'] = json.loads(row['module_ids'] or '[]')
        row['file_type'] = document_type(row['file_type'])
        row['source'] = row['source'] or 'Moodle'
        row['extraction_status'] = extraction_status(row)
        for flag in ('text_incomplete', 'extraction_error', 'ocr_used'):
            row[flag] = bool(row[flag])
        file = pdf_file(folder, row['id'], row['file_type'])
        row['pdf_available'] = file is not None
        if file and not row['added_at']:
            row['added_at'] = datetime.fromtimestamp(file[1].st_mtime).astimezone().isoformat()
    return result


def extraction_status(row: dict) -> str:
    """Summarise how much of a document's text the planner could index."""
    if row.get('extraction_error'):
        return 'error'
    if not row.get('text_length'):
        return 'no_text'
    if row.get('text_incomplete') is None:
        # Documents indexed before v1.0 did not record whether extraction was complete.
        return 'not_recorded'
    return 'partial' if row.get('text_incomplete') else 'complete'


def module_page(connection, page, size, q):
    """One page of modules with their task and document counts."""
    return paginate(
        connection,
        """
        SELECT m.id,m.name,
            (SELECT count(*) FROM planner_tasks t WHERE t.module_id=m.id) AS task_count,
            (SELECT count(*) FROM assessment_document_modules d WHERE d.module_id=m.id) AS document_count
        FROM moodle_modules m WHERE instr(planner_casefold(m.name),planner_casefold(?))>0 OR ?=''
    """,
        [q, q],
        'name COLLATE NOCASE,id',
        page,
        size,
    )


def module_options(connection):
    """Distinct module filter choices from every place modules appear."""
    return [
        dict(row)
        for row in connection.execute("""
        WITH options(value,name,priority) AS (
            SELECT 'id:' || id,name,0 FROM moodle_modules
            UNION ALL
            SELECT CASE WHEN module_id IS NULL OR module_id='' THEN 'name:' || category
                        ELSE 'id:' || module_id END,category,1 FROM planner_tasks WHERE category <> ''
            UNION ALL
            SELECT 'id:' || x.module_id,coalesce(m.name,json_extract(d.metadata,'$.module')),2
            FROM assessment_document_modules x JOIN assessment_documents d ON d.id=x.document_id
            LEFT JOIN moodle_modules m ON m.id=x.module_id
            UNION ALL
            SELECT 'name:' || json_extract(d.metadata,'$.module'),json_extract(d.metadata,'$.module'),2
            FROM assessment_documents d WHERE NOT EXISTS (
                SELECT 1 FROM assessment_document_modules x WHERE x.document_id=d.id
            )
        ), ranked AS (
            SELECT value,name,row_number() OVER (PARTITION BY value ORDER BY priority,name) AS rank FROM options
            WHERE value IS NOT NULL AND name <> ''
        )
        SELECT value,name FROM ranked WHERE rank=1 ORDER BY name COLLATE NOCASE,value
    """)
    ]


def history_page(connection, page, size):
    """One page of sync history with decoded metrics and errors."""
    result = paginate(connection, 'SELECT * FROM sync_history', [], 'started DESC,id', page, size)
    result['items'] = [
        dict(row, metrics=json.loads(row['metrics']), errors=json.loads(row['errors']))
        for row in result['items']
    ]
    return result


def overview(connection, root):
    """Dashboard data: next deadlines, recent documents and counts."""
    upcoming = task_page(connection, root, 1, 5, status='Upcoming')
    recent = document_page(connection, root, 1, 5, sort='recent')
    overdue = connection.execute(
        'SELECT count(*) FROM planner_tasks WHERE progress < 100 AND due < ?',
        (date.today().isoformat(),),
    ).fetchone()[0]
    modules = connection.execute('SELECT count(*) FROM moodle_modules').fetchone()[0]
    return {
        'upcoming': upcoming['items'],
        'recent': recent['items'],
        'counts': {
            'upcoming': upcoming['total'],
            'overdue': overdue,
            'modules': modules,
            'documents': recent['total'],
        },
    }


def reminders(
    connection, days: list[int], include_overdue: bool, today: date | None = None
) -> list:
    """Open deadlines that fall inside the furthest reminder window, plus overdue work."""
    today = today or date.today()
    horizon = max(days, default=0)
    start = '0000-01-01' if include_overdue else today.isoformat()
    rows = connection.execute(
        """SELECT id,title,due,category,source,weighting FROM planner_tasks
           WHERE due IS NOT NULL AND coalesce(progress,0) < 100 AND due >= ? AND due <= ?
           ORDER BY due,id LIMIT 50""",
        (start, date.fromordinal(today.toordinal() + horizon).isoformat()),
    )
    result = []
    for row in rows:
        remaining = (date.fromisoformat(row['due']) - today).days
        result.append(dict(row, days_remaining=remaining, overdue=remaining < 0))
    return result

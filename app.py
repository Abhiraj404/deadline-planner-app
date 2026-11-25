"""FastAPI routes for the single-user, local-first Deadline Planner.

Security model: the server binds to 127.0.0.1 and has no user accounts, so it trusts
programs running as the same computer user. The protections here stop *websites*
open in the user's browser from using it: only loopback Host headers are accepted
(defeating DNS rebinding), state-changing requests that carry a foreign Origin are
rejected, uploads need a non-simple Content-Type (which forces a CORS preflight this
server never approves), and files are only served by generated SHA-256 names, never
by user-supplied paths.
"""

import hmac
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from assessment_extraction import extract_assessment
from configuration import (
    APP_DIR,
    ROOT,
    app_version,
    configure_logging,
    default_sync_minutes,
    demo_mode,
    load_environment,
    update_environment,
)
from data_management import ACTIONS
from data_management import apply as apply_management
from data_management import plan as plan_management
from database import (
    TaskNotFound,
    add_task,
    delete_manual_task,
    initialize_database,
    open_database,
    save_imports,
    update_task,
)
from document_import import import_upload, link_document_to_task
from document_processing import DocumentError, word_preview
from listing import (
    document_page,
    history_page,
    module_options,
    module_page,
    overview,
    pdf_file,
    pdf_folder,
    reminders,
    task_page,
)
from moodle_details import enrich
from pdf_processing import MAX_PDF_BYTES
from planner_data import (
    PDF_ID,
    AppPreferences,
    SyncSettings,
    combine_documents,
    document_type,
    group_documents,
    read_documents,
)
from sources import collect
from synchronisation import InvalidSelection, JobConflict, Synchronisation
from task_identity import project_tasks
from word_processing import DOCX_MIME

load_environment(ROOT)
logger = logging.getLogger('deadline_planner.api')


def db():
    """Open a connection to the planner database (use as a context manager)."""
    return open_database(ROOT)


def initialize():
    """Create or migrate the database and reconcile the document index."""
    initialize_database(ROOT)


def save_import(source, rows, catalog):
    """Store one provider's imported rows; called by the sync worker."""
    return save_imports(ROOT, source, rows, catalog)


def settings() -> SyncSettings:
    """Saved sync settings, or defaults when nothing has been saved yet."""
    with db() as connection:
        row = connection.execute('SELECT data FROM planner_settings WHERE id=1').fetchone()
    if row:
        return SyncSettings.model_validate_json(row['data'])
    if demo_mode():
        return SyncSettings(automatic=False, sources=[])
    return SyncSettings(minutes=default_sync_minutes())


def preferences() -> AppPreferences:
    """Saved first-run and reminder preferences, or defaults."""
    with db() as connection:
        row = connection.execute('SELECT data FROM app_preferences WHERE id=1').fetchone()
    return AppPreferences.model_validate_json(row['data']) if row else AppPreferences()


jobs = Synchronisation(
    lambda: ROOT,
    lambda: settings(),
    lambda *a, **k: collect(*a, **k),
    lambda *a: save_import(*a),
    lambda *a, **k: enrich(*a, **k),
)
begin = jobs.begin
lock = jobs.lock


@asynccontextmanager
async def lifespan(app):
    """Start the database and scheduler with the server, and stop them cleanly."""
    handler = configure_logging(ROOT)
    stop = threading.Event()
    scheduler = threading.Thread(
        target=jobs.schedule, args=(stop,), name='planner-scheduler', daemon=True
    )
    try:
        await run_in_threadpool(initialize)
        # Demo mode never contacts Moodle, Outlook or Gmail, so it has no scheduler.
        if not demo_mode():
            scheduler.start()
        yield
    finally:
        stop.set()
        if scheduler.is_alive():
            await run_in_threadpool(scheduler.join, 1)
        jobs.stop()
        if jobs.worker and jobs.worker.is_alive():
            await run_in_threadpool(jobs.worker.join, 2)
        if handler:
            logging.getLogger('deadline_planner').removeHandler(handler)
            handler.close()


app = FastAPI(
    title='Deadline Planner',
    version=app_version(),
    description='Local, single-user API. Keep the server bound to 127.0.0.1.',
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
app.mount('/static', StaticFiles(directory=APP_DIR / 'static', check_dir=False), name='static')


@app.middleware('http')
async def local_request_policy(request, call_next):
    """Prevent websites from posting forms to this unauthenticated local app."""
    origin = request.headers.get('origin')
    if origin and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        try:
            parsed = urlsplit(origin)
            valid_origin = (
                parsed.scheme == request.url.scheme and parsed.netloc == request.url.netloc
            )
        except ValueError:
            valid_origin = False
        if not valid_origin:
            return JSONResponse(
                {'detail': 'This action must come from the local planner'}, status_code=403
            )
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    if request.url.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'private, no-store')
    return response


Page = Annotated[int | None, Query(ge=1, le=100000)]
PageSize = Annotated[int, Query(ge=1, le=50)]
Search = Annotated[str, Query(max_length=200)]
ModuleFilter = Annotated[str, Query(max_length=1005)]
TaskStatus = Literal['All', 'Upcoming', 'Overdue', 'Completed', 'Needs review', 'Undated']


class Task(BaseModel):
    """A deadline entered or confirmed by the user."""

    title: str = Field(min_length=1, max_length=1000)
    due: date
    category: str = Field('', max_length=1000)
    notes: str = Field('', max_length=500000)
    priority: Literal['Low', 'Medium', 'High'] = 'Medium'
    progress: int = Field(0, ge=0, le=100)
    weighting: str = Field('', max_length=20)
    # Set when the user confirms a deadline extracted from a stored document.
    document_id: str | None = Field(None, pattern=r'^[a-f0-9]{64}$')


@app.get('/')
def home():
    """Serve the single-page frontend."""
    return FileResponse(APP_DIR / 'index.html')


@app.get('/api/tasks')
def tasks(
    page: Page = None,
    size: PageSize = 25,
    q: Search = '',
    module: ModuleFilter = '',
    source: Literal['', 'Manual', 'Moodle', 'Outlook', 'Gmail'] = '',
    status: TaskStatus = 'All',
    start: date | None = None,
    end: date | None = None,
    sort: Literal['due', 'latest', 'title'] = 'due',
):
    """List deadlines: paged and filtered with ``page``, or the full legacy list."""
    with db() as connection:
        if page is not None:
            return task_page(
                connection, ROOT, page, size, q, module, source, status, start, end, sort
            )
        return project_tasks(
            [
                dict(row)
                for row in connection.execute("""
            SELECT tasks.* FROM tasks
            WHERE NOT EXISTS (
                SELECT 1 FROM moodle_undated WHERE tasks.source='Moodle' AND moodle_undated.link=tasks.link
            )
            ORDER BY progress=100,due,id
        """)
            ]
        )


@app.get('/api/moodle/modules')
def modules(page: Page = None, size: PageSize = 25, q: Search = ''):
    """List known Moodle modules: paged with ``page``, or all of them."""
    with db() as connection:
        if page is not None:
            return module_page(connection, page, size, q)
        return [
            dict(row)
            for row in connection.execute(
                'SELECT * FROM moodle_modules ORDER BY name COLLATE NOCASE,id'
            )
        ]


@app.get('/api/moodle/undated-assignments')
def undated_assignments():
    """Moodle assignments that have no published deadline."""
    with db() as connection:
        return [
            dict(row)
            for row in connection.execute('SELECT * FROM moodle_undated ORDER BY category,title')
        ]


@app.post('/api/tasks')
def add(task: Task):
    """Create a deadline, linking it to its source document when one is given."""
    if not task.document_id:
        return {'ok': True, 'id': add_task(ROOT, task)}
    # Linking writes the document index, which a running sync may also be writing.
    if not jobs.acquire():
        raise HTTPException(409, 'Wait for synchronisation to finish before confirming')
    try:
        task_id = add_task(ROOT, task)
        try:
            link_document_to_task(ROOT, task.document_id, task_id, task.title)
        except LookupError:
            logger.warning('Confirmed deadline saved without its missing document link')
        return {'ok': True, 'id': task_id}
    finally:
        jobs.release()


@app.put('/api/tasks/{id}')
def update(id: int, task: Task):
    """Edit a deadline. Imported duplicates of the same task are updated together."""
    try:
        update_task(ROOT, id, task)
    except TaskNotFound as error:
        raise HTTPException(404, str(error)) from error
    return {'ok': True}


@app.delete('/api/tasks/{id}')
def delete(id: int, confirmed: bool = False):
    """Delete one manually entered deadline after explicit confirmation."""
    if not confirmed:
        raise HTTPException(422, 'Confirm deletion first')
    try:
        delete_manual_task(ROOT, id)
    except TaskNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {'ok': True}


@app.get('/api/settings')
def get_settings():
    """Return the sync scope: sources, modules, date range and schedule."""
    return settings()


@app.put('/api/settings')
def set_settings(value: SyncSettings):
    """Save the sync scope. The scheduler picks it up on its next check."""
    with db() as connection:
        connection.execute(
            'INSERT INTO planner_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data WHERE data IS NOT excluded.data',
            (value.model_dump_json(),),
        )
    return value


def launch(mode, selection=None):
    """Start a background job, turning lock and selection errors into HTTP errors."""
    if demo_mode():
        raise HTTPException(409, 'Synchronisation is disabled in demo mode')
    try:
        job = jobs.begin(mode, selection=selection)
    except JobConflict as error:
        raise HTTPException(409, str(error)) from error
    except InvalidSelection as error:
        raise HTTPException(422, str(error)) from error
    if not job:
        raise HTTPException(409, 'A synchronisation or data management job is already running')
    return job.public()


@app.post('/api/sync', status_code=202)
def start():
    """Start a full synchronisation."""
    return launch('sync')


@app.post('/api/sync/stop')
def stop_sync():
    """Ask the running job to stop at its next cancellation checkpoint."""
    return {'stopping': jobs.stop()}


@app.post('/api/sync/preview', status_code=202)
def start_preview():
    """Start a preview that lists matching records without downloading files."""
    return launch('preview')


@app.get('/api/sync/preview')
def preview():
    """Return the results of the last completed preview."""
    job = jobs.last_preview
    if not job:
        return {'id': None, 'records': {}, 'documents': [], 'modules': [], 'matches': []}
    return {
        'id': job.id,
        'records': job.rows,
        'matches': job.matches,
        'modules': job.modules,
        'documents': [
            {
                'key': item['key'],
                'title': item['title'],
                'module': item['module']['name'],
                'date': item['date'],
                'assignment': (item['assignment'] or {}).get('title', ''),
            }
            for item in job.candidates.values()
        ],
    }


class DownloadSelection(BaseModel):
    """Documents chosen from a preview, or all of them when ``keys`` is None."""

    preview_id: str
    keys: list[str] | None = None


@app.post('/api/sync/download', status_code=202)
def download(selection: DownloadSelection):
    """Download the selected preview documents."""
    return launch('download', selection=selection)


@app.get('/api/sync/history')
def history(page: Page = None, size: PageSize = 25):
    """List past sync jobs: paged with ``page``, or all of them."""
    with db() as connection:
        if page is not None:
            return history_page(connection, page, size)
        return [
            dict(row, metrics=json.loads(row['metrics']), errors=json.loads(row['errors']))
            for row in connection.execute(
                'SELECT * FROM sync_history ORDER BY started DESC, rowid DESC'
            )
        ]


@app.get('/api/status')
def state():
    """Live job status, polled by the frontend."""
    job = jobs.active_job
    with db() as connection:
        previous = connection.execute(
            'SELECT id FROM sync_history ORDER BY started DESC,rowid DESC LIMIT 1'
        ).fetchone()
    return {
        'running': bool(job),
        'sources': jobs.status.copy(),
        'moodle': job.statistics.copy() if job else {},
        'job': job.public() if job else None,
        'last_job_id': previous['id'] if previous else None,
        'demo': demo_mode(),
    }


def document_index():
    """Read the document index file and normalise each entry for the API."""
    try:
        items = read_documents(ROOT)
    except (OSError, ValueError):
        logger.warning('Assessment document index could not be read')
        raise HTTPException(503, 'Assessment document index could not be read') from None
    result = []
    folder = pdf_folder(ROOT)
    for doc in items:
        key = doc['id']
        keys = doc.get('assignment_keys')
        keys = [item for item in keys if isinstance(item, str)] if isinstance(keys, list) else []
        if not keys and isinstance(doc.get('assignment_key'), str) and doc['assignment_key']:
            keys = [doc['assignment_key']]
        file_type = document_type(doc.get('file_type'))
        file = pdf_file(folder, key, file_type)
        available = file is not None
        result.append(
            {
                **{
                    field: str(doc.get(field) or '')
                    for field in (
                        'id',
                        'title',
                        'module',
                        'module_id',
                        'assignment',
                        'assignment_key',
                        'text',
                    )
                },
                'assignment_keys': keys,
                'module_ids': doc.get('module_ids')
                or ([doc['module_id']] if doc.get('module_id') else []),
                'document_date': doc.get('document_date') or '',
                'sha256': doc.get('sha256') or '',
                'added_at': doc.get('added_at')
                or (
                    datetime.fromtimestamp(file[1].st_mtime).astimezone().isoformat()
                    if available
                    else ''
                ),
                'pdf_available': available,
                'file_type': file_type,
            }
        )
    return result


@app.get('/api/assessment-documents')
def assessment_documents(
    page: Page = None,
    size: PageSize = 25,
    q: Search = '',
    module: ModuleFilter = '',
    start: date | None = None,
    end: date | None = None,
    dated: Literal['all', 'dated', 'undated'] = 'all',
    sort: Literal['group', 'recent', 'title'] = 'group',
):
    """List documents: paged with search and filters, or the full index."""
    if page is not None:
        with db() as connection:
            return document_page(connection, ROOT, page, size, q, module, start, end, dated, sort)
    return [
        combine_documents(group[0], group[1:], digest)
        for digest, group in group_documents(ROOT, document_index()).items()
    ]


def serve_assessment(key: str, file_type: str):
    """Send a stored file, but only if its hash name is in the index."""
    file = pdf_file(pdf_folder(ROOT), key, file_type)
    if file is None:
        raise HTTPException(404, 'Assessment document not found')
    try:
        indexed = next(
            (
                doc
                for doc in read_documents(ROOT)
                if doc['id'] == key and document_type(doc.get('file_type')) == file_type
            ),
            None,
        )
    except (ValueError, OSError):
        logger.warning('Document request could not verify the document index')
        raise HTTPException(503, 'Assessment document index could not be read') from None
    if not indexed:
        raise HTTPException(404, 'Assessment document not found')
    filename = None
    if file_type == 'docx':
        stem = re.sub(r'[^\w .-]', '_', str(indexed.get('title') or 'assessment'))
        stem = re.sub(r'\.docx$', '', stem, flags=re.I)[:100].strip('. ') or 'assessment'
        filename = f'{stem}.docx'
    return FileResponse(
        file[0],
        media_type=DOCX_MIME if file_type == 'docx' else 'application/pdf',
        filename=filename,
        headers={'Cross-Origin-Resource-Policy': 'same-origin'},
    )


@app.get('/api/assessment-documents/{key}/pdf')
def assessment_pdf(key: str):
    """Download or open a stored PDF."""
    return serve_assessment(key, 'pdf')


@app.get('/api/assessment-documents/{key}/docx')
def assessment_word(key: str):
    """Download a stored Word file."""
    return serve_assessment(key, 'docx')


def document_metadata(key: str) -> dict:
    """Return one document's catalogue entry, or 404."""
    if not PDF_ID.fullmatch(key):
        raise HTTPException(404, 'Assessment document not found')
    with db() as connection:
        row = connection.execute(
            'SELECT metadata FROM assessment_documents WHERE id=?', (key,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, 'Assessment document not found')
    return json.loads(row['metadata'])


@app.get('/api/assessment-documents/{key}/text')
def assessment_text(key: str):
    """Return one document's extracted text on demand."""
    return {'text': document_metadata(key).get('text') or ''}


@app.get('/api/assessment-documents/{key}/preview')
def assessment_preview(key: str):
    """Structured Word preview, or the stored PDF text with extraction indicators."""
    metadata = document_metadata(key)
    file_type = document_type(metadata.get('file_type'))
    flags = {
        flag: metadata.get(flag) for flag in ('text_incomplete', 'extraction_error', 'ocr_used')
    }
    if file_type == 'docx':
        file = pdf_file(pdf_folder(ROOT), key, 'docx')
        if file is None:
            raise HTTPException(404, 'Assessment document not found')
        try:
            result = word_preview(file[0].read_bytes(), lambda: None)
        except (*DocumentError, OSError):
            raise HTTPException(422, 'The Word document could not be previewed') from None
        return {'file_type': 'docx', **flags, **result}
    text = metadata.get('text') or ''
    blocks = [{'type': 'paragraph', 'text': line} for line in text.split('\n') if line.strip()]
    return {'file_type': 'pdf', **flags, 'blocks': blocks}


@app.get('/api/assessment-documents/{key}/extraction')
def assessment_extraction(key: str):
    """Suggested assessment details from stored text. Nothing is saved until confirmed."""
    return {'document_id': key, **extract_assessment(assessment_text(key)['text'])}


UPLOAD_TYPES = {'application/pdf', 'application/octet-stream', DOCX_MIME}


@app.post('/api/assessment-documents/upload', status_code=201)
async def upload_document(
    request: Request,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    module: Annotated[str, Query(max_length=200)] = '',
):
    """Import one PDF or DOCX sent as the raw request body.

    A raw body avoids a multipart parser dependency. The Content-Type must be a
    document type, which a browser never sends cross-site without a CORS preflight.
    """
    content_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if content_type not in UPLOAD_TYPES:
        raise HTTPException(415, 'Upload a PDF or DOCX file')
    if not filename.lower().endswith(('.pdf', '.docx')):
        raise HTTPException(415, 'Only .pdf and .docx files are supported')
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > MAX_PDF_BYTES:
        raise HTTPException(413, 'Documents are limited to 30 MB')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(413, 'Documents are limited to 30 MB')
    if not data:
        raise HTTPException(422, 'The uploaded file is empty')
    if not jobs.acquire():
        raise HTTPException(409, 'Wait for synchronisation to finish before uploading')
    try:
        document, created = await run_in_threadpool(
            import_upload, ROOT, bytes(data), filename, module
        )
    except DocumentError as error:
        raise HTTPException(422, str(error)) from None
    finally:
        jobs.release()
    return {
        'created': created,
        'document': {
            field: document.get(field)
            for field in ('id', 'title', 'module', 'file_type', 'text_incomplete', 'ocr_used')
        },
        'extraction': extract_assessment(document.get('text') or ''),
    }


@app.get('/api/reminders')
def reminder_list():
    """Open deadlines inside the reminder window from saved preferences."""
    prefs = preferences()
    if not prefs.reminders_enabled:
        return {'enabled': False, 'browser_notifications': False, 'items': []}
    with db() as connection:
        items = reminders(connection, prefs.reminder_days, prefs.include_overdue)
    return {
        'enabled': True,
        'browser_notifications': prefs.browser_notifications,
        'items': items,
    }


@app.get('/api/preferences')
def get_preferences():
    """Return reminder and first-run preferences."""
    return preferences()


@app.put('/api/preferences')
def set_preferences(value: AppPreferences):
    """Save reminder and first-run preferences."""
    with db() as connection:
        connection.execute(
            'INSERT INTO app_preferences VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
            (value.model_dump_json(),),
        )
    return value


def integration_status() -> dict:
    """Report what is configured without returning secrets such as the calendar URL."""
    return {
        'moodle': {
            'url': os.getenv('MOODLE_URL', ''),
            'calendar_configured': bool(os.getenv('MOODLE_ICS_URL')),
            'session_saved': (ROOT / 'moodle-session.json').is_file(),
        },
        'outlook': {
            'client_id': os.getenv('OUTLOOK_CLIENT_ID', ''),
            'tenant': os.getenv('OUTLOOK_TENANT', 'common'),
            'token_saved': (ROOT / 'outlook-cache.json').is_file(),
        },
        'gmail': {
            'credentials_present': (ROOT / 'credentials.json').is_file(),
            'token_saved': (ROOT / 'gmail-token.json').is_file(),
        },
        'timezone': os.getenv('MOODLE_TIMEZONE', ''),
    }


@app.get('/api/app')
def application_info():
    """Version, demo flag and whether first-run setup is complete."""
    return {
        'version': app_version(),
        'demo': demo_mode(),
        'setup_complete': preferences().setup_complete,
    }


@app.get('/api/setup')
def setup_status():
    """Application info plus integration status for the Setup page."""
    return {**application_info(), 'integrations': integration_status()}


class SetupRequest(BaseModel):
    """Integration settings saved to .env. ``None`` keeps a value and '' clears it."""

    moodle_url: str | None = Field(None, max_length=500)
    moodle_ics_url: str | None = Field(None, max_length=2000)
    outlook_client_id: str | None = Field(None, max_length=100)
    outlook_tenant: str | None = Field(None, max_length=100)
    timezone: str | None = Field(None, max_length=64)


def checked_url(value: str, label: str) -> str:
    """Accept only a single-line https URL without credentials; '' clears it."""
    value = value.strip()
    if not value:
        return ''
    if re.search(r'[\s\x00-\x1f\x7f]', value):
        raise HTTPException(422, f'{label} must be a single address without spaces')
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme != 'https' or not parsed.hostname or parsed.username:
        raise HTTPException(422, f'{label} must be an https:// address without a user name')
    return value


@app.put('/api/setup')
def save_setup(value: SetupRequest):
    """Validate integration settings and write them to .env."""
    if demo_mode():
        raise HTTPException(409, 'Integrations cannot be changed in demo mode')
    updates = {}
    if value.moodle_url is not None:
        updates['MOODLE_URL'] = checked_url(value.moodle_url, 'Moodle URL')
    if value.moodle_ics_url is not None:
        updates['MOODLE_ICS_URL'] = checked_url(value.moodle_ics_url, 'Calendar URL')
    if value.outlook_client_id is not None:
        client = value.outlook_client_id.strip()
        if client and not re.fullmatch(r'[0-9a-fA-F-]{36}', client):
            raise HTTPException(422, 'The Outlook client ID must be an application (GUID) ID')
        updates['OUTLOOK_CLIENT_ID'] = client
    if value.outlook_tenant is not None:
        tenant = value.outlook_tenant.strip() or 'common'
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,100}', tenant):
            raise HTTPException(422, 'Enter a tenant ID, a domain, or common')
        updates['OUTLOOK_TENANT'] = tenant
    if value.timezone is not None:
        zone = value.timezone.strip()
        if zone:
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                raise HTTPException(422, 'Enter an IANA timezone such as Europe/Dublin') from None
        updates['MOODLE_TIMEZONE'] = zone
    if updates:
        update_environment(ROOT, updates)
    return integration_status()


@app.get('/api/modules/options')
def module_choices():
    """Module filter options gathered from tasks, modules and documents."""
    with db() as connection:
        return module_options(connection)


@app.get('/api/overview')
def overview_page():
    """Counts, next deadlines and recent documents for the dashboard."""
    with db() as connection:
        return overview(connection, ROOT)


@app.get('/setup')
@app.get('/settings')
@app.get('/assignments')
@app.get('/modules')
@app.get('/library')
@app.get('/calendar')
@app.get('/history')
def navigation():
    """Serve the frontend for each page route so links and reloads work."""
    return home()


class ManagementRequest(BaseModel):
    """A data management action, with the confirmation token from its preview."""

    action: str
    include_manual: bool = False
    confirmation: str | None = None


def checked_action(value):
    """Reject unknown actions and manual-task deletion outside a full reset."""
    if value.action not in ACTIONS or (value.include_manual and value.action != 'full'):
        raise HTTPException(422, 'Invalid data management action')


@app.post('/api/data/preview')
def preview_management(value: ManagementRequest):
    """Count what an action would change and return a confirmation token."""
    checked_action(value)
    try:
        with db() as connection:
            result = plan_management(connection, ROOT, value.action, value.include_manual)
    except (ValueError, OSError):
        raise HTTPException(503, 'The current data could not be inspected safely') from None
    return {
        'counts': result['counts'],
        'confirmation': result['token'],
        'manual_tasks_present': result['manual'],
    }


@app.post('/api/data/apply')
def manage_data(value: ManagementRequest):
    """Apply a previewed action, after a backup, if the data has not changed."""
    checked_action(value)
    if not value.confirmation:
        raise HTTPException(422, 'Preview and confirm the action first')
    if not jobs.acquire():
        raise HTTPException(409, 'Stop synchronisation before changing imported data')
    try:
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            result = plan_management(connection, ROOT, value.action, value.include_manual)
            if result['token'] != value.confirmation:
                raise HTTPException(409, 'Data changed. Review a fresh preview before continuing')
            backup = apply_management(connection, ROOT, value.action, result)
        jobs.last_preview = None
        return {'counts': result['counts'], 'backup': backup}
    except (ValueError, OSError, sqlite3.Error):
        logger.warning('Data management operation could not be completed')
        raise HTTPException(503, 'Could not complete the action safely') from None
    finally:
        jobs.release()


@app.post('/api/server/shutdown', include_in_schema=False)
def shutdown(request: Request):
    """Graceful stop for stop.ps1. Requires the random token that run_server.py wrote
    to the private data folder, so a web page cannot stop the server."""
    expected = getattr(app.state, 'shutdown_token', None)
    supplied = request.headers.get('x-planner-shutdown-token', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(404, 'Not Found')
    app.state.request_shutdown()
    return {'stopping': True}


@app.get('/planner.js')
def frontend_script():
    """Serve the frontend entry script."""
    return FileResponse(APP_DIR / 'planner.js', media_type='text/javascript')

"""Uploads, previews, extraction, reminders, setup, search and demo-mode guards."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest

import app
import configuration
from search_index import supports_fts
from synthetic_documents import build_docx, build_pdf
from word_processing import DOCX_MIME

BRIEF = [
    'Module: DEMO1001 Introduction to Testing',
    'Assessment Title: CA1 Unit Test Report',
    'Weighting: 30%',
    'Submission deadline: Friday 11 December 2026 at 17:00',
    'Word count: 1,500 words',
]


@pytest.fixture
def planner(tmp_path, monkeypatch):
    """An initialised planner with integration settings cleared."""
    for key in configuration.EDITABLE_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv('PLANNER_DEMO', raising=False)
    with patch.object(app, 'ROOT', tmp_path):
        app.initialize()
        yield tmp_path


async def call(method, path, **kwargs):
    """Send one request through the real ASGI app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url='http://127.0.0.1'
    ) as client:
        return await client.request(method, path, **kwargs)


def request(method, path, **kwargs):
    """Synchronous wrapper around ``call``."""
    return asyncio.run(call(method, path, **kwargs))


def upload(data, filename, content_type='application/pdf', **params):
    """Upload raw bytes as a document."""
    return request(
        'POST',
        '/api/assessment-documents/upload',
        params={'filename': filename, **params},
        content=data,
        headers={'Content-Type': content_type},
    )


def test_pdf_upload_is_indexed_extracted_and_deduplicated(planner):
    data = build_pdf(BRIEF)
    first = upload(data, r'C:\private\path\brief.pdf', module='Testing')
    assert first.status_code == 201, first.text
    body = first.json()
    assert body['created'] is True
    assert body['document']['title'] == 'brief.pdf'
    extraction = body['extraction']
    assert extraction['submission_deadline']['value'] == '2026-12-11'
    assert extraction['submission_time']['value'] == '17:00'
    assert extraction['weighting']['value'] == '30%'

    again = upload(data, 'copy.pdf')
    assert again.json()['created'] is False
    assert len(list((planner / 'assessment_pdfs').glob('*.pdf'))) == 1

    listing = request('GET', '/api/assessment-documents', params={'page': 1, 'q': 'unit test'})
    (row,) = listing.json()['items']
    assert row['source'] == 'Upload'
    assert row['extraction_status'] == 'complete'
    assert request('GET', f'/api/assessment-documents/{row["id"]}/pdf').status_code == 200
    suggested = request('GET', f'/api/assessment-documents/{row["id"]}/extraction').json()
    assert suggested['title']['value'] == 'CA1 Unit Test Report'


def test_confirmed_deadline_links_back_to_its_document(planner):
    document_id = upload(build_pdf(BRIEF), 'brief.pdf').json()['document']['id']
    task = {'title': 'CA1 Unit Test Report', 'due': '2026-12-11', 'document_id': document_id}
    created = request('POST', '/api/tasks', json=task)
    assert created.status_code == 200
    (row,) = request('GET', '/api/tasks', params={'page': 1}).json()['items']
    assert row['id'] == created.json()['id']
    assert [doc['id'] for doc in row['related_documents']] == [document_id]


def test_word_upload_has_a_structured_preview(planner):
    data = build_docx(
        [
            {'type': 'heading', 'level': 1, 'text': 'CA2 Design Portfolio'},
            {'type': 'list_item', 'text': 'Wireframes'},
            {'type': 'table', 'rows': [['Criterion', 'Marks'], ['Layout', '40%']]},
        ]
    )
    response = upload(data, 'portfolio.docx', DOCX_MIME)
    assert response.status_code == 201, response.text
    preview = request(
        'GET', f'/api/assessment-documents/{response.json()["document"]["id"]}/preview'
    ).json()
    assert [block['type'] for block in preview['blocks']] == ['heading', 'list_item', 'table']
    assert preview['blocks'][2]['rows'][1] == ['Layout', '40%']


@pytest.mark.parametrize(
    ('data', 'filename', 'content_type', 'status'),
    [
        (b'%PDF-1.7 not really', 'brief.pdf', 'application/pdf', 422),
        (b'hello', 'notes.txt', 'application/octet-stream', 415),
        (b'%PDF-1.7', 'brief.pdf', 'text/plain', 415),
        (b'', 'brief.pdf', 'application/pdf', 422),
        (b'MZ executable', 'brief.pdf', 'application/pdf', 422),
    ],
)
def test_unsafe_or_invalid_uploads_are_rejected(planner, data, filename, content_type, status):
    assert upload(data, filename, content_type).status_code == status
    assert not list((planner / 'assessment_pdfs').glob('*')) or not any(
        path.suffix in ('.pdf', '.docx') for path in (planner / 'assessment_pdfs').iterdir()
    )


def test_cross_site_upload_is_rejected(planner):
    response = request(
        'POST',
        '/api/assessment-documents/upload?filename=a.pdf',
        content=build_pdf(BRIEF),
        headers={'Content-Type': 'application/pdf', 'Origin': 'https://example.com'},
    )
    assert response.status_code == 403


def test_reminders_follow_preferences(planner):
    today = date.today()
    for offset, title in ((-2, 'Late'), (1, 'Tomorrow'), (5, 'Soon'), (30, 'Later')):
        app.add(app.Task(title=title, due=today + timedelta(days=offset)))
    titles = [item['title'] for item in request('GET', '/api/reminders').json()['items']]
    assert titles == ['Late', 'Tomorrow', 'Soon']
    prefs = {'reminder_days': [1], 'include_overdue': False}
    assert request('PUT', '/api/preferences', json=prefs).status_code == 200
    reminders = request('GET', '/api/reminders').json()['items']
    assert [(item['title'], item['days_remaining']) for item in reminders] == [('Tomorrow', 1)]
    assert request('PUT', '/api/preferences', json={'reminder_days': [90]}).status_code == 422


def test_setup_writes_only_whitelisted_settings_and_hides_calendar_url(planner):
    (planner / '.env').write_text('# my comment\nCUSTOM=keep\nMOODLE_URL=\n', encoding='utf-8')
    secret = 'https://moodle.example.test/calendar/export_execute.php?authtoken=abc'
    payload = {
        'moodle_url': 'https://moodle.example.test/my/',
        'moodle_ics_url': secret,
        'timezone': 'Europe/Dublin',
    }
    with patch.dict('os.environ', {}, clear=False):
        response = request('PUT', '/api/setup', json=payload)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        status = request('GET', '/api/setup').json()
        assert status['integrations']['moodle']['calendar_configured'] is True
        assert secret not in str(status)
        content = (planner / '.env').read_text(encoding='utf-8')
        assert '# my comment' in content and 'CUSTOM=keep' in content
        assert content.count('MOODLE_URL=') == 1
        assert request('PUT', '/api/setup', json={'moodle_url': 'http://x.test'}).status_code == 422
        assert request('PUT', '/api/setup', json={'timezone': 'Mars/Base'}).status_code == 422
        bad = {'moodle_url': 'https://x.test/\nMOODLE_ICS_URL=https://evil.test'}
        assert request('PUT', '/api/setup', json=bad).status_code == 422
        assert 'evil' not in (planner / '.env').read_text(encoding='utf-8')


def test_demo_mode_disables_sync_and_integration_changes(planner, monkeypatch):
    monkeypatch.setenv('PLANNER_DEMO', '1')
    assert request('POST', '/api/sync').status_code == 409
    assert request('POST', '/api/sync/preview').status_code == 409
    assert request('PUT', '/api/setup', json={'moodle_url': ''}).status_code == 409
    assert request('GET', '/api/app').json()['demo'] is True
    assert app.settings().sources == []


@pytest.mark.skipif(not supports_fts(), reason='SQLite was built without FTS5')
def test_full_text_index_follows_task_and_document_changes(planner):
    app.add(app.Task(title='Statistics coursework', due='2026-12-01', notes='Regression models'))
    found = request('GET', '/api/tasks', params={'page': 1, 'q': 'regress'}).json()
    assert [row['title'] for row in found['items']] == ['Statistics coursework']
    task_id = found['items'][0]['id']
    edited = {'title': 'Statistics essay', 'due': '2026-12-01', 'notes': 'Bayesian methods'}
    request('PUT', f'/api/tasks/{task_id}', json=edited)
    assert request('GET', '/api/tasks', params={'page': 1, 'q': 'regress'}).json()['total'] == 0
    assert request('GET', '/api/tasks', params={'page': 1, 'q': 'bayes'}).json()['total'] == 1
    upload(build_pdf(BRIEF), 'brief.pdf')
    result = request('GET', '/api/assessment-documents', params={'page': 1, 'q': 'Introduction'})
    (row,) = result.json()['items']
    assert '\x01Introduction\x02' in row['snippet']
    with closing(sqlite3.connect(planner / 'deadlines.db')) as connection:
        tables = {name for (name,) in connection.execute('SELECT name FROM sqlite_master')}
    assert {'planner_task_fts', 'planner_document_fts'} <= tables


def test_punctuation_only_search_falls_back_safely(planner):
    app.add(app.Task(title='C++ lab', due='2026-12-01'))
    response = request('GET', '/api/tasks', params={'page': 1, 'q': '++'})
    assert response.status_code == 200
    assert response.json()['total'] == 1


def test_demo_builder_is_isolated_and_synthetic(tmp_path):
    from demo_data import DemoFolderError, build_demo

    personal = tmp_path / 'personal'
    personal.mkdir()
    (personal / 'deadlines.db').write_bytes(b'private')
    with pytest.raises(DemoFolderError):
        build_demo(personal)
    assert (personal / 'deadlines.db').read_bytes() == b'private'

    demo = tmp_path / 'demo'
    assert build_demo(demo) is True
    assert build_demo(demo) is False
    with patch.object(app, 'ROOT', demo):
        overview = app.overview_page()
        assert overview['counts']['documents'] == 6
        assert overview['counts']['modules'] == 4
        docs = app.assessment_documents(page=1, size=50)['items']
        assert {doc['file_type'] for doc in docs} == {'pdf', 'docx'}
        for doc in docs:
            extraction = app.assessment_extraction(doc['id'])
            assert extraction['submission_deadline']['value'] == doc['document_date']
        completed = app.tasks(page=1, status='Completed')['total']
        assert completed >= 3
        assert app.reminder_list()['items']

"""Exercise the real ASGI routes and a temporary local SQLite database."""

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import httpx
import pytest
from test_moodle import sample_pdf

import app
from database import update_document_registry


@pytest.fixture
def planner(tmp_path):
    """An initialised planner in a temporary data folder."""
    with patch.object(app, 'ROOT', tmp_path):
        app.initialize()
        yield tmp_path


async def request(method, path, **kwargs):
    """Send one request through the real ASGI app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url='http://127.0.0.1'
    ) as client:
        return await client.request(method, path, **kwargs)


def test_assignment_pagination_and_input_validation(planner):
    for number in range(28):
        app.add(app.Task(title=f'Task {number:02d}', due='2026-12-01', category='Study'))
    response = asyncio.run(request('GET', '/api/tasks?page=2&size=25&module=name:Study&sort=title'))
    assert response.status_code == 200
    result = response.json()
    assert result['total'] == 28
    assert [row['title'] for row in result['items']] == ['Task 25', 'Task 26', 'Task 27']
    assert asyncio.run(request('GET', '/api/tasks?page=1&size=500')).status_code == 422
    invalid = {'title': '', 'due': '2026-12-01', 'progress': 150}
    assert asyncio.run(request('POST', '/api/tasks', json=invalid)).status_code == 422
    (planner / 'index.html').write_text('<h1>Planner</h1>', encoding='utf-8')
    assert asyncio.run(request('GET', '/assignments')).status_code == 200


def test_pdf_index_supports_search_without_shipping_extracted_text(planner):
    folder = planner / 'assessment_pdfs'
    folder.mkdir()
    key = 'b' * 64
    (folder / f'{key}.pdf').write_bytes(sample_pdf())
    (folder / 'index.json').write_text(
        json.dumps(
            [
                {
                    'id': key,
                    'title': 'Brief',
                    'module': 'Computing',
                    'module_id': '8',
                    'text': 'A searchable report about vectors.',
                    'assignment_key': 'assignment:3',
                },
            ]
        ),
        encoding='utf-8',
    )
    with app.db() as connection:
        update_document_registry(connection, planner)
    response = asyncio.run(request('GET', '/api/assessment-documents?page=1&q=vectors&module=id:8'))
    (document,) = response.json()['items']
    assert document['id'] == key
    assert 'text' not in document
    assert asyncio.run(request('GET', f'/api/assessment-documents/{key}/text')).json() == {
        'text': 'A searchable report about vectors.'
    }
    assert asyncio.run(request('GET', f'/api/assessment-documents/{key}/pdf')).content.startswith(
        b'%PDF-'
    )
    unindexed = 'a' * 64
    (folder / f'{unindexed}.pdf').write_bytes(sample_pdf())
    assert (
        asyncio.run(request('GET', f'/api/assessment-documents/{unindexed}/pdf')).status_code == 404
    )


def test_rejects_cross_site_actions_and_invalid_host(planner):
    response = asyncio.run(
        request('POST', '/api/sync', headers={'Origin': 'https://other.example'})
    )
    assert response.status_code == 403
    response = asyncio.run(request('GET', '/api/tasks', headers={'Host': 'other.example'}))
    assert response.status_code == 400


def test_additive_migration_backs_up_pre_existing_database(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'deadlines.db')) as original, original:
        original.execute("""CREATE TABLE tasks(id INTEGER PRIMARY KEY, external TEXT UNIQUE,
            title TEXT NOT NULL, due TEXT NOT NULL, source TEXT, category TEXT, notes TEXT,
            link TEXT, priority TEXT, progress INTEGER, review INTEGER)""")
        original.execute(
            "INSERT INTO tasks VALUES(1,NULL,'Keep','2026-12-01','Manual','','','','High',60,0)"
        )
    with patch.object(app, 'ROOT', tmp_path):
        app.initialize()
        app.initialize()
        (item,) = app.tasks(page=1)['items']
        assert (item['title'], item['priority'], item['progress']) == ('Keep', 'High', 60)
    (backup,) = list((tmp_path / 'planner_backups').glob('*/deadlines.db'))
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute('PRAGMA user_version').fetchone()[0] == 0
        assert saved.execute('SELECT title FROM tasks').fetchone()[0] == 'Keep'


def test_paginated_legacy_duplicates_reflect_task_edits(planner):
    first = {
        'key': 'calendar',
        'title': 'Écho is due',
        'due': '2026-12-01',
        'category': 'Course',
        'module_id': '9',
    }
    second = {
        'key': 'assignment:3',
        'title': 'Écho',
        'due': '2026-12-02',
        'category': 'Course',
        'module_id': '9',
        'link': 'https://moodle.test/mod/assign/view.php?id=3',
    }
    app.save_import('Moodle', [first, second], {})
    (task,) = app.tasks(page=1, q='écho')['items']
    updated = asyncio.run(
        request(
            'PUT',
            f'/api/tasks/{task["id"]}',
            json={
                'title': 'Revised',
                'due': '2026-12-05',
                'progress': 100,
                'priority': 'High',
            },
        )
    )
    assert updated.status_code == 200
    (task,) = app.tasks(page=1, status='Completed')['items']
    assert (task['title'], task['due'], task['progress']) == ('Revised', '2026-12-05', 100)


def test_email_deadline_keeps_identity_when_extraction_order_changes(planner):
    original = {'key': 'msg:0', 'title': 'First', 'due': '2026-12-01'}
    new = {
        'key': 'msg:0',
        'title': 'Second',
        'due': '2026-12-02',
        'identity': 'message:msg:2026-12-02',
    }
    app.save_import('Gmail', [original], {})
    original_id = app.tasks()[0]['id']
    app.save_import(
        'Gmail', [new, {**original, 'key': 'msg:1', 'identity': 'message:msg:2026-12-01'}], {}
    )
    rows = {item['due']: item['id'] for item in app.tasks(page=1)['items']}
    assert rows.keys() == {'2026-12-01', '2026-12-02'}
    assert rows['2026-12-01'] == original_id


def test_restart_reconciles_newly_published_pdf_index(planner):
    folder = planner / 'assessment_pdfs'
    folder.mkdir()
    before = {'id': 'a' * 64, 'title': 'First', 'text': 'First text'}
    after = {'id': 'b' * 64, 'title': 'Second', 'text': 'Second text'}
    path = folder / 'index.json'
    path.write_text(json.dumps([before]), encoding='utf-8')
    app.initialize()
    path.write_text(json.dumps([before, after]), encoding='utf-8')
    app.initialize()
    assert app.assessment_documents(page=1)['total'] == 2


def test_automatic_gate_honours_settings_after_reset(planner):
    stale = app.SyncSettings(automatic=True)
    app.set_settings(app.SyncSettings(automatic=False))
    assert app.begin(scope=stale, automatic=True) is None
    assert not app.lock.locked()

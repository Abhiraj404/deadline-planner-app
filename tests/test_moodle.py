"""Moodle parsing, storage, provider fallbacks and a Chromium dashboard test."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import app
import sources
from configuration import APP_DIR
from moodle_details import MoodleScan
from moodle_discovery import assignment_details, merge_assignments, weighting


class MoodleDetailsTests(unittest.TestCase):
    """Parsing of Moodle pages, courses, calendars and grade reports."""

    def test_assignment_fields(self):
        html = """
        <h1>Course header</h1><main id="region-main">
        <div class="activity-header"><div data-region="activity-dates">
        <div><strong>Due:</strong> Friday, 1 May 2026, 11:55 PM</div></div></div>
        <div id="intro"><p>Write a report.</p><p>Weighting: 35%</p></div></main>
        """
        item = assignment_details(BeautifulSoup(html, 'html.parser'), 'Report')
        self.assertEqual(item['title'], 'Report')
        self.assertEqual(item['due'], '2026-05-01')
        self.assertEqual(item['weighting'], '35%')
        self.assertIn('Write a report.', item['notes'])
        self.assertIsNone(weighting('Similarity score 20%; score above 40%'))

    def test_paginated_discovery_uses_authenticated_root(self):
        page = SimpleNamespace(
            url='https://moodle.test/learn/my/',
            evaluate=lambda _: {'root': 'https://moodle.test/learn', 'key': 'fixture'},
        )
        replies = [
            {'courses': [{'id': 8, 'fullname': 'Science', 'shortname': 'SCI'}], 'nextoffset': 1},
            {'courses': [{'id': 9, 'fullname': 'History', 'shortname': 'HIST'}], 'nextoffset': 2},
            {'courses': [], 'nextoffset': 2},
        ]
        offsets = []

        def post(url, **options):
            """Mocked course service that records requested offsets."""
            self.assertEqual(url, 'https://moodle.test/learn/lib/ajax/service.php')
            self.assertEqual(options['max_redirects'], 0)
            offsets.append(options['data'][0]['args']['offset'])
            payload = replies.pop(0)
            return SimpleNamespace(
                ok=True, dispose=lambda: None, json=lambda: [{'error': False, 'data': payload}]
            )

        context = SimpleNamespace(new_page=lambda: page, request=SimpleNamespace(post=post))
        scan = MoodleScan(context, page.url, {'warnings': 0}, Path('.'))
        self.addCleanup(scan.http.close)
        with patch.object(scan, 'visit', return_value=BeautifulSoup('', 'html.parser')):
            self.assertEqual(scan.courses(), {'8': 'Science', '9': 'History'})
        self.assertEqual(offsets, [0, 1, 2])
        self.assertEqual(scan.module_aliases['8'][0], 'SCI')

    def test_auth_redirects_are_not_followed_by_html_requests(self):
        requested = []

        def get(url, **options):
            """Mocked page request that records requested URLs."""
            requested.append(url)
            self.assertEqual(options['max_redirects'], 0)
            return SimpleNamespace(
                status=302, headers={'location': 'https://sso.test/login'}, dispose=lambda: None
            )

        context = SimpleNamespace(new_page=lambda: None, request=SimpleNamespace(get=get))
        scan = MoodleScan(context, 'https://moodle.test', {'warnings': 0}, Path('.'))
        self.addCleanup(scan.http.close)
        with self.assertRaisesRegex(RuntimeError, 'Sign in again'):
            scan.visit('https://moodle.test/course/view.php?id=1')
        self.assertEqual(len(requested), 1)

    def test_optional_browser_calendar_reads_legacy_markup(self):
        scan = MoodleScan(
            SimpleNamespace(new_page=lambda: None),
            'https://moodle.test',
            {'warnings': 0},
            Path('.'),
        )
        self.addCleanup(scan.http.close)
        html = """<div class="event"><a href="/calendar/view.php?id=41">Quiz</a>
                  <time datetime="2026-05-01T12:00:00+01:00">1 May</time></div>"""
        with patch.object(scan, 'visit', return_value=BeautifulSoup(html, 'html.parser')):
            (row,) = scan.calendar_events()
        self.assertEqual((row['due'], row['title'], row['review']), ('2026-05-01', 'Quiz', 1))

    def test_grade_report_only_reads_weight_column(self):
        scan = MoodleScan(
            SimpleNamespace(new_page=lambda: None),
            'https://moodle.test',
            {'warnings': 0},
            Path('.'),
        )
        self.addCleanup(scan.http.close)
        html = """<table><tr><th class="item column-itemname">Report</th>
                    <td class="column-weight">30%</td><td class="column-percentage">90%</td></tr>
                    <tr><th class="item column-itemname">Essay</th><td class="column-weight">-</td></tr></table>"""
        assignments = [dict(title='Report', weighting=None), dict(title='Essay', weighting=None)]
        with patch.object(scan, 'visit', return_value=BeautifulSoup(html, 'html.parser')):
            scan.grade_weights({'id': '8'}, assignments)
        self.assertEqual([a['weighting'] for a in assignments], ['30%', None])

    def test_calendar_module_alias(self):
        row = dict(key='event', title='Lecture', due='2026-05-01', calendar_categories=['SCI101'])
        modules = [{'id': '5', 'name': 'Science', 'aliases': ['SCI101']}]
        (result,) = merge_assignments([row], [], modules, 'https://moodle.test')
        self.assertEqual((result['module_id'], result['category']), ('5', 'Science'))

    def test_link_match_preserves_calendar_identity(self):
        event = dict(
            key='original',
            link='/mod/assign/view.php?id=42&action=view',
            title='Calendar event',
            due='2026-04-30',
            notes='Calendar notes',
        )
        assignment = dict(
            key='assignment:42',
            link='https://moodle.test/mod/assign/view.php?id=42',
            title='Report',
            category='Science',
            due='2026-05-01',
            notes='',
            weighting='35%',
            module_id='8',
        )
        rows = merge_assignments([event], [assignment], [], 'https://moodle.test')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key'], 'original')
        self.assertEqual(rows[0]['due'], '2026-05-01')
        self.assertEqual(rows[0]['notes'], 'Calendar notes')


def sample_pdf():
    """A one-page PDF containing assessment text."""
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    name = NameObject
    font = DictionaryObject(
        {
            name('/Type'): name('/Font'),
            name('/Subtype'): name('/Type1'),
            name('/BaseFont'): name('/Helvetica'),
        }
    )
    page[name('/Resources')] = DictionaryObject(
        {name('/Font'): DictionaryObject({name('/F1'): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 100 Td (Assessment brief. Weighting: 40%) Tj ET')
    page[name('/Contents')] = stream
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


class StorageTests(unittest.TestCase):
    """Imports, migrations, task identity and the document index."""

    def test_incremental_migration_and_feed_failure_preserve_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'ROOT', Path(tmp)):
            with closing(sqlite3.connect(Path(tmp) / 'deadlines.db')) as db, db:
                db.execute("""CREATE TABLE tasks(id INTEGER PRIMARY KEY, external TEXT UNIQUE,
                           title TEXT NOT NULL, due TEXT NOT NULL, source TEXT, category TEXT,
                           notes TEXT, link TEXT, priority TEXT DEFAULT 'Medium',
                           progress INTEGER DEFAULT 0, review INTEGER DEFAULT 0)""")
                db.execute("""INSERT INTO tasks VALUES(7,'Moodle:cal','Report','2026-06-12',
                           'Moodle','Moodle calendar','','','High',75,0)""")
            app.initialize()
            app.initialize()
            link = 'https://moodle.test/mod/assign/view.php?id=12'
            row = dict(
                key='cal',
                title='Report',
                due='2026-06-12',
                category='Science',
                notes='Submit a report',
                weighting='40%',
                link=link,
                module_id='8',
            )
            app.save_import('Moodle', [row], {'modules': [{'id': '8', 'name': 'Science'}]})
            app.save_import('Moodle', [dict(row, key='assignment:12')], {})
            app.save_import(
                'Moodle',
                [
                    dict(
                        row,
                        module_id=None,
                        notes='',
                        link='',
                        weighting=None,
                        category='Moodle calendar',
                    )
                ],
                {},
            )
            tasks = app.tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(
                (tasks[0]['id'], tasks[0]['priority'], tasks[0]['progress']), (7, 'High', 75)
            )
            self.assertEqual(
                (tasks[0]['category'], tasks[0]['notes'], tasks[0]['weighting']),
                ('Science', 'Submit a report', '40%'),
            )
            self.assertEqual(app.modules(), [{'id': '8', 'name': 'Science'}])
            undated = dict(row, key='assignment:13', due=None, link=link.replace('12', '13'))
            app.save_import('Moodle', [], {'assignments': [undated]})
            self.assertEqual(app.undated_assignments()[0]['title'], 'Report')
            dated = dict(undated, due='2026-07-10')
            app.save_import('Moodle', [dated], {'assignments': [dated]})
            self.assertEqual(app.undated_assignments(), [])

    def test_confirmed_deadline_removal_and_return_preserve_progress(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'ROOT', Path(tmp)):
            app.initialize()
            row = dict(
                key='assignment:12',
                title='Report',
                due='2026-06-12',
                category='Science',
                notes='Submit a report',
                link='https://moodle.test/mod/assign/view.php?id=12',
                module_id='8',
                weighting=None,
            )
            app.save_import('Moodle', [row], {'assignments': [row]})
            (task,) = app.tasks()
            app.update(
                task['id'],
                app.Task(title=task['title'], due=task['due'], progress=75, priority='High'),
            )
            undated = dict(row, due=None)
            app.save_import('Moodle', [], {'assignments': [undated], 'calendar_complete': False})
            self.assertEqual(len(app.tasks()), 1)
            app.save_import('Moodle', [], {'assignments': [undated], 'calendar_complete': True})
            self.assertEqual(app.tasks(), [])
            self.assertEqual(app.undated_assignments()[0]['title'], 'Report')
            app.save_import('Moodle', [row], {'assignments': [row]})
            (restored,) = app.tasks()
            self.assertEqual(
                (restored['id'], restored['progress'], restored['priority']),
                (task['id'], 75, 'High'),
            )
            self.assertEqual(app.undated_assignments(), [])

    def test_document_index_and_pdf_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'ROOT', Path(tmp)):
            self.assertEqual(app.assessment_documents(), [])
            folder = Path(tmp) / 'assessment_pdfs'
            folder.mkdir()
            public, missing, unindexed = (letter * 64 for letter in 'abc')
            for key in (public, unindexed):
                (folder / f'{key}.pdf').write_bytes(sample_pdf())
            index = Path(tmp) / 'assessment_index.json'
            index.write_text(
                json.dumps(
                    [
                        {'id': public, 'url': 'private', 'text': 'Assessment'},
                        {'id': missing},
                        {'id': '../invalid'},
                    ]
                ),
                encoding='utf-8-sig',
            )
            docs = app.assessment_documents()
            self.assertEqual(len(docs), 2)
            self.assertNotIn('url', docs[0])
            self.assertTrue(docs[0]['pdf_available'])
            self.assertFalse(docs[1]['pdf_available'])
            self.assertEqual(app.assessment_pdf(public).media_type, 'application/pdf')
            for key in (missing, unindexed, '../invalid'):
                with self.assertRaises(HTTPException) as failure:
                    app.assessment_pdf(key)
                self.assertEqual(failure.exception.status_code, 404)
            index.write_text('{', encoding='utf-8')
            with self.assertRaises(HTTPException) as failure:
                app.assessment_documents()
            self.assertEqual(failure.exception.status_code, 503)

    def test_pdf_extraction_and_hash_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = {'warnings': 0}
            scan = MoodleScan(
                SimpleNamespace(new_page=lambda: None), 'https://moodle.test', stats, Path(tmp)
            )
            self.addCleanup(scan.http.close)
            url = 'https://moodle.test/pluginfile.php?file=/brief.pdf&token=private'
            with patch.object(scan, 'fetch_file', return_value=(url, sample_pdf())):
                module = {'id': '8', 'name': 'Science'}
                scan.pdf(url, 'Assessment brief', module)
                for key in ('assignment:1', 'assignment:2'):
                    scan.pdf(url, 'Assessment brief', module, {'key': key, 'title': 'Report'})
            (doc,) = scan.documents.values()
            self.assertEqual(doc['assignment_keys'], ['assignment:1', 'assignment:2'])
            self.assertIn('Weighting: 40%', doc['text'])
            self.assertNotIn('token', json.dumps(doc))
            self.assertTrue((Path(tmp) / 'assessment_pdfs' / (doc['id'] + '.pdf')).is_file())


class SourceTests(unittest.TestCase):
    """Provider fallbacks and email parsing with mocked services."""

    def test_moodle_calendar_survives_detail_failure(self):
        feed = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:calendar-event
DTSTART;VALUE=DATE:20260612
SUMMARY:Report is due
CATEGORIES:Science
URL:https://moodle.test/mod/assign/view.php?id=12&token=private
END:VEVENT
END:VCALENDAR
"""
        diagnostics = {}
        with (
            patch.dict(
                'os.environ',
                {
                    'MOODLE_ICS_URL': 'https://moodle.test/calendar',
                    'MOODLE_URL': 'https://moodle.test',
                },
            ),
            patch.object(sources.Path, 'is_file', return_value=True),
            patch('sources.requests.get') as get,
            patch('moodle_details.enrich', side_effect=RuntimeError('private')),
        ):
            get.return_value.content = feed
            rows = sources.collect('Moodle', diagnostics)
        self.assertEqual(rows[0]['due'], '2026-06-12')
        self.assertEqual(rows[0]['calendar_categories'], ['Science'])
        self.assertEqual(rows[0]['link'], 'https://moodle.test/mod/assign/view.php?id=12')
        self.assertEqual(diagnostics['status'], 'Failed')
        self.assertNotIn('private', str(diagnostics))

    def test_moodle_detail_scan_runs_without_feed(self):
        with (
            patch.dict('os.environ', {'MOODLE_ICS_URL': '', 'MOODLE_URL': 'https://moodle.test'}),
            patch.object(sources.Path, 'is_file', return_value=True),
            patch('moodle_details.enrich', return_value=[]) as enrich,
        ):
            self.assertEqual(sources.collect('Moodle'), [])
        enrich.assert_called_once()

    def test_email_deadline_parser(self):
        (row,) = sources.email_rows(
            'message-id', 'Assignment', '<p>Deadline: 12 June 2026</p>', 'https://mail.test/message'
        )
        self.assertEqual(row['due'], '2026-06-12')
        self.assertTrue(row['review'])

    def test_outlook_import_with_mock_graph(self):
        account = SimpleNamespace(
            get_accounts=lambda: ['account'],
            acquire_token_silent=lambda *args, **kwargs: {'access_token': 'mock'},
        )
        with (
            patch.dict('os.environ', {'OUTLOOK_CLIENT_ID': 'mock'}),
            patch.object(sources.Path, 'exists', return_value=True),
            patch(
                'auth.outlook_app', return_value=(account, SimpleNamespace(has_state_changed=False))
            ),
            patch('sources.requests.Session.get') as get,
        ):
            get.return_value.json.return_value = {
                'value': [{'id': 'msg', 'subject': 'Deadline: 12 June 2026'}]
            }
            (row,) = sources.collect('Outlook')
        self.assertEqual(row['key'], 'msg:0')
        self.assertEqual(row['due'], '2026-06-12')

    def test_gmail_import_with_mock_api(self):
        from unittest.mock import Mock

        credentials = SimpleNamespace(expired=False)
        api = Mock()
        messages = api.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {'messages': [{'id': 'msg'}]}
        messages.get.return_value.execute.return_value = {
            'payload': {'headers': [{'name': 'Subject', 'value': 'Deadline: 12 June 2026'}]}
        }
        with (
            patch.object(sources.Path, 'exists', return_value=True),
            patch(
                'google.oauth2.credentials.Credentials.from_authorized_user_file',
                return_value=credentials,
            ),
            patch('googleapiclient.discovery.build', return_value=api),
        ):
            (row,) = sources.collect('Gmail')
        self.assertEqual(row['key'], 'msg:0')
        self.assertEqual(row['due'], '2026-06-12')


class DashboardTests(unittest.TestCase):
    """The real frontend in Chromium against mocked API responses."""

    def test_module_filters_details_and_pdf_search_in_chromium(self):
        from urllib.parse import parse_qs, urlsplit

        from playwright.sync_api import expect, sync_playwright

        html = (APP_DIR / 'index.html').read_text(encoding='utf-8')
        docs = [
            dict(
                id='a' * 64,
                title='Report brief',
                module='Science',
                module_id='8',
                assignment='Report',
                text='Submit the literature review',
                pdf_available=True,
            ),
            dict(
                id='b' * 64,
                title='Essay brief',
                module='History',
                module_id='9',
                assignment='Essay',
                text='Submit the source analysis',
                pdf_available=False,
            ),
        ]
        tasks = [
            dict(
                id=1,
                source='Moodle',
                category='Science',
                module_id='8',
                due='2026-06-12',
                title='Report',
                progress=0,
                priority='Medium',
                notes='Write a report',
                weighting='40%',
            ),
            dict(
                id=2,
                source='Moodle',
                category='History',
                module_id='9',
                due='2026-06-13',
                title='Essay',
                progress=0,
                priority='Medium',
                notes='Write an essay',
            ),
            dict(
                key='assignment:3',
                source='Moodle',
                category='Science',
                module_id='8',
                title='Lab write-up',
            ),
        ]
        text_requests = []

        def fulfill_page(route, fields, rows):
            """Answer a paged API request from an in-memory list."""
            page, size = int(fields['page'][0]), int(fields['size'][0])
            route.fulfill(
                json=dict(
                    items=rows[(page - 1) * size : page * size],
                    total=len(rows),
                    page=page,
                    size=size,
                )
            )

        def respond(route):
            """Serve frontend files and mocked API endpoints to the browser."""
            url = urlsplit(route.request.url)
            endpoint, fields = url.path, parse_qs(url.query)
            module = fields.get('module', [''])[0].removeprefix('id:')
            if endpoint in ('/assignments', '/library', '/'):
                route.fulfill(body=html, content_type='text/html; charset=utf-8')
            elif endpoint == '/planner.js' or endpoint.startswith('/static/'):
                asset = APP_DIR / endpoint.lstrip('/')
                route.fulfill(
                    body=asset.read_text(encoding='utf-8'),
                    content_type='text/css' if asset.suffix == '.css' else 'text/javascript',
                )
            elif endpoint == '/api/status':
                route.fulfill(json=dict(running=False, sources={}, moodle={'status': 'Complete'}))
            elif endpoint == '/api/modules/options':
                route.fulfill(
                    json=[dict(value='id:8', name='Science'), dict(value='id:9', name='History')]
                )
            elif endpoint == '/api/tasks' and 'page' in fields:
                fulfill_page(
                    route,
                    fields,
                    [task for task in tasks if not module or task['module_id'] == module],
                )
            elif endpoint == '/api/assessment-documents' and 'page' in fields:
                query = fields.get('q', [''])[0]
                fulfill_page(
                    route,
                    fields,
                    [
                        {key: value for key, value in doc.items() if key != 'text'}
                        for doc in docs
                        if (not module or doc['module_id'] == module) and query in doc['text']
                    ],
                )
            elif endpoint.endswith('/preview'):
                text_requests.append(endpoint)
                document_id = endpoint.split('/')[-2]
                text = next(doc['text'] for doc in docs if doc['id'] == document_id)
                route.fulfill(json={'blocks': [{'type': 'paragraph', 'text': text}]})
            else:
                route.fulfill(status=404)

        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                self.skipTest('Install Playwright Chromium to test the dashboard')
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.route('https://planner.test/**', respond)
                page.goto('https://planner.test/assignments')
                expect(page.locator('#list h3')).to_have_count(3)
                page.select_option('#module', 'id:8')
                expect(page.locator('#list h3')).to_have_count(2)
                expect(page.locator('#list')).to_contain_text('40% weighting')
                expect(page.locator('#list')).to_contain_text('Deadline unavailable')
                page.locator('#list summary').click()
                expect(page.get_by_text('Write a report')).to_be_visible()
                page.goto('https://planner.test/library')
                expect(page.locator('#docs article')).to_have_count(2)
                page.select_option('#docmodule', 'id:8')
                page.fill('#docsearch', 'literature review')
                expect(page.locator('#docs article')).to_have_count(1)
                expect(page.locator('#docs article summary').first).to_contain_text('Report brief')
                page.locator('#docs article summary').first.click()
                self.assertEqual(text_requests, [])
                self.assertNotIn(
                    'Submit the literature review', page.locator('#docs').text_content()
                )
                page.get_by_text('Document preview', exact=True).click()
                expect(page.get_by_text('Submit the literature review')).to_be_visible()
                self.assertEqual(
                    text_requests, ['/api/assessment-documents/' + 'a' * 64 + '/preview']
                )
                expect(page.get_by_role('link', name='Open PDF')).to_have_attribute(
                    'href', '/api/assessment-documents/' + 'a' * 64 + '/pdf'
                )
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()

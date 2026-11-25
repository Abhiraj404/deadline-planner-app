"""Data management, backups, sync scheduling and download scope."""

import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from test_moodle import sample_pdf

import app
import sources
from document_store import publish_documents
from moodle_details import MoodleScan
from planner_data import SyncSettings, read_documents
from sync_runtime import Job, ProcessGate


class PlannerManagementTests(unittest.TestCase):
    """Previewed maintenance actions, locks and recovery."""

    def setUp(self):
        """Create an initialised planner in a temporary folder."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(app, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.initialize()

    def row(self, key='assignment:18', due='2026-06-02', title='Report', link=True):
        """A Moodle import row for tests."""
        return dict(
            key=key,
            due=due,
            title=title,
            category='Science',
            module_id='8',
            link='https://moodle.test/mod/assign/view.php?id=18' if link else '',
            notes='Write a report',
            weighting='40%',
        )

    def maintain(self, action, manual=False):
        """Preview and apply a data management action."""
        request = app.ManagementRequest(action=action, include_manual=manual)
        preview = app.preview_management(request)
        result = app.manage_data(
            request.model_copy(update={'confirmation': preview['confirmation']})
        )
        self.assertTrue(
            (self.root / 'planner_backups' / result['backup'] / 'deadlines.db').is_file()
        )
        return result['counts']

    def put_docs(self, *documents):
        """Write documents and their index entries into the planner."""
        folder = self.root / 'assessment_pdfs'
        folder.mkdir(exist_ok=True)
        for doc in documents:
            (folder / (doc['id'] + '.pdf')).write_bytes(sample_pdf())
        (folder / 'index.json').write_text(json.dumps(documents), encoding='utf-8')

    def test_three_syncs_and_assignment_updates(self):
        def collect(source, stats, catalog, scope, job, **kwargs):
            """Mocked provider that returns two Moodle rows."""
            return [self.row(), self.row('feed')]

        with patch.object(app, 'collect', collect):
            for _ in range(3):
                self.assertIsNotNone(app.begin(scope=SyncSettings(sources=['Moodle'])))
                self.wait_for_job()
        self.assertEqual(len(app.history()), 3)
        (task,) = app.tasks()
        app.update(
            task['id'], app.Task(title='Local title', due=task['due'], priority='High', progress=80)
        )
        app.save_import('Moodle', [self.row(due='2026-06-03', title='Revised report')], {})
        (updated,) = app.tasks()
        self.assertEqual(
            (
                updated['id'],
                updated['title'],
                updated['due'],
                updated['priority'],
                updated['progress'],
            ),
            (task['id'], 'Revised report', '2026-06-03', 'High', 80),
        )
        with app.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_cleanup_merges_legacy_progress_and_identical_pdf_filenames(self):
        calendar = self.row('feed', '2026-06-01', 'Report is due', link=False)
        app.save_import('Moodle', [calendar, self.row()], {})
        app.add(app.Task(title='Manual', due='2026-08-03', progress=25))
        with app.db() as connection:
            connection.execute(
                "UPDATE tasks SET priority='High',progress=65 WHERE external='Moodle:assignment:18'"
            )
        self.put_docs(
            {
                'id': 'a' * 64,
                'title': 'Brief-v1.pdf',
                'module_id': '8',
                'assignment_key': 'assignment:18',
            },
            {
                'id': 'b' * 64,
                'title': 'Alias.pdf',
                'module_id': '9',
                'assignment_key': 'assignment:30',
            },
        )
        counts = self.maintain('duplicates')
        self.assertEqual((counts['tasks'], counts['documents'], counts['files']), (1, 1, 1))
        self.assertEqual(len(list((self.root / 'assessment_pdfs').glob('*.pdf'))), 1)
        (doc,) = app.assessment_documents()
        self.assertEqual(set(doc['assignment_keys']), {'assignment:18', 'assignment:30'})
        self.assertEqual(set(doc['module_ids']), {'8', '9'})
        (task,) = [r for r in app.tasks() if r['source'] == 'Moodle']
        self.assertEqual(
            (task['priority'], task['progress'], task['due']), ('High', 65, '2026-06-02')
        )
        app.save_import('Moodle', [calendar, self.row()], {})
        self.assertEqual(len(app.tasks()), 2)

    def test_targeted_resets_preserve_manual_and_authentication(self):
        for name in (
            '.env',
            'moodle-session.json',
            'outlook-cache.json',
            'gmail-token.json',
            'credentials.json',
        ):
            (self.root / name).write_bytes(b'fixture')
        for source in ('Outlook', 'Gmail', 'Moodle'):
            app.save_import(source, [self.row()], {})
        app.add(app.Task(title='Keep', due='2026-06-02', progress=100))
        app.add(app.Task(title='Keep this too', due='2026-06-02'))
        self.put_docs({'id': 'a' * 64, 'title': 'Brief'})
        self.assertEqual(self.maintain('outlook')['tasks'], 1)
        self.assertEqual({r['source'] for r in app.tasks()}, {'Manual', 'Gmail', 'Moodle'})
        self.assertEqual(self.maintain('completed')['tasks'], 0)
        self.assertEqual(self.maintain('imported')['files'], 1)
        self.assertEqual(len(app.tasks()), 2)
        self.assertEqual(app.assessment_documents(), [])
        self.assertEqual(self.maintain('full')['manual_tasks'], 0)
        self.assertEqual(len(app.tasks()), 2)
        self.assertFalse(app.settings().automatic)
        self.assertEqual(self.maintain('full', manual=True)['manual_tasks'], 2)
        for name in (
            '.env',
            'moodle-session.json',
            'outlook-cache.json',
            'gmail-token.json',
            'credentials.json',
        ):
            self.assertEqual((self.root / name).read_bytes(), b'fixture')

    def test_duplicate_cleanup_keeps_available_file_when_canonical_is_missing(self):
        from hashlib import sha256

        digest = sha256(sample_pdf()).hexdigest()
        self.put_docs(
            {'id': digest, 'sha256': digest, 'title': 'Missing'},
            {'id': 'b' * 64, 'title': 'Available'},
        )
        (self.root / 'assessment_pdfs' / (digest + '.pdf')).unlink()
        self.maintain('duplicates')
        (document,) = app.assessment_documents()
        self.assertTrue(document['pdf_available'])
        self.assertEqual(document['id'], 'b' * 64)

    def test_unknown_calendar_dates_do_not_chain_into_one_assignment(self):
        app.save_import(
            'Moodle',
            [
                self.row('match', '2026-06-01', 'Report is due', link=False),
                self.row('old', '2026-05-31', 'Report is due', link=False),
                self.row(),
            ],
            {},
        )
        self.assertEqual(self.maintain('duplicates')['tasks'], 1)
        self.assertEqual(len(app.tasks()), 2)

    def test_download_uses_only_confirmed_selection_and_rejects_old_preview(self):
        job = Job('preview', SyncSettings())
        job.candidates.update({'one': {'key': 'one'}, 'two': {'key': 'two'}})
        observed = []

        def enrich(*args, **kwargs):
            """Mocked scan that records which candidates were downloaded."""
            observed.extend(item['key'] for item in kwargs['candidates'])

        with (
            patch.object(app.jobs, 'last_preview', job),
            patch.object(app, 'enrich', enrich),
            patch.dict('os.environ', {'MOODLE_URL': 'https://moodle.test'}),
        ):
            (self.root / 'moodle-session.json').write_text('fixture', encoding='utf-8')
            app.download(app.DownloadSelection(preview_id=job.id, keys=['two']))
            self.wait_for_job()
            self.assertEqual(observed, ['two'])
            self.maintain('pdfs')
            with self.assertRaises(HTTPException) as rejected:
                app.download(app.DownloadSelection(preview_id=job.id, keys=['one']))
            self.assertEqual(rejected.exception.status_code, 409)

    def test_startup_recovers_a_file_move_from_uncommitted_maintenance(self):
        pdf = 'a' * 64 + '.pdf'
        self.put_docs({'id': 'a' * 64, 'title': 'Brief'})
        library = self.root / 'assessment_pdfs'
        backup = self.root / 'planner_backups' / 'unfinished'
        saved = backup / 'assessment_pdfs'
        saved.mkdir(parents=True)
        shutil.copy(library / 'index.json', saved / 'index.json')
        (library / pdf).replace(saved / pdf)
        (library / 'index.json').write_text('[]', encoding='utf-8')
        (backup / 'pending.json').write_text(
            json.dumps({'files': [pdf], 'index_existed': True}), encoding='utf-8'
        )
        app.initialize()
        self.assertTrue((library / pdf).is_file())
        self.assertTrue(app.assessment_documents()[0]['pdf_available'])
        self.assertFalse((backup / 'pending.json').exists())

    def test_confirmation_rejects_stale_plan(self):
        request = app.ManagementRequest(action='gmail')
        plan = app.preview_management(request)
        app.save_import('Gmail', [self.row()], {})
        for value in (request, request.model_copy(update={'confirmation': plan['confirmation']})):
            with self.assertRaises(HTTPException):
                app.manage_data(value)
        self.assertEqual(len(app.tasks()), 1)

    def test_manual_automatic_and_separate_process_gate(self):
        entered = threading.Event()
        proceed = threading.Event()

        def collect(*args, **kwargs):
            """Mocked provider that blocks until the test lets it continue."""
            entered.set()
            self.assertTrue(proceed.wait(5))
            return []

        other_gate = ProcessGate()
        with patch.object(app, 'collect', collect):
            first = app.begin(scope=SyncSettings(automatic=True, sources=['Moodle']))
            self.assertTrue(entered.wait(2))
            try:
                self.assertIsNone(app.begin())
                self.assertFalse(other_gate.acquire(self.root))
                self.assertTrue(app.stop_sync()['stopping'])
            finally:
                proceed.set()
            self.wait_for_job()
        self.assertIsNotNone(first)
        self.assertEqual(app.history()[0]['outcome'], 'Stopped')
        self.assertTrue(other_gate.acquire(self.root))
        other_gate.release()

    def wait_for_job(self):
        """Wait until the background job releases the lock."""
        for _ in range(250):
            if not app.lock.locked():
                return
            time.sleep(0.02)
        self.fail('Sync did not complete')


class DownloadScopeTests(unittest.TestCase):
    """Date ranges and selective, incremental document downloads."""

    def test_inclusive_presets_and_saved_custom_range(self):
        scope = SyncSettings(preset='7')
        self.assertEqual(scope.bounds(date(2026, 11, 25)), (date(2026, 11, 19), date(2026, 11, 25)))
        scope = SyncSettings(
            preset='custom',
            start='2026-01-01',
            end='2026-01-31',
            include_undated=False,
            modules=['8'],
        )
        self.assertTrue(scope.includes('2026-01-31'))
        self.assertFalse(scope.includes('2026-02-01'))
        self.assertFalse(scope.includes(None))
        self.assertFalse(scope.includes_module('9'))

    def test_calendar_utc_midnight_offset(self):
        feed = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260601T230000Z\r\nSUMMARY:Report is due\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        with (
            patch.dict(
                'os.environ',
                {
                    'MOODLE_ICS_URL': 'https://moodle.test/feed',
                    'MOODLE_URL': '',
                    'MOODLE_TIMEZONE': 'Europe/London',
                },
            ),
            patch('sources.requests.get') as get,
        ):
            get.return_value.content = feed
            (row,) = sources.collect('Moodle')
        self.assertEqual(row['due'], '2026-06-02')

    def test_filter_before_pdf_body_and_preview_no_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = dict(id='8', name='Science')
            scope = SyncSettings(
                preset='custom', start='2026-01-01', end='2026-01-31', include_undated=False
            )
            job = Job('preview', scope)
            scan = MoodleScan(
                SimpleNamespace(new_page=lambda: None),
                'https://moodle.test',
                job.statistics,
                root,
                scope,
                job,
            )
            try:
                with (
                    patch.object(
                        scan,
                        'file_metadata',
                        return_value=(
                            'https://moodle.test/brief.pdf',
                            {'Content-Type': 'application/pdf'},
                            '2026-02-22',
                        ),
                    ) as head,
                    patch.object(scan, 'fetch_file') as get,
                ):
                    scan.pdf('https://moodle.test/mod/resource/view.php?id=11', 'Brief', module)
                    scan.pdf(
                        'https://moodle.test/old.pdf',
                        'Old',
                        module,
                        dict(due='2026-02-22', key='assignment:2'),
                    )
                    self.assertEqual(head.call_count, 1)
                    self.assertEqual(job.statistics['documents_outside_range'], 2)
                    head.return_value = (
                        'https://moodle.test/brief.pdf',
                        {'Content-Type': 'application/pdf'},
                        '2026-01-22',
                    )
                    scan.pdf('https://moodle.test/mod/resource/view.php?id=12', 'Brief', module)
                    self.assertEqual(len(job.candidates), 1)
                    get.assert_not_called()
                    self.assertEqual(read_documents(root), [])
            finally:
                scan.http.close()

    def test_pdf_three_scans_skip_bodies_and_replace_updated_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = dict(id='8', name='Science')
            scope = SyncSettings()
            link = 'https://moodle.test/mod/resource/view.php?id=11'
            final = 'https://moodle.test/pluginfile.php/example/brief.pdf'
            for i, version in enumerate(('v1', 'v1', 'v1', 'v2')):
                scan = MoodleScan(
                    SimpleNamespace(new_page=lambda: None), 'https://moodle.test', {}, root, scope
                )
                try:
                    data = sample_pdf() + (b'\n% changed' if version == 'v2' else b'')
                    with (
                        patch.object(
                            scan,
                            'file_metadata',
                            return_value=(
                                final,
                                {'Content-Type': 'application/pdf', 'ETag': version},
                                '2026-01-22',
                            ),
                        ),
                        patch.object(scan, 'fetch_file', return_value=(final, data)) as get,
                    ):
                        scan.pdf(link, 'Assessment brief', module, resource=True)
                        self.assertEqual(get.call_count, 1 if i in (0, 3) else 0)
                    publish_documents(root, scan.documents)
                    self.assertEqual(len(read_documents(root)), 1)
                    self.assertEqual(len(list((root / 'assessment_pdfs').glob('*.pdf'))), 1)
                finally:
                    scan.http.close()

    def test_one_of_two_resources_moves_to_new_pdf_without_stealing_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = dict(id='8', name='Science')
            scope = SyncSettings()
            for phase in range(5):
                scan = MoodleScan(
                    SimpleNamespace(new_page=lambda: None), 'https://moodle.test', {}, root, scope
                )
                try:
                    keys = (11, 12) if phase % 2 else (12, 11)
                    for key in keys:
                        changed = key == 11 and phase > 0
                        version = 'new' if changed else 'old'
                        link = f'https://moodle.test/mod/resource/view.php?id={key}'
                        data = sample_pdf() + (b'\n% updated' if changed else b'')
                        final = f'https://moodle.test/pluginfile.php/brief-{key}.pdf'
                        with (
                            patch.object(
                                scan,
                                'file_metadata',
                                return_value=(
                                    final,
                                    {'Content-Type': 'application/pdf', 'ETag': version},
                                    '',
                                ),
                            ),
                            patch.object(scan, 'fetch_file', return_value=(final, data)) as fetch,
                        ):
                            scan.pdf(link, 'Assessment brief', module, resource=True)
                            self.assertEqual(
                                fetch.call_count, int(phase == 0 or changed and phase == 1)
                            )
                    publish_documents(root, scan.documents, scan.observed)
                    records = read_documents(root)
                    self.assertEqual(len(records), 1 if phase == 0 else 2)
                    membership = [key for doc in records for key in doc['resource_keys']]
                    self.assertCountEqual(membership, ['8:activity:11', '8:activity:12'])
                finally:
                    scan.http.close()

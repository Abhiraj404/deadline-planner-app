"""Moodle file downloads: retries, closing responses, limits and cancellation."""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests import Response
from test_moodle import sample_pdf

from moodle_details import MoodleScan, enrich
from moodle_http import MoodleError
from planner_data import SyncSettings, SyncStopped
from sync_runtime import Job

BASE = 'https://moodle.test'
FILE = BASE + '/pluginfile.php/course/assessment.pdf'
MODULE = {'id': '8', 'name': 'Science'}


def file_response(status=200, content=b'', **headers):
    """A requests Response with the given status, body and headers."""
    response = Response()
    response.status_code = status
    response.headers.update(headers)
    response._content = content
    response._content_consumed = True
    response.close = Mock(wraps=response.close)
    return response


class MoodleScannerTests(unittest.TestCase):
    """Download behaviour of MoodleScan with mocked HTTP."""

    def setUp(self):
        """Create a temporary data folder and a mocked browser context."""
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.context = SimpleNamespace(new_page=lambda: None, cookies=lambda _: [])

    def scan(self, scope=None, job=None):
        """A MoodleScan for the mocked site, closed after the test."""
        scan = MoodleScan(self.context, BASE, {'warnings': 0}, self.root, scope, job)
        self.addCleanup(scan.http.close)
        return scan

    def test_shared_file_retains_assignment_links_with_one_head_and_get(self):
        scan = self.scan(SyncSettings())
        head = file_response(**{'Content-Type': 'application/pdf', 'ETag': '"v1"'})
        body = file_response(content=sample_pdf(), **{'Content-Type': 'application/pdf'})

        with (
            patch.object(scan.http, 'head', return_value=head) as get_metadata,
            patch.object(scan.http, 'get', return_value=body) as get_body,
        ):
            for key in ('assignment:1', 'assignment:2'):
                scan.pdf(
                    FILE, 'Assessment', MODULE, {'key': key, 'title': key, 'due': '2026-06-01'}
                )

        (document,) = scan.documents.values()
        self.assertEqual(document['assignment_keys'], ['assignment:1', 'assignment:2'])
        self.assertEqual(get_metadata.call_count, 1)
        self.assertEqual(get_body.call_count, 1)

    def test_transient_body_response_is_closed_and_retried(self):
        scan = self.scan()
        unavailable = file_response(503)
        body = file_response(content=sample_pdf(), **{'Content-Type': 'application/pdf'})

        with patch.object(scan.http, 'get', side_effect=[unavailable, body]) as get:
            scan.pdf(FILE, 'Assessment', MODULE)

        self.assertEqual(len(scan.documents), 1)
        self.assertEqual(get.call_count, 2)
        unavailable.close.assert_called_once()
        body.close.assert_called_once()

    def test_permission_failure_is_closed_without_retry(self):
        scan = self.scan()
        denied = file_response(403)

        with patch.object(scan.http, 'get', return_value=denied) as get:
            with self.assertRaises(MoodleError):
                scan.fetch_file(FILE)

        self.assertEqual(get.call_count, 1)
        denied.close.assert_called_once()

    def test_retry_disposes_html_response_before_next_request(self):
        scan = self.scan()
        closed = []
        page = SimpleNamespace(
            status=200,
            ok=True,
            headers={'content-type': 'text/html'},
            text=lambda: '<main>Course</main>',
            dispose=lambda: closed.append(200),
        )
        unavailable = SimpleNamespace(
            status=503, ok=False, headers={}, dispose=lambda: closed.append(503)
        )

        def get(*args, **kwargs):
            """Return the unavailable response until the page is marked closed."""
            return page if closed else unavailable

        scan.context.request = SimpleNamespace(get=get)
        self.assertEqual(scan.get_page(BASE + '/course/view.php?id=8')[1], '<main>Course</main>')
        self.assertEqual(closed, [503, 200])

    def test_retry_limit_is_bounded(self):
        scan = self.scan()
        replies = [file_response(503) for _ in range(4)]

        with patch.object(scan.http, 'get', side_effect=replies) as get:
            with self.assertRaises(MoodleError):
                scan.fetch_file(FILE)

        self.assertEqual(get.call_count, 3)
        for response in replies[:3]:
            response.close.assert_called_once()

    def test_cancelled_body_is_closed_without_writing(self):
        job = Job('sync', SyncSettings())
        scan = self.scan(job=job)
        body = file_response(**{'Content-Type': 'application/pdf'})

        def chunks(_):
            """Yield part of a PDF, then cancel the job mid-download."""
            yield sample_pdf()[:50]
            job.stop.set()
            yield b'% trailing comment'

        body.iter_content = chunks
        with patch.object(scan.http, 'get', return_value=body):
            with self.assertRaises(SyncStopped):
                scan.pdf(FILE, 'Assessment', MODULE)

        self.assertFalse(list(self.root.rglob('*.pdf')))
        body.close.assert_called_once()

    def test_interrupted_scan_closes_http_then_browser_context(self):
        job = Job('sync', SyncSettings())
        closed = []
        self.context.route = Mock()
        self.context.close = lambda: closed.append('context')
        browser = SimpleNamespace(
            new_context=lambda **options: self.context, close=lambda: closed.append('browser')
        )
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda: browser))
        http = SimpleNamespace(close=lambda: closed.append('http'))

        with (
            patch('playwright.sync_api.sync_playwright', return_value=nullcontext(playwright)),
            patch('moodle_details.requests.Session', return_value=http),
            patch.object(MoodleScan, 'run', side_effect=SyncStopped),
        ):
            with self.assertRaises(SyncStopped):
                enrich([], BASE, root=self.root, scope=job.scope, job=job)

        self.assertEqual(closed, ['http', 'context', 'browser'])

    def test_valid_pdf_exceeding_declared_limit_is_not_streamed(self):
        scan = self.scan()
        body = file_response(
            content=sample_pdf(),
            **{
                'Content-Type': 'application/pdf',
                'Content-Length': '30000001',
            },
        )
        self.addCleanup(body.close)
        with (
            patch.object(scan.http, 'get', return_value=body),
            patch.object(body, 'iter_content', wraps=body.iter_content) as read,
        ):
            scan.pdf(FILE, 'Assessment', MODULE)
        self.assertFalse(list(self.root.rglob('*.pdf')))
        read.assert_not_called()

    def test_invalid_pdf_with_valid_signature_is_never_written(self):
        scan = self.scan()
        body = file_response(
            content=b'%PDF-1.7\nnot a PDF\n%%EOF', **{'Content-Type': 'application/pdf'}
        )

        with patch.object(scan.http, 'get', return_value=body):
            scan.pdf(FILE, 'Assessment', MODULE)

        self.assertEqual(scan.documents, {})
        self.assertFalse(list(self.root.rglob('*.pdf')))
        self.assertEqual(scan.stats['warnings'], 1)


if __name__ == '__main__':
    unittest.main()

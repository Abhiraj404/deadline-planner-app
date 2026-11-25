"""Run one authenticated Moodle scan with Playwright and publish what it finds.

The scan is split by responsibility: ``moodle_http`` (requests and retries),
``moodle_discovery`` (courses, assignments and dates), ``moodle_documents`` (files)
and ``moodle_urls`` (URL rules). This module only coordinates them.
"""

import logging
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

from document_store import publish_documents
from moodle_discovery import MoodleDiscovery, merge_assignments, weighting
from moodle_documents import MoodleDocuments
from moodle_http import MoodleError, MoodleTransport
from planner_data import SyncStopped

logger = logging.getLogger('deadline_planner.moodle')


class MoodleScan(MoodleTransport, MoodleDiscovery, MoodleDocuments):
    """Coordinate discovery and document processing for one Moodle scan."""

    def __init__(self, context, base, diagnostics, root, scope=None, job=None):
        """Prepare a scan for one Moodle site using a signed-in browser context."""
        self._attach_session(context, base)
        self.root = root
        self.stats = diagnostics
        self.module_aliases = {}
        self.scope = scope
        self.job = job
        self._load_document_index()
        self.http = requests.Session()

    def check(self):
        """Stop the scan if the user cancelled the job."""
        if self.job:
            self.job.check()

    def bump(self, key):
        """Increase one scan statistic by one."""
        self.stats[key] = self.stats.get(key, 0) + 1

    def issue(self, stage, error, item=None):
        """Record a warning by stage and error type without exposing private details."""
        reason = str(error) if isinstance(error, MoodleError) else type(error).__name__
        self.bump('warnings')
        self.stats['last_warning'] = f'{stage}: {reason}'
        logger.warning('Moodle %s item=%s: %s', stage, item or '-', reason)

    def _fill_weights(self, modules, assignments):
        """Fill missing weightings from briefs, then from the grade report."""
        pending = {item['key'] for item in assignments if not item['weighting']}
        document_text = defaultdict(list)
        for document in self.documents.values():
            for key in document['assignment_keys']:
                if key in pending:
                    document_text[key].append(document['text'])
        by_module = defaultdict(list)
        for assignment in assignments:
            self.check()
            if assignment['key'] in document_text:
                assignment['weighting'] = weighting('\n'.join(document_text[assignment['key']]))
            if not assignment['weighting']:
                by_module[assignment['module_id']].append(assignment)
        for module in modules:
            if module['id'] in by_module:
                self.grade_weights(module, by_module[module['id']])

    def run(self):
        """Scan eligible courses, then fill any remaining assignment weights."""
        course_names = self.courses()
        modules = [
            dict(id=key, name=name, aliases=self.module_aliases.get(key, []))
            for key, name in course_names.items()
        ]
        self.stats['modules'] = len(modules)
        assignments = []
        for module in modules:
            self.check()
            if not self.scope or self.scope.includes_module(module['id']):
                self._scan_course(module, assignments)
        if not self.job or self.job.mode != 'preview':
            self._fill_weights(modules, assignments)
        return modules, assignments


def enrich(
    rows,
    base,
    diagnostics=None,
    catalog=None,
    root=Path('.'),
    include_calendar=False,
    scope=None,
    job=None,
    candidates=None,
):
    """Run an authenticated scan and publish each completed document."""
    from playwright.sync_api import sync_playwright

    stats = diagnostics if diagnostics is not None else {}
    stats.update(
        status='Scanning', warnings=0, modules=0, modules_scanned=0, assignments=0, documents=0
    )
    interrupted = False
    modules, assignments = [], []
    if job:
        job.check()

    def block_assets(route):
        """Skip images, fonts and files while browsing to keep scans fast."""
        path = urlsplit(route.request.url).path.lower()
        if (
            route.request.resource_type in ('image', 'font', 'media')
            or '/pluginfile.php' in path
            or path.endswith(('.pdf', '.docx'))
        ):
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(storage_state=str(root / 'moodle-session.json'))
            try:
                context.route('**/*', block_assets)
                scan = MoodleScan(context, base, stats, root, scope, job)
                try:
                    if candidates is None:
                        modules, assignments = scan.run()
                    else:
                        for item in candidates:
                            scan.pdf(
                                item['url'],
                                item['title'],
                                item['module'],
                                item['assignment'],
                                item['resource'],
                                resource_key=item['key'],
                            )
                    if include_calendar:
                        rows.extend(scan.calendar_events())
                except SyncStopped:
                    interrupted = True
                finally:
                    scan.http.close()
            finally:
                context.close()
        finally:
            browser.close()

    if catalog is not None:
        catalog.update(modules=modules, assignments=assignments)
    if not job or job.mode != 'preview':
        publish_documents(root, scan.documents, scan.observed)
    if interrupted:
        raise SyncStopped()
    stats.update(
        status='Partial' if stats['warnings'] else 'Complete',
        assignments=len(assignments),
        documents=len(scan.documents),
    )
    logger.info(
        'Moodle scan: %d modules, %d assignments, %d documents, %d warnings',
        stats['modules'],
        stats['assignments'],
        stats['documents'],
        stats['warnings'],
    )
    return merge_assignments(rows, assignments, modules, scan.base)

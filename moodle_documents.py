"""Discover Moodle assessment files and hand their bytes to the document processor."""

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from document_processing import process_document
from local_files import write_atomic
from moodle_http import MoodleError
from moodle_urls import ACTIVITY, CA, MAIN_CONTENT, activity_id, file_identity, word_url
from pdf_processing import PdfProcessingError
from planner_data import (
    SyncStopped,
    combine_documents,
    document_filename,
    group_documents,
    read_documents,
)
from word_processing import DOCX_MIME, WordProcessingError

ASSESSMENT_FILE = re.compile(
    r'\b(?:CA\s*\d+|continuous assessment|assessment brief|assignment brief|marking rubric|coursework brief|submission deadline)\b',
    re.I,
)


def associate_document(document, assignment):
    """Link a document to an assignment, keeping its first assignment title."""
    if assignment['key'] not in document['assignment_keys']:
        document['assignment_keys'].append(assignment['key'])
    if not document['assignment_key']:
        document.update(assignment=assignment['title'], assignment_key=assignment['key'])


class MoodleDocuments:
    """Discover, identify and retain assessment documents for a scan."""

    def _load_document_index(self):
        """Load already downloaded documents so unchanged files are skipped."""
        self.documents = {}
        self.documents_by_digest = {}
        self.downloaded_urls = {}
        self.seen = set()
        self.observed = {}
        self.cached = {}
        self.legacy = {}
        self.legacy_by_digest = {}
        self.metadata = {}
        if self.scope or self.job:
            for digest, group in group_documents(self.root, read_documents(self.root)).items():
                doc = combine_documents(group[0], group[1:], digest)
                self.legacy_by_digest[digest] = doc
                for old in group:
                    self.legacy[old['id']] = doc
                for key in doc['resource_keys']:
                    self.cached[key] = doc

    def resource_key(self, url, module):
        """A stable key for a file: its course plus activity ID or file path."""
        activity = activity_id(url, self.base, ACTIVITY)
        if activity:
            return f'{module["id"]}:activity:{activity}'
        return f'{module["id"]}:file:{file_identity(url)}'

    def _embedded_files(self, content, page_url):
        """Yield same-site files embedded in a page (iframes and objects)."""
        for element in content.select('iframe[src], embed[src], object[data]'):
            try:
                linked = urljoin(page_url, element.get('src') or element.get('data') or '')
            except ValueError:
                continue
            if self.allowed(linked) and '/pluginfile.php' in urlsplit(linked).path:
                yield linked

    def _scan_resource_page(self, url, title, module, assignment, key=None):
        """Find the files on a resource page that wraps its document in HTML."""
        if self.job and self.job.mode == 'download':
            raise MoodleError('A selected resource changed. Refresh the import preview')
        if key is None:
            page = self.visit(url)
            page_url = self.current_url
            content = page.select_one(MAIN_CONTENT) or page
            self.scan_links(content, module, assignment)
            for linked in self._embedded_files(content, page_url):
                self.pdf(linked, title, module, assignment)
            return
        page_url, html = self.get_page(url)
        page = BeautifulSoup(html, 'html.parser')
        content = page.select_one(MAIN_CONTENT) or page
        links = {
            href: label
            for href, label in self.links(content, page_url=page_url)
            if '/pluginfile.php' in urlsplit(href).path
        }
        for linked in self._embedded_files(content, page_url):
            links.setdefault(linked, title)
        for linked, label in links.items():
            self.pdf(
                linked,
                label or title,
                module,
                assignment,
                resource_key=key if len(links) == 1 else None,
            )

    def _register_document(self, document, module, assignment, candidate=None):
        """Record the file and all observed associations in the existing index format."""
        key = document['id']
        if candidate:
            resource = candidate['key']
            self.observed[resource] = key
            resources = document.setdefault('resource_keys', [])
            if resource not in resources:
                resources.append(resource)
            document.setdefault('resource_versions', {})[resource] = candidate['version']
        if assignment:
            associate_document(document, assignment)
        modules = document.setdefault('module_ids', [document['module_id']])
        if module['id'] not in modules:
            modules.append(module['id'])
        self.documents[key] = document
        self.documents_by_digest[document['sha256']] = key
        self.stats['documents'] = len(self.documents)

    def prepare_pdf(self, url, title, module, assignment, resource, resource_key=None):
        """Apply scope and version checks before a file body can be downloaded."""
        due = assignment.get('due') if assignment else None
        if assignment and not self.scope.includes(due):
            self.bump('documents_outside_range')
            return None
        source_key = self.resource_key(url, module)
        key = resource_key or source_key
        marker = (key, source_key, assignment.get('key') if assignment else None)
        if marker in self.seen:
            self.bump('duplicates_prevented')
            return None
        self.seen.add(marker)
        metadata = self.metadata.get(url)
        if metadata is None:
            metadata = self.file_metadata(url)
            if metadata[0] and metadata[1]:
                self.metadata[url] = metadata
                self.metadata.setdefault(metadata[0], metadata)
        final, headers, modified = metadata
        if not final:
            return None
        kind = headers.get('Content-Type', '').lower()
        if resource and 'html' in kind:
            self._scan_resource_page(final, title, module, assignment, key)
            return None
        if kind and (
            'html' in kind
            or (
                'pdf' not in kind
                and DOCX_MIME not in kind
                and 'octet-stream' not in kind
                and not word_url(final)
            )
        ):
            return None
        day = due if assignment else modified
        if not assignment and not self.scope.includes(day):
            self.bump('documents_outside_range')
            return None
        existing = self.cached.get(key) or self.legacy.get(file_identity(final))
        version = headers.get('ETag') or headers.get('Last-Modified') or ''
        candidate = dict(
            key=key,
            url=url,
            title=title,
            module=module,
            assignment=assignment,
            date=day,
            version=version,
            resource=resource,
        )
        if existing and (
            not version or existing.get('resource_versions', {}).get(key, version) == version
        ):
            path = self.root / 'assessment_pdfs' / document_filename(existing)
            if path.is_file():
                prior = self.documents.get(existing['id'])
                updated = combine_documents(
                    prior or existing, [existing] if prior else [], existing['sha256']
                )
                updated['document_date'] = day or updated.get('document_date', '')
                self._register_document(updated, module, assignment, candidate)
                self.bump('existing_pdfs_skipped')
                return None
        if self.job and self.job.mode == 'preview':
            self.job.candidates[key] = candidate
            return None
        candidate['final'] = final
        return candidate

    def pdf(self, url, title, module, assignment=None, resource=False, resource_key=None):
        """Process one candidate and retain all links to an already downloaded file."""
        try:
            self.check()
            candidate = None
            if self.scope:
                candidate = self.prepare_pdf(url, title, module, assignment, resource, resource_key)
                if candidate is None:
                    return
            version = candidate['version'] if candidate else ''
            expected_url = candidate['final'] if candidate else url
            key = self.downloaded_urls.get((expected_url, version)) or self.downloaded_urls.get(
                (url, version)
            )
            if key:
                self.bump('duplicates_prevented')
                self._register_document(self.documents[key], module, assignment, candidate)
                return
            final, data = self.fetch_file(url)
            if data is None:
                if resource and final:
                    self._scan_resource_page(final, title, module, assignment)
                return
            document = self._process_pdf(data, final, title, module, assignment, candidate)
            if document:
                self.downloaded_urls[(url, version)] = document['id']
                self.downloaded_urls[(final, version)] = document['id']
        except SyncStopped:
            raise
        except (PdfProcessingError, WordProcessingError) as error:
            self.issue('Document validation', MoodleError(str(error)), module['id'])
        except Exception as error:
            self.issue('Document download', error, module['id'])

    def _process_pdf(self, data, url, title, module, assignment, candidate):
        """Validate, extract, deduplicate and store one downloaded document."""
        self.check()
        digest = hashlib.sha256(data).hexdigest()
        document = self.documents.get(self.documents_by_digest.get(digest))
        existing = self.legacy_by_digest.get(digest) if candidate else None
        if (
            document is None
            and existing
            and (self.root / 'assessment_pdfs' / document_filename(existing)).is_file()
        ):
            document = combine_documents(existing, [], digest)
        if document is not None:
            self.bump('duplicates_prevented')
        else:
            processed = process_document(data, self.check)
            file_type, text = processed['file_type'], processed['text']
            if processed['extraction_error']:
                self.issue(
                    'PDF text extraction', MoodleError('Could not extract all text'), digest[:12]
                )
            parsed = urlsplit(url)
            file_path = parse_qs(parsed.query).get('file', [''])[0]
            if not assignment and not (
                CA.search(f'{title} {parsed.path} {file_path}')
                or ASSESSMENT_FILE.search(text[:3000])
            ):
                return None
            self.check()
            folder = self.root / 'assessment_pdfs'
            folder.mkdir(exist_ok=True)
            write_atomic(folder / f'{digest}.{file_type}', data)
            self.bump('new_pdfs_downloaded')
            document = dict(
                id=digest,
                sha256=digest,
                title=title or 'Assessment document',
                file_type=file_type,
                module=module['name'],
                module_id=module['id'],
                text=text,
                assignment='',
                assignment_key='',
                assignment_keys=[],
                document_date=candidate['date'] if candidate else None,
                added_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                resource_keys=[],
                resource_versions={},
                source='Moodle',
                **processed['flags'],
            )
        self._register_document(document, module, assignment, candidate)
        return document

    def scan_links(self, soup, module, assignment=None):
        """Process the document links found in a piece of page content."""
        self._scan_linked_pdfs(self.links(soup), module, assignment)

    def _scan_linked_pdfs(self, links, module, assignment=None):
        """Download links that look like briefs or belong to an assignment."""
        for url, title in dict(links).items():
            parsed = urlsplit(url)
            path = parsed.path.lower()
            if '/pluginfile.php' not in path:
                continue
            if (
                assignment
                or path.endswith(('.pdf', '.docx'))
                or CA.search(title)
                or (parse_qs(parsed.query).get('file', [''])[0].lower().endswith(('.pdf', '.docx')))
            ):
                self.pdf(url, title, module, assignment)

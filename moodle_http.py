"""Moodle requests: same-site checks, redirects, retries and bounded downloads."""

from email.utils import parsedate_to_datetime
from time import sleep
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from requests.exceptions import ChunkedEncodingError, SSLError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from moodle_urls import MAIN_CONTENT, origin, word_url
from pdf_processing import MAX_PDF_BYTES, PdfProcessingError, read_pdf_body
from source_http import RETRY_STATUS
from word_processing import DOCX_MIME

REDIRECTS = {301, 302, 303, 307, 308}
READ_RETRY_DELAYS = (0.25, 0.5)


class MoodleError(RuntimeError):
    """A diagnostic safe to display without URLs, credentials or page contents."""


class _TransientMoodleError(MoodleError):
    """A read failed with a status that is safe to retry."""


def _check_read_status(status, headers, label):
    """Raise for HTTP errors; transient ones are retried by the caller."""
    if status in RETRY_STATUS:
        # Leave delayed retries to the next scan if Moodle supplied Retry-After.
        wait = headers.get('retry-after') or headers.get('Retry-After')
        error = MoodleError if wait else _TransientMoodleError
        raise error(f'{label} returned HTTP {status}')


class MoodleTransport:
    """Read Moodle pages and files with the authenticated browser session."""

    def _attach_session(self, context, base):
        """Use a signed-in Playwright context and remember the site address."""
        self.context = context
        self.page = context.new_page()
        self.landing_url = base
        self.current_url = base
        self.base = base.rstrip('/')

    def session_key(self):
        """Read Moodle's session key and site root from the signed-in page."""
        config = self.page.evaluate(
            '() => ({root: window.M?.cfg?.wwwroot, key: window.M?.cfg?.sesskey})'
        )
        if not config.get('key') or not config.get('root'):
            raise MoodleError('Authenticated Moodle configuration was not found')
        if not self.allowed(config['root']):
            raise MoodleError('Moodle reported an unexpected installation origin')
        self.base = config['root'].rstrip('/')
        return config['key']

    def course_service(self, key, offset):
        """Call Moodle's enrolled-courses web service for one page of courses."""
        self.check()
        method = 'core_course_get_enrolled_courses_by_timeline_classification'
        response = self.context.request.post(
            self.base + '/lib/ajax/service.php',
            params={'sesskey': key, 'info': method},
            data=[
                dict(
                    index=0,
                    methodname=method,
                    args=dict(classification='allincludinghidden', limit=100, offset=offset),
                )
            ],
            max_redirects=0,
            timeout=25000,
        )
        try:
            self.check()
            if not response.ok:
                raise MoodleError(f'Course service returned HTTP {response.status}')
            result = response.json()[0]
        finally:
            response.dispose()
        if result['error']:
            raise MoodleError('Course service rejected the request')
        return result['data']

    def allowed(self, url):
        """True only for URLs on the configured Moodle origin."""
        site = origin(url)
        return site is not None and site == origin(self.base)

    def visit(self, url, rendered=False):
        """Load a same-site page and return parsed HTML."""
        self.check()
        if not self.allowed(url):
            raise MoodleError('Unexpected Moodle origin')
        if rendered:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=25000)
            self.current_url = self.page.url
            if not self.allowed(self.current_url) or '/login/' in urlsplit(self.current_url).path:
                raise MoodleError('Sign in again with auth.py moodle')
            if response is None or not response.ok:
                raise MoodleError(
                    f'Page returned HTTP {response.status}' if response else 'No page response'
                )
            self.check()
            html = self.page.content()
        else:
            self.current_url, html = self.get_page(url)
        self.check()
        soup = BeautifulSoup(html, 'html.parser')
        if soup.select_one('form#login, body#page-login-index'):
            raise MoodleError('Sign in again with auth.py moodle')
        if soup.select_one('.box.errorbox, [data-region="error-message"]'):
            raise MoodleError('Moodle denied access or could not display this page')
        if not rendered and not soup.select_one(MAIN_CONTENT):
            return self.visit(self.current_url, rendered=True)
        return soup

    def _retry_read(self, read, *errors):
        """Retry a read within its redirect hop, after the failed response is released."""
        transient = (_TransientMoodleError, *errors)
        for delay in (*READ_RETRY_DELAYS, None):
            self.check()
            try:
                return read()
            except transient as error:
                if delay is None or isinstance(error, SSLError):
                    raise
                self.check()
                sleep(delay)

    def get_page(self, url):
        """Read a same-origin HTML page and return its resolved URL and source."""

        def get(url):
            """Fetch a page, following only same-site redirects."""
            response = self.context.request.get(url, max_redirects=0, timeout=20000)
            try:
                self.check()
                if response.status in REDIRECTS:
                    target = response.headers.get('location')
                    if not target:
                        raise MoodleError('Page redirect had no destination')
                    return target, None
                _check_read_status(response.status, response.headers, 'Page')
                if not response.ok:
                    raise MoodleError(f'Page returned HTTP {response.status}')
                if 'html' not in response.headers.get('content-type', '').lower():
                    raise MoodleError('Expected an HTML page')
                return None, response.text()
            finally:
                response.dispose()

        for _ in range(8):
            if not self.allowed(url) or '/login/' in urlsplit(url).path:
                raise MoodleError('Sign in again with auth.py moodle')
            target, html = self._retry_read(lambda: get(url), PlaywrightTimeoutError)
            if target is None:
                return url, html
            url = urljoin(url, target)
        raise MoodleError('Too many page redirects')

    def _file_request(self, method, url, read, **options):
        """Make a file request with the browser's cookies, without following redirects."""

        def request():
            """Send the request with the session cookies and read the response."""
            cookies = {cookie['name']: cookie['value'] for cookie in self.context.cookies([url])}
            self.http.cookies.clear()
            with method(url, cookies=cookies, allow_redirects=False, **options) as response:
                self.check()
                headers = response.headers
                if response.status_code in REDIRECTS:
                    target = headers.get('Location')
                    if not target:
                        raise MoodleError('File redirect had no destination')
                    return target, None
                _check_read_status(response.status_code, headers, 'File')
                return None, read(response)

        return self._retry_read(
            request, RequestsConnectionError, RequestsTimeout, ChunkedEncodingError
        )

    def fetch_file(self, url):
        """Fetch a same-origin assessment document through verified redirects."""

        def read(response):
            """Read a file body within the size limit, rejecting HTML error pages."""
            if not response.ok:
                raise MoodleError(f'File returned HTTP {response.status_code}')
            kind = response.headers.get('Content-Type', '').lower()
            length = response.headers.get('Content-Length', '')
            if (
                ('pdf' in kind or DOCX_MIME in kind)
                and length.isascii()
                and length.isdecimal()
                and int(length) > MAX_PDF_BYTES
            ):
                raise MoodleError('PDF exceeds 30 MB')
            try:
                return read_pdf_body(
                    response.iter_content(65536),
                    self.check,
                    MAX_PDF_BYTES,
                    allow_word=DOCX_MIME in kind or word_url(url),
                ), kind
            except PdfProcessingError as error:
                raise MoodleError(str(error)) from error

        for _ in range(8):
            self.check()
            if not self.allowed(url):
                self.bump('external_files_skipped')
                return None, None
            if '/login/' in urlsplit(url).path:
                raise MoodleError('Sign in again with auth.py moodle')
            target, body = self._file_request(
                self.http.get, url, read, stream=True, timeout=(10, 25)
            )
            if target is None:
                data, kind = body
                return (url, data) if data is not None or 'html' in kind else (None, None)
            url = urljoin(url, target)
        raise MoodleError('Too many file redirects')

    def file_metadata(self, url):
        """Resolve the file URL and read version and date headers without a body."""
        for _ in range(8):
            self.check()
            if not self.allowed(url) or '/login/' in urlsplit(url).path:
                return None, {}, ''
            target, headers = self._file_request(
                self.http.head,
                url,
                lambda response: response.headers if response.ok else {},
                timeout=(8, 15),
            )
            if target is not None:
                url = urljoin(url, target)
                continue
            modified = headers.get('Last-Modified', '')
            try:
                day = (
                    parsedate_to_datetime(modified).astimezone().date().isoformat()
                    if modified
                    else ''
                )
            except (ValueError, TypeError, OverflowError):
                day = ''
            return url, headers, day
        return None, {}, ''

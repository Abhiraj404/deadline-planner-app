"""Validate downloaded PDFs and extract bounded text for the assessment index."""

from contextlib import ExitStack
from io import BytesIO
from os import getenv
from pathlib import Path
from shutil import which
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from tempfile import TemporaryDirectory
from time import monotonic

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

MAX_PDF_BYTES = 30_000_000
MAX_TEXT_CHARS = 100_000
MAX_TEXT_PAGES = 200
PDF_HEADER = b'%PDF-'
PDF_HEADER_WINDOW = 1024
MAX_OCR_PAGES = 12
OCR_COMMAND_TIMEOUT = 15


class PdfProcessingError(ValueError):
    """A PDF could not be processed, with a safe diagnostic."""


def extraction_limit(name, default, ceiling):
    """Read a positive integer limit from the environment, clamped to a safe ceiling."""
    try:
        return min(max(int(getenv(name, default)), 1), ceiling)
    except ValueError:
        return default


def text_limit():
    """Maximum characters of document text stored for search."""
    return extraction_limit('PLANNER_DOCUMENT_MAX_TEXT_CHARS', MAX_TEXT_CHARS, 500_000)


def ocr_enabled() -> bool:
    """OCR is opt-in and only uses locally installed Poppler and Tesseract executables."""
    return getenv('PLANNER_DOCUMENT_OCR', '').lower() in ('1', 'true', 'yes', 'on')


def run_ocr_command(command, check):
    """Run a local OCR tool with no shell, no stdin and a hard timeout."""
    deadline = monotonic() + OCR_COMMAND_TIMEOUT
    with Popen(command, stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL) as process:
        try:
            while monotonic() < deadline:
                check()
                try:
                    output, _ = process.communicate(timeout=0.25)
                except TimeoutExpired:
                    continue
                if process.returncode:
                    raise PdfProcessingError('Local OCR command failed')
                return output
            raise PdfProcessingError('Local OCR command timed out')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def ocr_pdf_page(path, page, renderer, recognizer, check):
    """Render one page to PNG with pdftoppm, then read it with Tesseract."""
    prefix = path.with_name('page')
    run_ocr_command(
        [
            renderer,
            '-f',
            str(page),
            '-l',
            str(page),
            '-scale-to',
            '2000',
            '-png',
            '-singlefile',
            str(path),
            str(prefix),
        ],
        check,
    )
    result = run_ocr_command([recognizer, str(prefix.with_suffix('.png')), 'stdout'], check)
    return result.decode('utf-8', errors='replace').strip()


def read_pdf_body(chunks, check, limit=MAX_PDF_BYTES, *, allow_word=False):
    """Read a bounded PDF or permitted Word package, or return None for other content."""
    signatures = (PDF_HEADER, b'PK\x03\x04') if allow_word else (PDF_HEADER,)
    data = bytearray()
    identified = False
    for chunk in chunks:
        check()
        if len(data) + len(chunk) > limit:
            raise PdfProcessingError('PDF exceeds 30 MB')
        data.extend(chunk)
        if not identified:
            header = data[:PDF_HEADER_WINDOW].lstrip()
            identified = header.startswith(signatures)
            if not identified and (
                len(data) >= PDF_HEADER_WINDOW
                or not any(signature.startswith(header) for signature in signatures)
            ):
                return None
    check()
    return bytes(data) if identified else None


def extract_pdf_preview(data, check, *, max_chars=None, max_pages=None):
    """Return bounded selectable text, optional local OCR text and completeness metadata."""
    check()
    if not data[:PDF_HEADER_WINDOW].lstrip().startswith(PDF_HEADER) or b'%%EOF' not in data:
        raise PdfProcessingError('Invalid or incomplete PDF')
    with BytesIO(data) as stream, ExitStack() as reader_scope:
        try:
            reader = reader_scope.enter_context(PdfReader(stream))
            pages = reader.pages
            page_count = len(pages)
        except FileNotDecryptedError:
            return {
                'text': '',
                'text_incomplete': True,
                'extraction_error': False,
                'ocr_used': False,
                'page_count': 0,
            }
        except Exception as error:
            raise PdfProcessingError('Invalid or incomplete PDF') from error
        if not page_count:
            raise PdfProcessingError('PDF contains no pages')

        max_pages = max_pages or extraction_limit(
            'PLANNER_DOCUMENT_MAX_PDF_PAGES', MAX_TEXT_PAGES, 500
        )
        remaining = max_chars or text_limit()
        use_ocr = ocr_enabled()
        renderer = which('pdftoppm') if use_ocr else None
        recognizer = which('tesseract') if use_ocr else None
        ocr_path = None
        ocr_count = 0
        ocr_used = False
        parts = []
        incomplete = page_count > max_pages
        extraction_error = False
        for index in range(min(page_count, max_pages)):
            check()
            try:
                part = (pages[index].extract_text() or '').strip()
            except Exception:
                incomplete = extraction_error = True
                break
            if not part and renderer and recognizer and ocr_count < MAX_OCR_PAGES:
                ocr_count += 1
                try:
                    if ocr_path is None:
                        directory = reader_scope.enter_context(TemporaryDirectory())
                        path = Path(directory) / 'source.pdf'
                        path.write_bytes(data)
                        ocr_path = path
                    part = ocr_pdf_page(ocr_path, index + 1, renderer, recognizer, check)
                    ocr_used = ocr_used or bool(part)
                except (OSError, PdfProcessingError):
                    extraction_error = True
            if not part:
                incomplete = True
                continue
            if parts:
                remaining -= 1
            if len(part) > remaining:
                if remaining > 0:
                    parts.append(part[:remaining])
                incomplete = True
                break
            parts.append(part)
            remaining -= len(part)
        return {
            'text': '\n'.join(parts).strip(),
            'text_incomplete': incomplete,
            'extraction_error': extraction_error,
            'ocr_used': ocr_used,
            'page_count': page_count,
        }

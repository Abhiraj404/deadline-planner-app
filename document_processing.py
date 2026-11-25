"""Turn untrusted PDF or Word bytes into bounded, searchable text.

This module knows nothing about Moodle, email or SQLite. Every importer (Moodle
scans, manual uploads and the demo data builder) calls ``process_document`` and
receives the same result shape, which keeps validation rules in one place.

Security: documents are parsed as data only. PDFs are read with pypdf, Word files
with a streaming XML parser that rejects DTDs, and macro-enabled packages are
refused. Nothing embedded in a document is executed or opened.
"""

from collections.abc import Callable
from typing import TypedDict

from pdf_processing import MAX_PDF_BYTES, PdfProcessingError, extract_pdf_preview
from word_processing import WordProcessingError, extract_word_preview

DocumentError = (PdfProcessingError, WordProcessingError)


class ExtractionFlags(TypedDict):
    """How complete a document's text extraction was."""

    text_incomplete: bool
    extraction_error: bool
    ocr_used: bool


class ProcessedDocument(TypedDict):
    """The detected type, extracted text and completeness flags."""

    file_type: str
    text: str
    flags: ExtractionFlags
    extraction_error: bool


def detect_type(data: bytes) -> str | None:
    """Identify a supported document by its leading bytes rather than its file name."""
    head = data[:1024].lstrip()
    if head.startswith(b'%PDF-'):
        return 'pdf'
    if data.startswith(b'PK\x03\x04'):
        return 'docx'
    return None


def process_document(data: bytes, check: Callable[[], None]) -> ProcessedDocument:
    """Validate a PDF or DOCX and return its text with completeness indicators.

    ``check`` is called between pages and XML chunks so a sync can be cancelled.
    Raises ``PdfProcessingError`` or ``WordProcessingError`` for unsafe or unreadable
    files.
    """
    if len(data) > MAX_PDF_BYTES:
        raise PdfProcessingError('Document exceeds 30 MB')
    file_type = detect_type(data)
    if file_type == 'docx':
        result = extract_word_preview(data, check)
        flags = ExtractionFlags(
            text_incomplete=result['text_incomplete'], extraction_error=False, ocr_used=False
        )
    elif file_type == 'pdf':
        result = extract_pdf_preview(data, check)
        flags = ExtractionFlags(
            text_incomplete=result['text_incomplete'],
            extraction_error=result['extraction_error'],
            ocr_used=result['ocr_used'],
        )
    else:
        raise PdfProcessingError('Only PDF and DOCX files are supported')
    return ProcessedDocument(
        file_type=file_type,
        text=result['text'],
        flags=flags,
        extraction_error=flags['extraction_error'],
    )


def word_preview(data: bytes, check: Callable[[], None]) -> dict:
    """Return headings, paragraphs, list items, tables, headers and footers of a DOCX.

    PDFs have no reliable structure to recover, so their preview uses the stored text.
    """
    result = extract_word_preview(data, check)
    return {'blocks': result['preview'], 'text_incomplete': result['text_incomplete']}

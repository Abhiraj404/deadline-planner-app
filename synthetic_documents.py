"""Build small, synthetic PDF and DOCX files for demo mode and automated tests.

The generated files contain only the text passed in. They are created at run time,
so the repository never needs to ship binary documents.
"""

import io
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from word_processing import MAIN_TYPE, WORD_NAMESPACE

LINES_PER_PAGE = 48


def _pdf_literal(text: str) -> bytes:
    """Escape text as a PDF string literal in Latin-1."""
    # Standard PDF fonts use Latin-1. Replace anything else so the file stays valid.
    safe = text.encode('latin-1', errors='replace')
    return b'(' + safe.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)') + b')'


def build_pdf(lines: list[str]) -> bytes:
    """Return a text PDF with one line of text per entry."""
    writer = PdfWriter()
    name = NameObject
    font = DictionaryObject(
        {
            name('/Type'): name('/Font'),
            name('/Subtype'): name('/Type1'),
            name('/BaseFont'): name('/Helvetica'),
        }
    )
    for offset in range(0, max(len(lines), 1), LINES_PER_PAGE):
        page = writer.add_blank_page(595, 842)
        page[name('/Resources')] = DictionaryObject(
            {name('/Font'): DictionaryObject({name('/F1'): font})}
        )
        body = b'BT /F1 11 Tf 14 TL 50 800 Td '
        for line in lines[offset : offset + LINES_PER_PAGE]:
            body += _pdf_literal(line) + b' Tj T* '
        stream = DecodedStreamObject()
        stream.set_data(body + b'ET')
        page[name('/Contents')] = stream
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def _run(text: str) -> str:
    """A Word text run that keeps spaces."""
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def build_docx(blocks: list[dict]) -> bytes:
    """Return a minimal Word document.

    Blocks use the same shapes as the Word preview: ``heading`` (with ``level``),
    ``paragraph``, ``list_item`` and ``table`` (with ``rows``).
    """
    body = []
    for block in blocks:
        kind = block['type']
        if kind == 'heading':
            style = f'<w:pPr><w:pStyle w:val="Heading{block.get("level", 1)}"/></w:pPr>'
            body.append(f'<w:p>{style}{_run(block["text"])}</w:p>')
        elif kind == 'list_item':
            numbering = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            body.append(f'<w:p>{numbering}{_run(block["text"])}</w:p>')
        elif kind == 'table':
            rows = ''.join(
                '<w:tr>'
                + ''.join(f'<w:tc><w:p>{_run(cell)}</w:p></w:tc>' for cell in row)
                + '</w:tr>'
                for row in block['rows']
            )
            body.append(f'<w:tbl>{rows}</w:tbl>')
        else:
            body.append(f'<w:p>{_run(block["text"])}</w:p>')
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as package:
        package.writestr(
            '[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{MAIN_TYPE}"/></Types>',
        )
        package.writestr('word/document.xml', document)
    return buffer.getvalue()

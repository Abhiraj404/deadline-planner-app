"""PDF text extraction limits and optional OCR."""

from io import BytesIO
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)


def pdf_sample(*texts):
    """A PDF with one page per text."""
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=100, height=100)
        page[NameObject('/Resources')] = DictionaryObject(
            {
                NameObject('/Font'): DictionaryObject(
                    {
                        NameObject('/F1'): DictionaryObject(
                            {
                                NameObject('/Type'): NameObject('/Font'),
                                NameObject('/Subtype'): NameObject('/Type1'),
                                NameObject('/BaseFont'): NameObject('/Helvetica'),
                            }
                        ),
                    }
                ),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    with BytesIO() as result:
        writer.write(result)
        return result.getvalue()


def test_pdf_preview_records_page_and_character_cutoffs():
    from pdf_processing import extract_pdf_preview

    data = pdf_sample('First page', 'Second page')
    page_limited = extract_pdf_preview(data, lambda: None, max_pages=1)
    char_limited = extract_pdf_preview(data, lambda: None, max_chars=5)
    exact = extract_pdf_preview(data, lambda: None, max_chars=22)
    assert (page_limited['text'], page_limited['text_incomplete']) == ('First page', True)
    assert (char_limited['text'], char_limited['text_incomplete']) == ('First', True)
    assert (exact['text'], exact['text_incomplete']) == ('First page\nSecond page', False)
    assert extract_pdf_preview(data, lambda: None)['extraction_error'] is False


def test_local_ocr_is_only_used_for_textless_pages(monkeypatch):
    from pdf_processing import extract_pdf_preview

    monkeypatch.setenv('PLANNER_DOCUMENT_OCR', '1')
    with (
        patch('pdf_processing.which', side_effect=lambda command: command),
        patch('pdf_processing.ocr_pdf_page', return_value='Scanned page') as ocr,
    ):
        result = extract_pdf_preview(pdf_sample('Selectable text', ''), lambda: None)
    assert result['text'] == 'Selectable text\nScanned page'
    assert result['text_incomplete'] is False
    assert ocr.call_args.args[1] == 2

"""Word document validation, extraction, previews and storage."""

from contextlib import closing
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import pytest

import app
from document_store import publish_documents
from moodle_details import MoodleScan
from planner_data import SyncSettings
from word_processing import (
    DOCX_MIME,
    MAIN_TYPE,
    WORD_NAMESPACE,
    WordProcessingError,
    extract_word_preview,
)


def word_sample(xml=None, macro=False, extra_parts=None):
    """A minimal DOCX package, optionally with a macro or extra parts."""
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as package:
        package.writestr(
            '[Content_Types].xml',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/word/document.xml" ContentType="{MAIN_TYPE}"/></Types>',
        )
        package.writestr(
            'word/document.xml',
            xml
            or f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>'
            '<w:p><w:r><w:t>Instructions and weighting: 40%</w:t></w:r></w:p>'
            '</w:body></w:document>',
        )
        if macro:
            package.writestr('word/vbaProject.bin', b'fixture')
        for name, content in (extra_parts or {}).items():
            package.writestr(name, content)
    return buffer.getvalue()


def test_word_candidate_is_stored_searchable_and_downloadable(tmp_path):
    module = {'id': '1', 'name': 'Science'}
    url = 'https://moodle.test/pluginfile.php/5/course/brief.docx'
    blob = word_sample()
    with patch.object(app, 'ROOT', tmp_path):
        app.initialize()
        for _ in range(2):
            scan = MoodleScan(
                SimpleNamespace(new_page=lambda: None),
                'https://moodle.test',
                {},
                tmp_path,
                SyncSettings(),
            )
            with (
                closing(scan.http),
                patch.object(
                    scan,
                    'file_metadata',
                    return_value=(
                        url,
                        {'Content-Type': DOCX_MIME, 'ETag': 'fixture'},
                        '2026-01-01',
                    ),
                ),
                patch.object(scan, 'fetch_file', return_value=(url, blob)) as fetch,
            ):
                scan.pdf(url, 'Assessment brief.docx', module)
                assert fetch.call_count == int(_ == 0)
                publish_documents(tmp_path, scan.documents, scan.observed)
        app.initialize()
        (row,) = app.assessment_documents(page=1, q='weighting')['items']
        assert row['file_type'] == 'docx'
        assert row['pdf_available']
        assert app.assessment_text(row['id'])['text'] == 'Instructions and weighting: 40%'
        response = app.assessment_word(row['id'])
        assert response.media_type == DOCX_MIME
        assert (
            'Assessment brief.docx' in response.headers['content-disposition']
            or 'Assessment%20brief.docx' in response.headers['content-disposition']
        )
        assert len(list((tmp_path / 'assessment_pdfs').glob('*.docx'))) == 1
        preview = app.preview_management(app.ManagementRequest(action='pdfs'))
        assert preview['counts']['files'] == 1


@pytest.mark.parametrize(
    'data',
    [
        word_sample(macro=True),
        word_sample('<!DOCTYPE doc [<!ENTITY x "unsafe">]><doc>&x;</doc>'),
        b'PK\x03\x04invalid',
    ],
)
def test_word_extractor_rejects_unsupported_or_unsafe_packages(data):
    with pytest.raises(WordProcessingError):
        extract_word_preview(data, lambda: None)


def test_word_body_requires_an_expected_type():
    from pdf_processing import read_pdf_body

    payload = word_sample()
    chunks = [payload[:2], payload[2:]]
    assert read_pdf_body(chunks, lambda: None) is None
    assert read_pdf_body(chunks, lambda: None, allow_word=True) == payload


def test_word_preview_preserves_structure_and_literal_text():
    from word_processing import extract_word_preview

    data = word_sample(
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Rubric</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Section</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>40%</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/></w:numPr></w:pPr>'
        '<w:r><w:t>&lt;script&gt;</w:t></w:r></w:p>'
        '</w:body></w:document>',
        extra_parts={
            'word/header1.xml': f'<w:hdr xmlns:w="{WORD_NAMESPACE}">'
            '<w:p><w:r><w:t>Science</w:t></w:r></w:p></w:hdr>',
            'word/footer1.xml': f'<w:ftr xmlns:w="{WORD_NAMESPACE}">'
            '<w:p><w:r><w:t>2026</w:t></w:r></w:p></w:ftr>',
        },
    )
    result = extract_word_preview(data, lambda: None)
    assert result['preview'] == [
        {'type': 'header', 'text': 'Science'},
        {'type': 'heading', 'level': 2, 'text': 'Rubric'},
        {'type': 'table', 'rows': [['Section', '40%']]},
        {'type': 'list_item', 'level': 1, 'text': '<script>'},
        {'type': 'footer', 'text': '2026'},
    ]
    assert result['text'] == 'Science\nRubric\nSection\t40%\n<script>\n2026'
    assert result['text_incomplete'] is False


def test_word_preview_marks_text_cutoff():
    from word_processing import extract_word_preview

    result = extract_word_preview(word_sample(), lambda: None, max_chars=12)
    assert result['text'] == 'Instructions'
    assert result['text_incomplete'] is True

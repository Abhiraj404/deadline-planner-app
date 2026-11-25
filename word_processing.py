"""Extract structured text from Word packages without opening embedded objects."""

from io import BytesIO
from re import fullmatch
from xml.etree.ElementTree import TreeBuilder
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from pdf_processing import text_limit

DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
WORD_NAMESPACE = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
MAX_XML_BYTES = 8_000_000
MAX_MARGIN_PARTS = 8
# ElementTree lookups need Clark notation ({namespace}tag); expat reports 'namespace tag'.
WORD = f'{{{WORD_NAMESPACE}}}'
TRANSPARENT_BLOCKS = {WORD + name for name in ('sdt', 'sdtContent', 'customXml', 'ins')}


class WordProcessingError(ValueError):
    """A Word package could not be safely read."""


def read_word_xml(package, name, check, start, end=None, text=None):
    """Stream-parse one package part with size limits and DTDs rejected."""
    member = package.getinfo(name)
    if member.file_size > MAX_XML_BYTES:
        raise WordProcessingError('Word XML exceeds the extraction limit')
    parser = expat.ParserCreate(namespace_separator=' ')
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text

    def reject_declaration(*args):
        """Refuse document type declarations, which can define entities."""
        raise WordProcessingError('Word XML contains a prohibited document declaration')

    parser.StartDoctypeDeclHandler = reject_declaration
    total = 0
    with package.open(member) as stream:
        while chunk := stream.read(65_536):
            check()
            total += len(chunk)
            if total > MAX_XML_BYTES:
                raise WordProcessingError('Word XML exceeds the extraction limit')
            parser.Parse(chunk, False)
    parser.Parse(b'', True)


def clark_name(name: str) -> str:
    """Convert an expat 'namespace tag' name into ElementTree's '{namespace}tag' form."""
    namespace, separator, tag = name.rpartition(' ')
    return f'{{{namespace}}}{tag}' if separator else tag


def parse_word_part(package, path, check, expected_root):
    """Parse one bounded Word XML part into an element tree with DTDs rejected."""
    tree = TreeBuilder()

    def start(name, attributes):
        """Pass an element to the tree builder with names converted to {namespace}tag."""
        tree.start(clark_name(name), {clark_name(key): value for key, value in attributes.items()})

    read_word_xml(package, path, check, start, lambda name: tree.end(clark_name(name)), tree.data)
    element = tree.close()
    if element.tag != WORD + expected_root:
        raise WordProcessingError('Invalid Word document part')
    return element


def validate_word_package(package, check):
    """Require a standard Word main part and refuse macro projects."""
    valid_type = False

    def content_type(name, attributes):
        """Note whether the manifest declares a standard Word main document."""
        nonlocal valid_type
        if (
            name.endswith(' Override')
            and attributes.get('PartName') == '/word/document.xml'
            and attributes.get('ContentType') == MAIN_TYPE
        ):
            valid_type = True

    if any('vbaproject' in name.lower() for name in package.namelist()):
        raise WordProcessingError('Macro-enabled Word files are unsupported')
    read_word_xml(package, '[Content_Types].xml', check, content_type)
    if not valid_type:
        raise WordProcessingError('File is not a standard Word document')


def paragraph_text(paragraph):
    """Text of a paragraph, keeping tabs and line breaks."""
    spacing = {WORD + 'tab': '\t', WORD + 'br': '\n', WORD + 'cr': '\n'}
    return ''.join(
        (element.text or '') if element.tag == WORD + 't' else spacing.get(element.tag, '')
        for element in paragraph.iter()
    ).strip()


def paragraph_block(paragraph, area):
    """Classify a paragraph as heading, list item, paragraph, header or footer."""
    text = paragraph_text(paragraph)
    if not text:
        return None
    properties = paragraph.find(WORD + 'pPr')
    if area == 'body' and properties is not None:
        style = properties.find(WORD + 'pStyle')
        if style is not None:
            heading = fullmatch(r'Heading\s*([1-9])', style.get(WORD + 'val', ''), flags=2)
            if heading:
                return {'type': 'heading', 'text': text, 'level': int(heading.group(1))}
        numbering = properties.find(WORD + 'numPr')
        if numbering is not None:
            depth = numbering.find(WORD + 'ilvl')
            value = depth.get(WORD + 'val', '') if depth is not None else ''
            level = int(value) if fullmatch(r'[0-8]', value) else 0
            return {'type': 'list_item', 'text': text, 'level': level}
    return {'type': 'paragraph' if area == 'body' else area, 'text': text}


def word_blocks(container, area):
    """Yield the paragraphs and tables in a part, looking inside content controls."""
    pending = [iter(container)]
    while pending:
        node = next(pending[-1], None)
        if node is None:
            pending.pop()
        elif node.tag in TRANSPARENT_BLOCKS:
            pending.append(iter(node))
        elif node.tag == WORD + 'p':
            if block := paragraph_block(node, area):
                yield block
        elif node.tag == WORD + 'tbl':
            yield {
                'type': 'table',
                'rows': (
                    (
                        '\n'.join(map(paragraph_text, cell.iter(WORD + 'p')))
                        for cell in row.iterfind(WORD + 'tc')
                    )
                    for row in node.iterfind(WORD + 'tr')
                ),
            }


def append_preview_block(block, parts, preview, remaining):
    """Add a block to the preview within the remaining character budget."""
    if parts:
        remaining -= 1
    if block['type'] != 'table':
        text = block['text']
        if remaining > 0:
            parts.append(text[:remaining])
            preview.append(dict(block, text=text[:remaining]))
        return remaining - len(text)
    rows = []
    for source_row in block['rows']:
        if rows:
            remaining -= 1
        row = []
        for cell in source_row:
            if row:
                remaining -= 1
            if remaining < 0:
                break
            row.append(cell[:remaining])
            remaining -= len(cell)
            if remaining < 0:
                break
        if row:
            rows.append(row)
        if remaining < 0:
            break
    if rows:
        parts.append('\n'.join('\t'.join(row) for row in rows))
        preview.append({'type': 'table', 'rows': rows})
    return remaining


def extract_word_preview(data, check, *, max_chars=None):
    """Read bounded body and margin text and preserve basic document structure."""
    remaining = max_chars or text_limit()
    parts = []
    preview = []
    incomplete = False
    check()
    try:
        with ZipFile(BytesIO(data)) as package:
            validate_word_package(package, check)
            body = parse_word_part(package, 'word/document.xml', check, 'document').find(
                WORD + 'body'
            )
            if body is None:
                raise WordProcessingError('Invalid Word document body')
            headers = sorted(
                name for name in package.namelist() if fullmatch(r'word/header\d+\.xml', name)
            )
            footers = sorted(
                name for name in package.namelist() if fullmatch(r'word/footer\d+\.xml', name)
            )
            incomplete = max(len(headers), len(footers)) > MAX_MARGIN_PARTS
            sections = [
                *((name, 'header', 'hdr') for name in headers[:MAX_MARGIN_PARTS]),
                (None, 'body', None),
                *((name, 'footer', 'ftr') for name in footers[:MAX_MARGIN_PARTS]),
            ]
            for path, area, root in sections:
                check()
                try:
                    container = parse_word_part(package, path, check, root) if path else body
                except (BadZipFile, WordProcessingError, expat.ExpatError):
                    incomplete = True
                    continue
                for block in word_blocks(container, area):
                    check()
                    remaining = append_preview_block(block, parts, preview, remaining)
                    if remaining < 0:
                        break
                if remaining < 0:
                    incomplete = True
                    break
    except (BadZipFile, KeyError, expat.ExpatError, RuntimeError, NotImplementedError) as error:
        raise WordProcessingError('Invalid or unreadable Word document') from error
    return {'text': '\n'.join(parts).strip(), 'preview': preview, 'text_incomplete': incomplete}

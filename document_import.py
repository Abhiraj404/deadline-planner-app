"""Import a PDF or Word file chosen by the user, without any external account.

Uploaded bytes are treated as untrusted. They are size-limited, identified by their
content (not their name), parsed by ``document_processing`` and stored under a name
derived from their SHA-256 digest, so a crafted file name can never choose a path.
Identical files are detected by digest and are not stored twice.
"""

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from database import open_database, update_document_registry
from document_processing import process_document
from document_store import publish_documents
from local_files import write_atomic
from planner_data import PDF_ID, combine_documents, group_documents, read_documents

MAX_TITLE_CHARS = 200
MAX_MODULE_CHARS = 200


def clean_title(filename: str) -> str:
    """Use only the final path component of a browser-supplied name, without controls."""
    name = re.split(r'[\\/]', filename or '')[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip()
    return name[:MAX_TITLE_CHARS] or 'Uploaded document'


def find_document(root: Path, document_id: str) -> dict | None:
    """Return the indexed document with this ID or digest, or None."""
    if not PDF_ID.fullmatch(document_id):
        return None
    for digest, group in group_documents(root, read_documents(root)).items():
        doc = combine_documents(group[0], group[1:], digest)
        if doc['id'] == document_id or document_id in {item['id'] for item in group}:
            return doc
    return None


def import_upload(
    root: Path,
    data: bytes,
    filename: str,
    module: str = '',
    check: Callable[[], None] = lambda: None,
) -> tuple[dict, bool]:
    """Store one uploaded document and return ``(document, created)``.

    The caller must hold the operation lock, because Moodle syncs also write the
    document index.
    """
    digest = hashlib.sha256(data).hexdigest()
    existing = find_document(root, digest)
    if existing is not None:
        return existing, False
    processed = process_document(data, check)
    folder = root / 'assessment_pdfs'
    folder.mkdir(exist_ok=True)
    write_atomic(folder / f'{digest}.{processed["file_type"]}', data)
    module_name = re.sub(r'[\x00-\x1f\x7f]', '', module or '').strip()[:MAX_MODULE_CHARS]
    document = dict(
        id=digest,
        sha256=digest,
        title=clean_title(filename),
        file_type=processed['file_type'],
        module=module_name,
        module_id='',
        text=processed['text'],
        assignment='',
        assignment_key='',
        assignment_keys=[],
        module_ids=[],
        document_date='',
        added_at=datetime.now().astimezone().isoformat(timespec='seconds'),
        resource_keys=[],
        resource_versions={},
        source='Upload',
        **processed['flags'],
    )
    publish_documents(root, {digest: document}, observed={})
    with open_database(root) as connection:
        update_document_registry(connection, root)
    return document, True


def link_document_to_task(root: Path, document_id: str, task_id: int, title: str) -> None:
    """Attach a confirmed task to its source document so the task shows the file."""
    document = find_document(root, document_id)
    if document is None:
        raise LookupError('Assessment document not found')
    key = f'task:{task_id}'
    if key not in document['assignment_keys']:
        document['assignment_keys'].append(key)
    if not document.get('assignment'):
        document.update(assignment=title, assignment_key=key)
    publish_documents(root, {document['id']: document}, observed={})
    with open_database(root) as connection:
        update_document_registry(connection, root)

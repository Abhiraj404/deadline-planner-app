"""Publish the local assessment document index without depending on any provider.

``assessment_pdfs/index.json`` is the durable record of downloaded and uploaded
files. SQLite holds a searchable copy that ``update_document_registry`` rebuilds
from this index, so the index is always written first.
"""

import json
import logging
from uuid import uuid4

from local_files import write_atomic
from planner_data import combine_documents, document_filename, read_documents

logger = logging.getLogger('deadline_planner.documents')


def publish_documents(root, changed, observed=None):
    """Merge changed documents into the index and archive files that lost every owner."""
    directory = root / 'assessment_pdfs'
    index = directory / 'index.json'
    documents = {doc['id']: doc for doc in read_documents(root)}
    owners = (
        observed
        if observed is not None
        else {
            resource: doc['id']
            for doc in changed.values()
            for resource in doc.get('resource_keys', [])
        }
    )
    replaced = []
    for key, doc in list(documents.items()):
        resources = doc.get('resource_keys') or []
        remaining = [resource for resource in resources if owners.get(resource, key) == key]
        if len(remaining) != len(resources):
            if remaining or key in changed:
                doc['resource_keys'] = remaining
                doc['resource_versions'] = {
                    r: v for r, v in (doc.get('resource_versions') or {}).items() if r in remaining
                }
            else:
                del documents[key]
                replaced.append(document_filename(doc))
    for key, doc in changed.items():
        old = documents.get(key)
        current = combine_documents(doc, [old] if old else [], doc.get('sha256') or key)
        current['resource_keys'] = [
            resource for resource in current['resource_keys'] if owners.get(resource, key) == key
        ]
        current['resource_versions'] = {
            r: v for r, v in current['resource_versions'].items() if r in current['resource_keys']
        }
        if doc.get('resource_keys') and not current['resource_keys']:
            documents.pop(key, None)
            replaced.append(document_filename(doc))
        else:
            documents[key] = current
    directory.mkdir(exist_ok=True)
    # The index must point to the replacement before old files are archived. An
    # interruption can leave an unused file, but neither index points at a moved file.
    write_atomic(index, json.dumps(list(documents.values()), ensure_ascii=False).encode('utf-8'))
    for filename in set(replaced):
        source = directory / filename
        if source.is_file():
            backup = root / 'planner_backups' / 'replaced-documents'
            try:
                backup.mkdir(parents=True, exist_ok=True)
                source.replace(backup / f'{source.stem}-{uuid4().hex}{source.suffix}')
            except OSError as error:
                logger.warning('Retained the replaced local PDF (%s)', type(error).__name__)

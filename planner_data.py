"""Shared import scope, identity and local document storage."""

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, model_validator

SOURCES = ('Moodle', 'Outlook', 'Gmail')
PDF_ID = re.compile(r'[a-f0-9]{64}')


class SyncSettings(BaseModel):
    """What a sync imports: sources, modules, date range and schedule."""

    automatic: bool = True
    minutes: int = Field(30, ge=1, le=1440)
    preset: str = 'all'
    start: date | None = None
    end: date | None = None
    semester_start: date | None = None
    semester_end: date | None = None
    include_undated: bool = True
    sources: list[str] = Field(default_factory=lambda: list(SOURCES))
    modules: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_scope(self):
        """Reject unknown presets, sources or modules and reversed date ranges."""
        if self.preset not in ('all', '7', '30', '90', 'semester', 'custom'):
            raise ValueError('Select a date range preset')
        if not set(self.sources) <= set(SOURCES):
            raise ValueError('Unknown import source')
        if any(not re.fullmatch(r'[0-9]+', item) for item in self.modules):
            raise ValueError('Invalid Moodle module ID')
        for start, end in ((self.start, self.end), (self.semester_start, self.semester_end)):
            if start and end and start > end:
                raise ValueError('Start date must precede end date')
        if self.preset == 'semester' and not (self.semester_start and self.semester_end):
            raise ValueError('Enter the start and end dates for your current semester')
        if self.preset == 'custom' and not (self.start or self.end):
            raise ValueError('Enter a start date, an end date, or both')
        return self

    def bounds(self, today=None):
        """The inclusive (start, end) dates for the chosen preset."""
        today = today or date.today()
        if self.preset.isdecimal():
            return today - timedelta(days=int(self.preset) - 1), today
        if self.preset == 'semester':
            return self.semester_start, self.semester_end
        return (self.start, self.end) if self.preset == 'custom' else (None, None)

    def includes(self, value):
        """True if a date is inside the range; undated items follow include_undated."""
        if not value:
            return self.include_undated
        start, end = self.bounds()
        day = date.fromisoformat(value)
        return (start is None or day >= start) and (end is None or day <= end)

    def includes_module(self, module_id):
        """True if the module is selected, or no modules are selected."""
        return not self.modules or module_id in self.modules


class AppPreferences(BaseModel):
    """First-run and reminder preferences. Stored in SQLite, never in .env."""

    setup_complete: bool = False
    reminders_enabled: bool = True
    # Days before a deadline when a reminder appears, for example 7 and 1.
    reminder_days: list[int] = Field(default_factory=lambda: [7, 1], max_length=6)
    include_overdue: bool = True
    browser_notifications: bool = False

    @model_validator(mode='after')
    def validate_days(self):
        """Keep reminder days between 0 and 60, unique and largest first."""
        if any(day < 0 or day > 60 for day in self.reminder_days):
            raise ValueError('Reminder days must be between 0 and 60')
        self.reminder_days = sorted(set(self.reminder_days), reverse=True)
        return self


class SyncStopped(Exception):
    """Raised inside a job when the user stops it."""

    pass


def assignment_id(link):
    """The Moodle assignment ID in an assignment link, or None."""
    parts = urlsplit(link or '')
    ids = parse_qs(parts.query).get('id', [])
    if parts.path.endswith('/mod/assign/view.php') and ids and re.fullmatch(r'[0-9]+', ids[0]):
        return ids[0]
    return None


DOCUMENT_TYPES = ('pdf', 'docx')


def document_type(value):
    """Normalise a stored file type to "pdf" or "docx"."""
    return value if value in DOCUMENT_TYPES else 'pdf'


def document_filename(doc):
    """The stored file name: the SHA-256 ID plus extension."""
    return f'{doc["id"]}.{document_type(doc.get("file_type"))}'


def read_documents(root):
    """Read the document index, accepting only entries with valid IDs."""
    local = root / 'assessment_pdfs' / 'index.json'
    path = local if local.exists() else root / 'assessment_index.json'
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(items, list):
        raise ValueError('Invalid document index')
    return [
        d
        for d in items
        if isinstance(d, dict) and isinstance(d.get('id'), str) and PDF_ID.fullmatch(d['id'])
    ]


@lru_cache(maxsize=1024)
def hash_version(path, size, modified_ns):
    """SHA-256 of a file, cached by path, size and modification time."""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def file_hash(path, verify=False):
    """SHA-256 of a file, reusing the cache unless ``verify`` is set."""
    if verify:
        return hash_version.__wrapped__(path, 0, 0)
    stat = path.stat()
    return hash_version(path, stat.st_size, stat.st_mtime_ns)


def group_documents(root, documents, verify=False):
    """Group index entries that refer to identical file contents."""
    groups = defaultdict(list)
    for doc in documents:
        path = root / 'assessment_pdfs' / document_filename(doc)
        claimed = doc.get('sha256')
        if not verify and isinstance(claimed, str) and PDF_ID.fullmatch(claimed):
            digest = claimed
        else:
            digest = file_hash(path, verify) if path.is_file() else doc['id']
        groups[digest].append(doc)
    for entries in groups.values():
        entries.sort(
            key=lambda doc: not (root / 'assessment_pdfs' / document_filename(doc)).is_file()
        )
    return groups


def combine_documents(primary, others, digest):
    """Merge duplicate index entries into one, keeping every association."""
    result = dict(primary, sha256=digest)
    for key, single in (
        ('assignment_keys', 'assignment_key'),
        ('module_ids', 'module_id'),
        ('resource_keys', None),
    ):
        values = []
        for doc in [primary, *others]:
            values.extend(doc.get(key) or [])
            if single and doc.get(single):
                values.append(doc[single])
        result[key] = list(dict.fromkeys(values))
    result['resource_versions'] = {}
    for doc in [*others, primary]:
        result['resource_versions'].update(doc.get('resource_versions') or {})
    for field in ('assignment', 'assignment_key', 'module', 'module_id', 'text', 'document_date'):
        if not result.get(field):
            result[field] = next((d[field] for d in others if d.get(field)), '')
    return result

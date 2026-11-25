"""Measure search, listing and extraction speed on synthetic data.

Usage::

    python scripts/benchmark.py                 # synthetic data only (default)
    python scripts/benchmark.py --tasks 20000   # a larger synthetic library
    python scripts/benchmark.py --local-copy    # also time a temporary copy of deadlines.db

Every search is timed twice: with the SQLite FTS5 index and with the substring
fallback that runs when FTS5 is unavailable. Only timings and row counts are
printed, never record contents, so the output is safe to share.
"""

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# Run from anywhere: the application modules live one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import listing
from assessment_extraction import extract_assessment
from database import open_database, update_document_registry
from document_processing import process_document
from synthetic_documents import build_docx, build_pdf

# A Zipf-like vocabulary: a few common words and many rare ones, like real briefs.
COMMON = (
    'the and of to a in for is on that with by be this are as your will an at module '
    'assessment report project design analysis'
).split()
RARE = [f'{stem}{n}' for stem in ('topic', 'term', 'concept') for n in range(1500)]
QUERIES = ('assessment', 'topic42', 'concept7*', 'portfolio regression', 'nonexistentword')


def median_ms(callback, repeats: int) -> float:
    """Median run time of ``callback`` in milliseconds."""
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        callback()
        durations.append(time.perf_counter() - started)
    return round(statistics.median(durations) * 1000, 2)


def sentence(rng: random.Random, words: int) -> str:
    """Random text with a few common and many rare words."""
    return ' '.join(
        rng.choice(COMMON) if rng.random() < 0.7 else rng.choice(RARE) for _ in range(words)
    )


def build_synthetic(root: Path, task_count: int, document_count: int) -> None:
    """Create a synthetic planner with ``task_count`` tasks and documents."""
    rng = random.Random(42)
    with patch.object(app, 'ROOT', root):
        app.initialize()
    with open_database(root) as connection:
        connection.executemany(
            'INSERT INTO tasks(title,due,category,notes,source,priority,progress) VALUES(?,?,?,?,?,?,?)',
            (
                (
                    sentence(rng, 4).title(),
                    f'2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}',
                    f'Module {rng.randint(1, 40)}',
                    sentence(rng, 60),
                    'Manual',
                    'Medium',
                    rng.choice((0, 50, 100)),
                )
                for _ in range(task_count)
            ),
        )
    documents = []
    for number in range(document_count):
        text = sentence(rng, 3000)
        digest = hashlib.sha256(f'{number}'.encode()).hexdigest()
        documents.append(
            dict(
                id=digest,
                sha256=digest,
                title=f'Brief {number}',
                file_type='pdf',
                module=f'Module {number % 40}',
                module_id='',
                text=text,
                assignment='',
                assignment_key='',
                assignment_keys=[],
                module_ids=[],
                document_date='',
                resource_keys=[],
                resource_versions={},
            )
        )
    with open_database(root) as connection:
        update_document_registry(connection, root, documents)


def time_searches(root: Path, repeats: int) -> None:
    """Time each query with FTS5 and with the substring fallback."""
    with patch.object(app, 'ROOT', root):
        for label, fts in (('FTS5', True), ('substring', False)):
            with patch.object(listing, 'enabled', lambda *_: fts):
                for query in QUERIES:
                    tasks = median_ms(lambda: app.tasks(page=1, q=query), repeats)
                    documents = median_ms(
                        lambda: app.assessment_documents(page=1, q=query), repeats
                    )
                    total = app.assessment_documents(page=1, q=query)['total']
                    print(
                        f'  {label:9} q={query!r:18} tasks {tasks:8.2f} ms   '
                        f'documents {documents:8.2f} ms   ({total} document matches)'
                    )
        print(f'  Unfiltered task page:     {median_ms(lambda: app.tasks(page=1), repeats)} ms')
        print(f'  Overview:                 {median_ms(app.overview_page, repeats)} ms')


def time_documents(repeats: int) -> None:
    """Time PDF, DOCX and assessment extraction on generated files."""
    lines = [sentence(random.Random(number), 12) for number in range(400)]
    lines.insert(5, 'Submission deadline: Friday 11 December 2026 at 17:00')
    pdf = build_pdf(lines)
    docx = build_docx([{'type': 'paragraph', 'text': line} for line in lines])
    text = '\n'.join(lines)
    print(
        f'  PDF extraction (9 pages):  {median_ms(lambda: process_document(pdf, lambda: None), repeats)} ms'
    )
    print(
        f'  DOCX extraction:           {median_ms(lambda: process_document(docx, lambda: None), repeats)} ms'
    )
    print(f'  Assessment extraction:     {median_ms(lambda: extract_assessment(text), repeats)} ms')


def main() -> None:
    """Parse options, build the data and print the timings."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tasks', type=int, default=5000)
    parser.add_argument('--documents', type=int, default=500)
    parser.add_argument('--repeats', type=int, default=15)
    parser.add_argument('--local-copy', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started = time.perf_counter()
        build_synthetic(root, args.tasks, args.documents)
        size = (root / 'deadlines.db').stat().st_size
        print(
            f'Synthetic library: {args.tasks} tasks, {args.documents} documents '
            f'(built in {time.perf_counter() - started:.1f} s, database {size / 1e6:.1f} MB)'
        )
        time_searches(root, args.repeats)
        if args.local_copy and (app.ROOT / 'deadlines.db').is_file():
            local = root / 'local'
            local.mkdir()
            source_uri = (app.ROOT / 'deadlines.db').as_uri() + '?mode=ro'
            with closing(sqlite3.connect(source_uri, uri=True)) as source:
                with closing(sqlite3.connect(local / 'deadlines.db')) as target:
                    source.backup(target)
            if (app.ROOT / 'assessment_pdfs' / 'index.json').is_file():
                (local / 'assessment_pdfs').mkdir()
                shutil.copy(app.ROOT / 'assessment_pdfs' / 'index.json', local / 'assessment_pdfs')
            with patch.object(app, 'ROOT', local):
                app.initialize()
            print('Temporary copy of the local database:')
            time_searches(local, args.repeats)
        print('Document processing:')
        time_documents(args.repeats)
    print(json.dumps({'sqlite': sqlite3.sqlite_version}))


if __name__ == '__main__':
    main()

"""Decide where personal data lives, and move it out of the source folder once.

Personal data (``.env``, the database, sign-in sessions, tokens, documents, backups
and logs) lives in ``data/`` beside the source code. Development versions kept it in
the project folder itself. ``migrate_legacy_data`` moves those files into ``data/``
the first time the planner starts, without overwriting anything. Until that happens,
``resolve_data_dir`` keeps using the old location so no data ever looks missing.

This module only uses the standard library (and ``local_files``) so ``run_server.py``
can call it before any third-party package is imported.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from local_files import try_lock

DATA_FOLDER = 'data'
# Everything that belongs to one person's planner. Regenerable lock files are not listed.
RUNTIME_ITEMS = (
    '.env',
    'deadlines.db',
    'deadlines.db-wal',
    'deadlines.db-shm',
    'deadlines.db-journal',
    'deadlines-backup.db',
    'moodle-session.json',
    'credentials.json',
    'gmail-token.json',
    'outlook-cache.json',
    'assessment_index.json',
    'assessment_pdfs',
    'planner_backups',
    'planner_logs',
)


def legacy_items(app_dir: Path) -> list[str]:
    """Runtime files still stored directly in the source folder."""
    return [name for name in RUNTIME_ITEMS if (app_dir / name).exists()]


def resolve_data_dir(app_dir: Path, environ: Mapping[str, str] = os.environ) -> Path:
    """Return the data folder: PLANNER_DATA_DIR, else data/, else the unmigrated legacy root."""
    configured = environ.get('PLANNER_DATA_DIR')
    if configured:
        return Path(configured).resolve()
    data_dir = app_dir / DATA_FOLDER
    if (app_dir / 'deadlines.db').is_file() and not (data_dir / 'deadlines.db').exists():
        # Not migrated yet: keep reading the old location rather than show an empty planner.
        return app_dir
    return data_dir


def _locked(path: Path) -> bool:
    """True when another planner process holds this lock file."""
    if not path.exists():
        return False
    stream = try_lock(path)
    if stream is None:
        return True
    stream.close()
    return False


def _move(source: Path, target: Path) -> None:
    """Rename source to target. Folders are merged; existing files are never replaced."""
    if not target.exists():
        source.replace(target)
    elif source.is_dir() and target.is_dir():
        for child in source.iterdir():
            _move(child, target / child.name)
        if not any(source.iterdir()):
            source.rmdir()


def migrate_legacy_data(app_dir: Path) -> list[str]:
    """Move runtime files from the source folder into data/. Returns the moved names.

    Nothing is copied or overwritten: each item is renamed on the same drive, and an
    item whose target already exists is left where it is. The move is skipped while a
    planner server or sync holds its lock.
    """
    data_dir = app_dir / DATA_FOLDER
    pending = legacy_items(app_dir)
    if not pending or (data_dir / 'deadlines.db').exists():
        return []
    if _locked(app_dir / '.planner-server.lock') or _locked(app_dir / '.planner-operation.lock'):
        raise RuntimeError('Stop the running planner before its data folder can be moved.')
    data_dir.mkdir(exist_ok=True)
    moved = []
    for name in pending:
        _move(app_dir / name, data_dir / name)
        moved.append(name)
    for lock in ('.planner-operation.lock', '.planner-server.lock'):
        (app_dir / lock).unlink(missing_ok=True)
    return moved

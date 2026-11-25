"""Local configuration, the data directory and bounded application logs.

Code and private data are kept apart. ``APP_DIR`` holds the source code and the
frontend. ``ROOT`` is the data directory that holds ``.env``, the SQLite database,
authentication sessions, downloaded documents, backups and logs: ``data/`` by default
(see ``data_location.py``). ``PLANNER_DATA_DIR`` points the application at another
data directory, which is how demo mode keeps its synthetic data apart.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from data_location import resolve_data_dir
from local_files import write_atomic

APP_DIR = Path(__file__).resolve().parent
ROOT = resolve_data_dir(APP_DIR)
DEFAULT_SYNC_MINUTES = 30
SCHEDULER_POLL_SECONDS = 5
LOG_BYTES = 1_048_576
LOG_BACKUPS = 3

# Only these keys can be written from the first-run setup page. Everything else in
# .env (and any key a user adds by hand) is preserved untouched.
EDITABLE_SETTINGS = (
    'MOODLE_URL',
    'MOODLE_ICS_URL',
    'MOODLE_TIMEZONE',
    'OUTLOOK_CLIENT_ID',
    'OUTLOOK_TENANT',
)
_ENV_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')


def app_version() -> str:
    """Return the semantic version recorded in the VERSION file."""
    try:
        return (APP_DIR / 'VERSION').read_text(encoding='utf-8').strip() or '0.0.0'
    except OSError:
        return '0.0.0'


def demo_mode() -> bool:
    """Demo mode serves synthetic data and disables every external integration."""
    return os.getenv('PLANNER_DEMO', '').lower() in ('1', 'true', 'yes', 'on')


def load_environment(root: Path = ROOT) -> None:
    """Load ``.env`` from the data folder into the process environment."""
    load_dotenv(root / '.env')


def default_sync_minutes() -> int:
    """Initial automatic sync interval from SYNC_MINUTES, kept within 1-1440."""
    try:
        return min(1440, max(1, int(os.getenv('SYNC_MINUTES', str(DEFAULT_SYNC_MINUTES)))))
    except ValueError:
        return DEFAULT_SYNC_MINUTES


def _quoted(value: str) -> str:
    """Quote a value for .env so spaces, # and = survive reading it back."""
    # Double quotes keep '#', spaces and '=' inside the value when python-dotenv reads it.
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def update_environment(root: Path, values: dict[str, str]) -> None:
    """Write whitelisted settings to ``.env`` atomically and apply them to this process.

    Existing lines, comments and unrelated keys are kept. Values never contain new
    lines, so a value cannot inject an extra setting.
    """
    unknown = set(values) - set(EDITABLE_SETTINGS)
    if unknown:
        raise ValueError('Unsupported setting')
    if any('\n' in value or '\r' in value or '\x00' in value for value in values.values()):
        raise ValueError('Settings must be a single line')
    path = root / '.env'
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        lines = []
    pending = dict(values)
    output = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match[1] in pending:
            output.append(f'{match[1]}={_quoted(pending.pop(match[1]))}')
        else:
            output.append(line)
    output.extend(f'{key}={_quoted(value)}' for key, value in pending.items())
    write_atomic(path, ('\n'.join(output) + '\n').encode('utf-8'))
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def configure_logging(root: Path) -> RotatingFileHandler | None:
    """Keep diagnostics local. Provider adapters log status or exception type only."""
    logger = logging.getLogger('deadline_planner')
    level = os.getenv('PLANNER_LOG_LEVEL', 'INFO').upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    folder = root / 'planner_logs'
    try:
        folder.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            folder / 'planner.log', maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8'
        )
    except OSError:
        logger.warning('Could not open the planner log file')
        return None
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
    return handler

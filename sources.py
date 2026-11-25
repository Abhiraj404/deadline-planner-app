"""Provider adapters: Moodle (calendar feed and scan), Gmail and Outlook."""

import logging
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from email_deadlines import email_rows, html_text
from gmail_import import collect_gmail
from outlook_import import collect_outlook
from planner_data import SyncStopped
from source_http import get_with_retries

__all__ = ['collect', 'collect_moodle', 'email_rows']

logger = logging.getLogger('deadline_planner.sources')


def calendar_rows(feed, job):
    """Turn a Moodle calendar feed into task rows in the chosen timezone."""
    from moodle_urls import calendar_link

    zone_name = os.getenv('MOODLE_TIMEZONE')
    for event in Calendar.from_ical(feed).walk('VEVENT'):
        if job:
            job.check()
        start = event.get('DTSTART')
        uid = event.get('UID')
        if not start or not uid:
            continue
        due = start.dt
        if isinstance(due, datetime):
            if due.tzinfo:
                due = due.astimezone(ZoneInfo(zone_name) if zone_name else None)
            due = due.date()
        categories = event.get('CATEGORIES', [])
        if not isinstance(categories, list):
            categories = [categories]
        yield dict(
            key=str(uid),
            title=str(event.get('SUMMARY', 'Moodle event')),
            due=due.isoformat(),
            notes=html_text(str(event.get('DESCRIPTION', ''))),
            link=calendar_link(str(event.get('URL', ''))),
            category='Moodle calendar',
            calendar_categories=[str(cat) for group in categories for cat in group.cats],
        )


def collect_moodle(diagnostics, catalog, scope=None, job=None, *, root=None):
    """Import Moodle through the calendar feed and, if signed in, a full scan."""
    from moodle_details import enrich
    from moodle_http import MoodleError

    root = Path(root) if root is not None else Path('.')
    url = os.getenv('MOODLE_ICS_URL')
    base = os.getenv('MOODLE_URL')
    rows = []
    calendar_failed = False
    if url:
        try:
            with closing(get_with_retries(requests.get, url, job=job)) as response:
                rows.extend(calendar_rows(response.content, job))
            diagnostics['calendar'] = f'{len(rows)} calendar events'
        except SyncStopped:
            raise
        except Exception as error:
            calendar_failed = True
            diagnostics['calendar'] = f'Calendar import failed ({type(error).__name__})'
            logger.warning('Moodle calendar import failed: %s', type(error).__name__)
    else:
        diagnostics['calendar'] = 'Calendar feed not configured'
    if base and (root / 'moodle-session.json').is_file():
        try:
            enriched = enrich(
                rows,
                base,
                diagnostics,
                catalog,
                root=root,
                include_calendar=not url or calendar_failed,
                scope=scope,
                job=job,
            )
            catalog['calendar_complete'] = not calendar_failed
            if calendar_failed and diagnostics.get('status') == 'Complete':
                diagnostics['status'] = 'Partial'
            return enriched
        except SyncStopped:
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, MoodleError) else type(error).__name__
            diagnostics.update(status='Failed', last_warning=reason)
            logger.warning('Moodle detail scan failed: %s', reason)
            if not url or calendar_failed:
                raise MoodleError(reason) from None
    else:
        diagnostics['status'] = 'Set MOODLE_URL and run auth.py moodle for assignment details'
    if calendar_failed:
        raise MoodleError(diagnostics['calendar'])
    return rows if url else None


def collect(source, diagnostics=None, catalog=None, scope=None, job=None, *, root=None):
    """Import one source, or return None when that source is not configured."""
    if source == 'Moodle':
        return collect_moodle(
            diagnostics if diagnostics is not None else {},
            catalog if catalog is not None else {},
            scope,
            job,
            root=root,
        )
    root = Path(root) if root is not None else Path('.')
    if source == 'Gmail':
        return collect_gmail(root / 'gmail-token.json', job)
    if source == 'Outlook':
        with requests.Session() as session:
            return collect_outlook(root / 'outlook-cache.json', session.get, job)
    return None

"""Find duplicate provider records without changing manual deadlines."""

from collections import defaultdict
from datetime import date, timedelta

from moodle_discovery import normalized_title
from planner_data import assignment_id


def parsed_date(value):
    """Parse an ISO date, returning None for missing or invalid values."""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def email_identity(row):
    """A stable identity for an email deadline: message ID plus date."""
    external = row.get('external') or ''
    key = external.partition(':')[2]
    if key.startswith('message:'):
        return key
    prefix, sep, ordinal = external.rpartition(':')
    if sep and ordinal.isdecimal() and ':' in prefix:
        return f'message:{prefix.split(":", 1)[1]}:{row["due"]}'
    return None


def task_groups(rows):
    """Match trustworthy activity IDs first, then unlinked midnight due events."""
    activities = defaultdict(list)
    calendar = []
    messages = defaultdict(list)
    for row in rows:
        if row.get('source') in ('Gmail', 'Outlook'):
            message = email_identity(row)
            if message:
                messages[row['source'], message].append(row)
            continue
        if row.get('source') != 'Moodle':
            continue
        aid = assignment_id(row.get('link'))
        if aid:
            activities[aid].append(row)
        elif row.get('module_id') and row['title'].casefold().endswith(' is due'):
            calendar.append(row)
    by_title = defaultdict(set)
    for aid, matches in activities.items():
        for row in matches:
            if row.get('module_id'):
                by_title[row['module_id'], normalized_title(row['title'])].add(aid)
    linked_dates = {
        aid: {parsed_date(row['due']) for row in matches} for aid, matches in activities.items()
    }
    for row in calendar:
        due = parsed_date(row['due'])
        if not due:
            continue
        ids = by_title[row['module_id'], normalized_title(row['title'])]
        candidates = [aid for aid in ids if linked_dates[aid] & {due, due + timedelta(days=1)}]
        if len(candidates) == 1:
            activities[candidates[0]].append(row)
    return [group for group in [*activities.values(), *messages.values()] if len(group) > 1]


def combined_task(group):
    """Merge a group of duplicate rows into the one shown to the user."""
    # Keep the earliest row's ID and choice of source key. Use the activity for published fields.
    primary = min(group, key=lambda row: row['id'])
    detailed = max(
        group,
        key=lambda row: (
            ':assignment:' in (row.get('external') or ''),
            bool(assignment_id(row.get('link'))),
            bool(row.get('weighting')),
            len(row.get('notes') or ''),
            row['id'],
        ),
    )
    priority = max(
        group,
        key=lambda row: (row.get('priority') != 'Medium', row.get('progress') or 0, -row['id']),
    )['priority']
    notes = list(dict.fromkeys(row['notes'] for row in [detailed, *group] if row.get('notes')))
    return {
        **primary,
        **{
            key: detailed.get(key)
            for key in ('title', 'due', 'category', 'link', 'module_id', 'weighting')
        },
        'notes': '\n\n'.join(notes),
        'priority': priority,
        'progress': max(row.get('progress') or 0 for row in group),
        'review': min(row.get('review') or 0 for row in group),
    }


def project_tasks(rows):
    """Replace each group of duplicates with its combined task."""
    groups = task_groups(rows)
    representatives = {min(row['id'] for row in group): combined_task(group) for group in groups}
    hidden = {row['id'] for group in groups for row in group if row['id'] not in representatives}
    return [representatives.get(row['id'], row) for row in rows if row['id'] not in hidden]

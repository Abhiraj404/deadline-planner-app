"""Extract structured assessment details from plain document text.

The input is plain text from any source (a PDF, a Word file or pasted text). The
output is a JSON-compatible dictionary described in ``docs/assessment-data-model.md``.
The module has no dependency on Moodle, SQLite, FastAPI or third-party libraries so
the same rules can be ported line by line to JavaScript for the planned Chrome
extension. The shared fixture ``tests/fixtures/extraction_cases.json`` tests both.

Rules that keep the output trustworthy:

* Nothing is invented. A field that the text does not state is ``None`` (or an empty
  list). A date without a year is reported with ``value: None`` and flagged.
* Numeric dates are read day-first (Irish and European convention). When both parts
  could be a month, for example ``03/04/2026``, the confidence is lowered.
* A date only becomes the submission deadline when nearby wording says so. Feedback,
  release, late-submission and presentation dates are reported separately.
* Every value carries the ``source_text`` it came from and a ``confidence`` of
  ``high``, ``medium`` or ``low``. ``needs_confirmation`` is true unless a single,
  unambiguous, high-confidence submission deadline was found.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

SCHEMA_VERSION = 1
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 300
MAX_SOURCE_CHARS = 200

MONTHS = {
    'jan': 1,
    'feb': 2,
    'mar': 3,
    'apr': 4,
    'may': 5,
    'jun': 6,
    'jul': 7,
    'aug': 8,
    'sep': 9,
    'oct': 10,
    'nov': 11,
    'dec': 12,
}
MONTH = (
    r'(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?'
    r'|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?'
)
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
WEEKDAY = r'(?:(?P<weekday>mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+(?:the\s+)?)?'
ORDINAL = r'(?:st|nd|rd|th)?'

# Each pattern produces day, month and an optional year. Order does not matter because
# overlapping matches are resolved by position and length.
DATE_PATTERNS = (
    (
        'named_dmy',
        re.compile(
            rf'\b{WEEKDAY}(?P<day>\d{{1,2}}){ORDINAL}(?:\s+of)?[\s-]+{MONTH},?(?:[\s-]+(?P<year>\d{{4}}))?\b',
            re.I,
        ),
    ),
    (
        'named_mdy',
        re.compile(
            rf'\b{WEEKDAY}{MONTH}\s+(?P<day>\d{{1,2}}){ORDINAL},?(?:\s+(?P<year>\d{{4}}))?\b',
            re.I,
        ),
    ),
    ('iso', re.compile(r'\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b')),
    (
        'numeric',
        re.compile(
            rf'\b{WEEKDAY}(?P<day>\d{{1,2}})(?P<sep>[/.-])(?P<month>\d{{1,2}})(?P=sep)(?P<year>\d{{4}}|\d{{2}})(?![\d.:/-]\d)',
            re.I,
        ),
    ),
)
TIME_PATTERN = re.compile(
    r'\b(?:(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)\s*(?P<meridiem>[ap]\.?m\.?)?'
    r'|(?P<hour12>1[0-2]|0?[1-9])\s*(?P<meridiem12>[ap]\.?m\.?)'
    r'|(?P<word>midday|noon|midnight))(?![\w])',
    re.I,
)

# Wording that classifies a date. The closest phrase before the date wins and a longer
# phrase wins a tie, so "late submission" beats "submission".
DATE_LABELS = (
    (
        'submission',
        r'submission\s+(?:deadline|date)|submission|submit(?:ted)?(?:\s+\w+){0,4}\s+(?:by|before|on)'
        r'|deadline|due(?:\s+date)?(?!\s+to\b)|hand(?:ed)?[\s-]+in|closing\s+date|upload(?:ed)?(?:\s+\w+){0,4}\s+by',
    ),
    (
        'presentation',
        r'presentations?|viva(?:\s+voce)?|demonstrations?|demos?|pitch(?:es)?|interviews?',
    ),
    ('late_submission', r'late\s+submissions?|extensions?|penalt(?:y|ies)|grace\s+period'),
    (
        'feedback',
        r'feedback|results?|grades?\s+(?:will\s+be\s+)?(?:released|returned)|marks?\s+(?:will\s+be\s+)?returned',
    ),
    (
        'release',
        r'release(?:d)?(?:\s+date)?|issue(?:d)?(?:\s+date)?|published|date\s+of\s+issue|handed\s+out|available\s+from',
    ),
    ('exam', r'exam(?:ination)?s?|class\s+tests?|in-class\s+tests?|quiz(?:zes)?'),
)
LABEL_PATTERNS = [(name, re.compile(rf'\b(?:{pattern})\b', re.I)) for name, pattern in DATE_LABELS]

FIELD_LABEL = r'\s*(?:[:\-–—]|\t)\s*'
TITLE_PATTERNS = (
    re.compile(
        rf'^(?:assessment|assignment|project|coursework)\s+(?:title|name){FIELD_LABEL}(?P<value>.+)$',
        re.I,
    ),
    re.compile(rf'^title{FIELD_LABEL}(?P<value>.+)$', re.I),
)
TITLE_FALLBACK = re.compile(
    r'^(?P<value>(?:CA|continuous\s+assessment|assignment|assessment|project|coursework)\s*\d{1,2}\b.{0,120})$',
    re.I,
)
MODULE_NAME = re.compile(
    rf'^(?:module|subject|course)(?:\s+(?:title|name))?{FIELD_LABEL}(?P<value>.+)$', re.I
)
MODULE_CODE_LABELLED = re.compile(
    rf'\b(?:module|subject|course)\s+code{FIELD_LABEL}(?P<value>[A-Z]{{2,6}}[\s-]?[A-Z]?\d{{3,5}}[A-Z]?)\b',
    re.I,
)
MODULE_CODE_INLINE = re.compile(r'\b(?P<value>[A-Z]{2,6}\d{3,5}[A-Z]?)\b')

PERCENT = r'(?P<value>\d{1,3}(?:[.,]\d+)?)\s*%'
WEIGHT_PATTERNS = (
    (
        'high',
        re.compile(
            rf'\b(?:assessment\s+)?weight(?:ing)?(?:{FIELD_LABEL}|\s+(?:is|of)\s+|\s+){PERCENT}',
            re.I,
        ),
    ),
    (
        'high',
        re.compile(
            rf'{PERCENT}\s+(?:of|towards)\s+(?:the\s+)?(?:overall\s+|final\s+|total\s+)?'
            r'(?:module|course|final|overall)\s*(?:grade|mark|result|assessment)?\b',
            re.I,
        ),
    ),
    ('medium', re.compile(rf'\bworth\s+{PERCENT}', re.I)),
)
WORD_COUNT_PATTERNS = (
    re.compile(
        r'\b(?:word\s+(?:count|limit)|length)\s*(?:[:\-–]|is|of)?\s*(?:approx(?:\.|imately)?\s+|max(?:imum)?(?:\s+of)?\s+|up\s+to\s+|between\s+)?'
        r'(?P<low>\d{1,2}(?:,\d{3})|\d{3,5})(?:\s*(?:-|–|to|and)\s*(?P<high>\d{1,2}(?:,\d{3})|\d{3,5}))?(?:\s*words?)?',
        re.I,
    ),
    re.compile(
        r'\b(?P<low>\d{1,2}(?:,\d{3})|\d{3,5})(?:\s*(?:-|–|to)\s*(?P<high>\d{1,2}(?:,\d{3})|\d{3,5}))?\s*(?:-\s*)?words?\b',
        re.I,
    ),
)

SECTION_HEADINGS = {
    'deliverables': re.compile(
        r'^(?:deliverables?|what\s+(?:to|you\s+(?:must|should|need\s+to))\s+submit|submission\s+(?:requirements|contents|checklist)|you\s+must\s+submit|required\s+submissions?)\b',
        re.I,
    ),
    'marking_criteria': re.compile(
        r'^(?:marking\s+(?:criteria|scheme|rubric|breakdown|guide)|assessment\s+criteria|grading\s+(?:criteria|rubric)|rubric|mark\s+allocation|marks?\s+breakdown)\b',
        re.I,
    ),
    'instructions': re.compile(
        r'^(?:instructions?|important(?:\s+(?:information|notes?))?|notes?|guidelines?|submission\s+instructions|academic\s+integrity|plagiarism|general\s+information)\b',
        re.I,
    ),
}
BULLET = re.compile(r'^\s*(?:[-*•▪◦●·]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+', re.I)
INSTRUCTION_LINE = re.compile(r'^(?:important|note|n\.b\.?|please\s+note)\b\s*[:\-–]?', re.I)


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    """Collapse whitespace and trim text to ``limit`` characters."""
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + '…'


def _field(value: Any, source: str, confidence: str, **extra: Any) -> dict[str, Any]:
    """Build a field object: value, source line and confidence."""
    return {
        'value': value,
        'source_text': _clip(source, MAX_SOURCE_CHARS),
        'confidence': confidence,
        **extra,
    }


def _line_at(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the start and end offsets of the line containing a span."""
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    return line_start, len(text) if line_end == -1 else line_end


def _lines(text: str) -> list[str]:
    """Split text into stripped lines."""
    return [line.strip() for line in text.splitlines()]


def _year(value: str | None) -> tuple[int | None, bool]:
    """Return the year and whether a two-digit year had to be expanded."""
    if not value:
        return None, False
    if len(value) == 2:
        return 2000 + int(value), True
    return int(value), False


def find_dates(text: str) -> list[dict[str, Any]]:
    """Find explicit calendar dates, read day-first, without guessing missing years."""
    found = []
    for kind, pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            month_text = match['month']
            month = int(month_text) if month_text.isdigit() else MONTHS[month_text[:3].lower()]
            day = int(match['day'])
            year, expanded = _year(match['year'])
            confidence = 'high'
            notes = []
            if kind == 'numeric' and day <= 12 and month <= 12 and day != month:
                # 03/04/2026 is 3 April in Ireland but 4 March in the United States.
                confidence = 'medium'
                notes.append('Numeric date read as day/month/year')
            if expanded:
                confidence = 'medium'
                notes.append('Two-digit year expanded to 20xx')
            value = None
            if year is None:
                confidence = 'low'
                notes.append('The year is not stated')
            else:
                try:
                    value = date(year, month, day)
                except ValueError:
                    continue
            weekday = match.groupdict().get('weekday')
            if value and weekday and not WEEKDAYS[value.weekday()].startswith(weekday.lower()[:3]):
                confidence = 'low'
                notes.append('The weekday does not match the date')
            found.append(
                dict(
                    start=match.start(),
                    end=match.end(),
                    text=match[0],
                    value=value.isoformat() if value else None,
                    day=day,
                    month=month,
                    confidence=confidence,
                    notes=notes,
                )
            )
    # Keep the longest match where patterns overlap, for example a weekday prefix.
    found.sort(key=lambda item: (item['start'], -(item['end'] - item['start'])))
    result: list[dict[str, Any]] = []
    for item in found:
        if result and item['start'] < result[-1]['end']:
            continue
        result.append(item)
    return result


def _time_value(match: re.Match[str]) -> str:
    """Convert a matched time (17.00, 5pm, noon) to 24-hour HH:MM."""
    if match['word']:
        return '00:00' if match['word'].lower() == 'midnight' else '12:00'
    hour = int(match['hour'] or match['hour12'])
    minute = int(match['minute'] or 0)
    meridiem = (match['meridiem'] or match['meridiem12'] or '').lower().replace('.', '')
    if meridiem == 'pm' and hour < 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def _nearby_time(text: str, item: dict[str, Any], dates: list[dict[str, Any]]) -> str | None:
    """Find a time written on the same line as a date, within a short distance."""
    line_start, line_end = _line_at(text, item['start'], item['end'])
    spans = [(other['start'], other['end']) for other in dates]
    best = None
    for match in TIME_PATTERN.finditer(
        text, max(line_start, item['start'] - 40), min(line_end, item['end'] + 40)
    ):
        if any(start <= match.start() < end for start, end in spans):
            continue
        distance = (
            match.start() - item['end']
            if match.start() >= item['end']
            else item['start'] - match.end()
        )
        if best is None or distance < best[0]:
            best = (distance, _time_value(match))
    return best[1] if best else None


def _classify(text: str, item: dict[str, Any]) -> str:
    """Label a date using the closest classifying phrase before it on the same line."""
    line_start, line_end = _line_at(text, item['start'], item['end'])
    before_start = max(line_start, item['start'] - 120)
    context = text[before_start : item['start']]
    if not context.strip() and line_start > 0:
        # A table or form often puts the label on the line above the value.
        previous_start = text.rfind('\n', 0, line_start - 1) + 1
        context = text[previous_start:line_start]
    best = None
    for name, pattern in LABEL_PATTERNS:
        for match in pattern.finditer(context):
            rank = (match.end(), match.end() - match.start())
            if best is None or rank > best[0]:
                best = (rank, name)
    if best:
        return best[1]
    after = text[item['end'] : min(line_end, item['end'] + 60)]
    for name, pattern in LABEL_PATTERNS:
        if name != 'submission' and pattern.search(after):
            return name
    return 'other'


def _labelled_value(lines: list[str], patterns) -> tuple[str, str] | None:
    """Return the first ``Label: value`` match and its line."""
    for line in lines:
        for pattern in patterns:
            match = pattern.match(line)
            if match and match['value'].strip():
                return match['value'].strip(' .'), line
    return None


def _module_code(text: str, module_name: str | None) -> dict[str, Any] | None:
    """Find a labelled module code, or one at the start of the module name."""
    match = MODULE_CODE_LABELLED.search(text)
    if match:
        line = text[slice(*_line_at(text, match.start(), match.end()))]
        return _field(re.sub(r'\s+', '', match['value']).upper(), line, 'high')
    for candidate in filter(None, (module_name,)):
        inline = MODULE_CODE_INLINE.search(candidate)
        if inline:
            return _field(inline['value'], candidate, 'medium')
    return None


def _weighting(text: str) -> dict[str, Any] | None:
    """Return the assessment weighting; several values lower the confidence."""
    found: dict[str, tuple[str, str]] = {}
    for confidence, pattern in WEIGHT_PATTERNS:
        for match in pattern.finditer(text):
            number = float(match['value'].replace(',', '.'))
            if 0 < number <= 100:
                line = text[slice(*_line_at(text, match.start(), match.end()))]
                found.setdefault(f'{number:g}%', (line, confidence))
    if len(found) == 1:
        value, (line, confidence) = next(iter(found.items()))
        return _field(value, line, confidence)
    if len(found) > 1:
        # Several percentages usually describe criteria, not the assessment's weighting.
        value, (line, _) = next(iter(found.items()))
        return _field(value, line, 'low', alternatives=sorted(found))
    return None


def _word_count(text: str) -> dict[str, Any] | None:
    """Return a word limit or range with its minimum and maximum."""
    for index, pattern in enumerate(WORD_COUNT_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        low = int(match['low'].replace(',', ''))
        high = int(match['high'].replace(',', '')) if match['high'] else None
        if low < 50 or (high is not None and high < low):
            continue
        line = text[slice(*_line_at(text, match.start(), match.end()))]
        value = f'{low:,}–{high:,} words' if high else f'{low:,} words'
        return _field(
            value,
            line,
            'high' if index == 0 else 'medium',
            minimum=low if high else None,
            maximum=high or low,
        )
    return None


def _sections(lines: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Collect bullet or short lines under recognised headings such as 'Deliverables'."""
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_HEADINGS}
    current = None
    for line in lines:
        if not line:
            continue
        heading = next(
            (name for name, pattern in SECTION_HEADINGS.items() if pattern.match(line)), None
        )
        if heading and len(line) <= 80:
            current = heading
            remainder = re.split(r'[:\-–]\s*', line, maxsplit=1)
            if len(remainder) == 2 and len(remainder[1]) > 3:
                result[current].append(_field(_clip(remainder[1]), line, 'medium'))
            continue
        if current is None:
            continue
        is_bullet = bool(BULLET.match(line))
        looks_like_heading = len(line) <= 60 and line.endswith(':') and not is_bullet
        if looks_like_heading:
            current = None
            continue
        if len(result[current]) < MAX_LIST_ITEMS:
            item = BULLET.sub('', line)
            result[current].append(_field(_clip(item), line, 'high' if is_bullet else 'medium'))
    for line in lines:
        if INSTRUCTION_LINE.match(line) and len(result['instructions']) < MAX_LIST_ITEMS:
            item = _field(_clip(INSTRUCTION_LINE.sub('', line).strip() or line), line, 'medium')
            if item['value'] not in {existing['value'] for existing in result['instructions']}:
                result['instructions'].append(item)
    return result


def _date_field(
    text: str, item: dict[str, Any], dates: list[dict[str, Any]], label: str
) -> dict[str, Any]:
    """Build a date field with its time, matched text, label and notes."""
    line = text[slice(*_line_at(text, item['start'], item['end']))]
    return _field(
        item['value'],
        line,
        item['confidence'],
        time=_nearby_time(text, item, dates),
        matched_text=item['text'],
        label=label,
        notes=item['notes'],
    )


def extract_assessment(text: str) -> dict[str, Any]:
    """Return the assessment data model for one document's text."""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = _lines(text)
    warnings: list[str] = []

    title = _labelled_value(lines, TITLE_PATTERNS)
    title_field = _field(title[0], title[1], 'high') if title else None
    if title_field is None:
        fallback = _labelled_value(lines[:40], (TITLE_FALLBACK,))
        title_field = _field(fallback[0], fallback[1], 'medium') if fallback else None

    module = _labelled_value(lines, (MODULE_NAME,))
    module_name = module[0] if module else None
    if module_name:
        # "Module: COMP1234 Software Design" carries the code inside the name.
        code_inside = MODULE_CODE_INLINE.match(module_name)
        if code_inside:
            stripped = module_name[code_inside.end() :].strip(' :-–—')
            module_name = stripped or module_name
    module_field = _field(module_name, module[1], 'high') if module else None
    code_field = _module_code(text, module[0] if module else None)

    dates = find_dates(text)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in dates:
        label = _classify(text, item)
        grouped.setdefault(label, []).append(_date_field(text, item, dates, label))

    submissions = grouped.pop('submission', [])
    distinct = {
        (entry['value'], entry['matched_text'] if entry['value'] is None else '')
        for entry in submissions
    }
    deadline = None
    if len(distinct) == 1:
        deadline = submissions[0]
        if deadline['value'] is None:
            warnings.append('The submission date has no year. Enter the full date.')
    elif len(distinct) > 1:
        warnings.append('Several possible submission dates were found. Choose the correct one.')
    elif dates:
        warnings.append('Dates were found, but none is labelled as the submission deadline.')
    else:
        warnings.append('No submission date was found in the document.')
    times = {entry['time'] for entry in submissions if entry['time']}
    submission_time = None
    if deadline and deadline['time']:
        submission_time = _field(deadline['time'], deadline['source_text'], deadline['confidence'])
    elif len(times) > 1:
        warnings.append('Several submission times were found.')

    sections = _sections(lines)
    needs_confirmation = not (
        deadline and deadline['value'] and deadline['confidence'] == 'high' and len(distinct) == 1
    )
    return {
        'schema_version': SCHEMA_VERSION,
        'title': title_field,
        'module_name': module_field,
        'module_code': code_field,
        'weighting': _weighting(text),
        'submission_deadline': deadline,
        'submission_time': submission_time,
        'deadline_candidates': submissions,
        'presentation_dates': grouped.pop('presentation', []),
        'other_dates': [entry for entries in grouped.values() for entry in entries],
        'word_count': _word_count(text),
        'deliverables': sections['deliverables'],
        'marking_criteria': sections['marking_criteria'],
        'instructions': sections['instructions'],
        'needs_confirmation': needs_confirmation,
        'warnings': warnings,
    }

"""Find explicit deadlines in email text, conservatively.

Only dates next to deadline wording become tasks, and every imported email deadline
is marked for review."""

import re
from urllib.parse import urlsplit

import dateparser
from bs4 import BeautifulSoup

DEADLINE = re.compile(
    r'\b(?:due(?: date)?|deadline|submit(?:ted)? by|submission date)\s*(?:is|on|:|by)?\s*('
    r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
    r'|\d{4}-\d{2}-\d{2}'
    r'|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+(?:\s+\d{4})?'
    r'|[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)',
    re.IGNORECASE,
)
DATE_SETTINGS = {'DATE_ORDER': 'DMY', 'PREFER_DATES_FROM': 'future'}


def html_text(body):
    """Visible text of an HTML email, ignoring scripts, styles and hidden parts."""
    if '<' not in body and '&' not in body:
        return body.strip()
    soup = BeautifulSoup(body, 'html.parser')
    for hidden in soup.select('head, script, style, template, noscript, [hidden]'):
        hidden.decompose()
    return soup.get_text(' ', strip=True)


def safe_http_link(link):
    """Return the link only if it is a plain http(s) URL without credentials."""
    if not isinstance(link, str) or re.search(r'[\s\x00-\x1f\x7f\\]', link):
        return ''
    try:
        target = urlsplit(link)
        if target.scheme in ('http', 'https') and target.hostname and target.username is None:
            return link
    except ValueError:
        pass
    return ''


def email_rows(key, subject, body, link, *, body_is_html=True):
    """Turn one email into reviewable deadline rows, one per distinct date."""
    # Only explicit dates near deadline wording become reviewable tasks.
    text = html_text(body) if body_is_html else body
    link = safe_http_link(link)
    rows = []
    seen = set()
    index = 0
    for match in DEADLINE.finditer(f'{subject} {text}'):
        parsed = dateparser.parse(match[1], settings=DATE_SETTINGS)
        if not parsed:
            continue
        due = parsed.date().isoformat()
        if due not in seen:
            rows.append(
                dict(
                    key=f'{key}:{index}',
                    identity=f'message:{key}:{due}',
                    title=subject or 'Email deadline',
                    due=due,
                    notes=match[0],
                    link=link,
                    review=1,
                )
            )
            seen.add(due)
        index += 1
    return rows

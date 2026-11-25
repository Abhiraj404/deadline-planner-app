"""URL rules for Moodle: origins, activity IDs, file identities and links."""

import hashlib
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

ASSIGN = re.compile(r'/mod/assign/view\.php$')
COURSE = re.compile(r'/course/view\.php$')
ACTIVITY = re.compile(r'/mod/(assign|resource|folder|page|url)/view\.php$')
SECTION = re.compile(r'/course/section\.php$')
MAIN_CONTENT = '#region-main, main, [role="main"]'
INTRO = '#intro, .activity-description, .mod_introbox'
CA = re.compile(
    r'\b(?:CA\s*\d*|assessment|assignment|coursework|project brief|marking rubric)\b', re.I
)


def origin(url):
    """The (scheme, host, port) of an http(s) URL, or None if unsafe or invalid."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None:
            return (
                parsed.scheme,
                parsed.hostname,
                port if port is not None else {'https': 443, 'http': 80}[parsed.scheme],
            )
    except ValueError:
        pass
    return None


def activity_id(url, base, pattern):
    """The numeric ID in a Moodle URL that matches ``pattern``, or None."""
    try:
        url = urljoin(base.rstrip('/') + '/', url or '')
        parsed = urlsplit(url)
    except ValueError:
        return None
    values = parse_qs(parsed.query).get('id', [])
    site = origin(url)
    if (
        site
        and site == origin(base)
        and pattern.search(parsed.path)
        and values
        and re.fullmatch(r'[0-9]+', values[0])
    ):
        return values[0]
    return None


def calendar_link(url):
    """Keep a calendar event link only if it is a safe web address."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ''
    if parsed.username is not None or parsed.scheme not in ('', 'http', 'https'):
        return ''
    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query)
        if (key in ('id', 'time', 'course') and value.isdecimal())
        or (key == 'view' and value in ('day', 'month', 'upcoming'))
    ]
    return parsed._replace(query=urlencode(params), fragment='').geturl()


def file_identity(url):
    """Preserve the file's existing stable identifier without storing its URL."""
    parsed = urlsplit(url)
    file_path = parse_qs(parsed.query).get('file', [''])[0]
    identity = parsed._replace(query='', fragment='').geturl() + '\n' + file_path
    return hashlib.sha256(identity.encode()).hexdigest()


def word_url(url):
    """True when a URL points at a .docx file."""
    parsed = urlsplit(url)
    name = parse_qs(parsed.query).get('file', [parsed.path])[0]
    return name.lower().endswith('.docx')

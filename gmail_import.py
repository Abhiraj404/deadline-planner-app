"""Read-only Gmail import of recent messages that mention a deadline."""

import base64
import binascii
from urllib.parse import quote

from email_deadlines import email_rows, html_text
from local_files import write_atomic
from source_http import REQUEST_TIMEOUT

GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
GMAIL_QUERY = 'newer_than:90d {deadline "due date" "submit by" submission}'


def message_body(part):
    """Collect the text of a message's parts, skipping attachments."""
    if part.get('filename'):
        return ''
    children = part.get('parts')
    if children:
        return ' '.join(message_body(child) for child in children)
    mime = part.get('mimeType')
    if mime not in ('text/plain', 'text/html'):
        return ''
    encoded = (part.get('body') or {}).get('data') or ''
    try:
        data = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
    except (binascii.Error, ValueError):
        return ''
    text = data.decode('utf-8', errors='replace')
    return html_text(text) if mime == 'text/html' else text


def collect_gmail(token_path, job=None):
    """Import Gmail deadlines, or return None when Gmail is not connected."""
    if not token_path.exists():
        return None

    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp, Request
    from googleapiclient.discovery import build
    from httplib2 import Http

    credentials = Credentials.from_authorized_user_file(str(token_path), [GMAIL_SCOPE])
    http = Http(timeout=REQUEST_TIMEOUT)
    try:
        if job:
            job.check()
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request(http))
            write_atomic(token_path, credentials.to_json().encode('utf-8'))
        service = build('gmail', 'v1', http=AuthorizedHttp(credentials, http=http))
        return read_messages(service.users().messages(), job)
    finally:
        http.close()


def read_messages(messages, job):
    """Page through matching messages and extract their deadline rows."""
    rows = []
    token = None
    seen_tokens = set()
    while True:
        if job:
            job.check()
        page = messages.list(userId='me', q=GMAIL_QUERY, pageToken=token).execute(num_retries=2)
        for message in page.get('messages', []):
            if job:
                job.check()
            key = message['id']
            result = messages.get(userId='me', id=key, format='full').execute(num_retries=2)
            payload = result.get('payload') or {}
            subject = next(
                (
                    header.get('value', '')
                    for header in payload.get('headers', [])
                    if header.get('name', '').lower() == 'subject'
                ),
                '',
            )
            link = 'https://mail.google.com/mail/u/0/#all/' + quote(key, safe='')
            rows.extend(email_rows(key, subject, message_body(payload), link, body_is_html=False))
        token = page.get('nextPageToken')
        if not token:
            return rows
        if token in seen_tokens:
            raise RuntimeError('Gmail repeated a page token')
        seen_tokens.add(token)

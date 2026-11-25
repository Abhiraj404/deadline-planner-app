"""Read-only Outlook import through Microsoft Graph."""

import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from email_deadlines import email_rows
from local_files import write_atomic
from source_http import get_with_retries

OUTLOOK_SCOPE = 'Mail.Read'
GRAPH_MESSAGES = 'https://graph.microsoft.com/v1.0/me/messages'


def graph_page_url(url):
    """Accept a next-page link only if it points at Microsoft Graph."""
    try:
        target = urlsplit(url if isinstance(url, str) else '')
    except ValueError:
        target = None
    if (
        target is None
        or target.scheme != 'https'
        or target.fragment
        or target.netloc.lower() not in ('graph.microsoft.com', 'graph.microsoft.com:443')
    ):
        raise RuntimeError('Unexpected Outlook page URL')
    return url


def collect_outlook(cache_path, get, job=None):
    """Import Outlook deadlines, or return None when Outlook is not connected."""
    if not os.getenv('OUTLOOK_CLIENT_ID') or not cache_path.exists():
        return None

    from auth import outlook_app

    if job:
        job.check()
    app, cache = outlook_app(cache_path)
    accounts = app.get_accounts()
    result = app.acquire_token_silent([OUTLOOK_SCOPE], account=accounts[0]) if accounts else None
    if cache.has_state_changed:
        write_atomic(cache_path, cache.serialize().encode('utf-8'))
    if not result or 'access_token' not in result:
        raise RuntimeError('Reauthorize Outlook with auth.py outlook')

    return read_messages(get, result['access_token'], job)


def read_messages(get, access_token, job):
    """Page through the last 90 days of mail and extract deadline rows."""
    earliest = datetime.now(UTC) - timedelta(days=90)
    headers = {'Authorization': f'Bearer {access_token}'}
    url = GRAPH_MESSAGES
    params = {
        '$select': 'id,subject,body,webLink,receivedDateTime',
        '$filter': f'receivedDateTime ge {earliest:%Y-%m-%dT%H:%M:%SZ}',
        '$top': 50,
    }
    rows = []
    seen_pages = set()
    while url:
        if url in seen_pages:
            raise RuntimeError('Outlook repeated a page URL')
        seen_pages.add(url)
        response = get_with_retries(
            get, url, headers=headers, params=params, allow_redirects=False, job=job
        )
        with closing(response):
            if response.status_code in range(300, 400):
                raise RuntimeError('Outlook returned an unexpected redirect')
            page = response.json()
        for message in page.get('value', []):
            if job:
                job.check()
            body = message.get('body') or {}
            content_type = (body.get('contentType') or 'html').lower()
            rows.extend(
                email_rows(
                    message['id'],
                    message.get('subject') or '',
                    body.get('content') or '',
                    message.get('webLink'),
                    body_is_html=content_type != 'text',
                )
            )
        next_page = page.get('@odata.nextLink')
        url = graph_page_url(next_page) if next_page else None
        params = None
    return rows

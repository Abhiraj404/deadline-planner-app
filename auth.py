"""Interactive, one-off authorisation for Gmail, Outlook and Moodle.

Run ``python auth.py moodle|gmail|outlook``. Tokens and sessions are written to the
data folder and never leave this computer."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from configuration import ROOT, load_environment
from gmail_import import GMAIL_SCOPE
from local_files import write_atomic
from outlook_import import OUTLOOK_SCOPE
from source_http import REQUEST_TIMEOUT


class AuthorizationError(RuntimeError):
    """An authorization failure safe to display in the terminal."""


def outlook_app(cache_path=None):
    """Create the MSAL public client and its token cache for Outlook."""
    import msal

    cache = msal.SerializableTokenCache()
    path = Path(cache_path) if cache_path is not None else Path('outlook-cache.json')
    if path.exists():
        cache.deserialize(path.read_text(encoding='utf-8'))
    tenant = os.getenv('OUTLOOK_TENANT', 'common')
    app = msal.PublicClientApplication(
        os.environ['OUTLOOK_CLIENT_ID'],
        authority=f'https://login.microsoftonline.com/{tenant}',
        token_cache=cache,
        timeout=REQUEST_TIMEOUT,
    )
    return app, cache


def authorize_gmail(root):
    """Run Google's desktop OAuth flow and save a read-only Gmail token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(root / 'credentials.json'), [GMAIL_SCOPE])
    credentials = flow.run_local_server(port=0)
    write_atomic(root / 'gmail-token.json', credentials.to_json().encode('utf-8'))


def authorize_outlook(root):
    """Run Microsoft's device sign-in flow and save the token cache."""
    path = root / 'outlook-cache.json'
    app, cache = outlook_app(path)
    flow = app.initiate_device_flow(scopes=[OUTLOOK_SCOPE])
    if not flow.get('message') or not flow.get('user_code'):
        raise AuthorizationError('Could not start Outlook authorization')
    print(flow['message'])
    result = app.acquire_token_by_device_flow(flow)
    if 'access_token' not in result:
        raise AuthorizationError('Outlook authorization failed')
    write_atomic(path, cache.serialize().encode('utf-8'))


def moodle_signed_in(tab, base):
    """True when a browser tab shows a signed-in page on the Moodle site."""
    if tab.is_closed():
        return False
    url = urlsplit(tab.url)
    return (
        url.netloc.lower() == base.netloc.lower()
        and '/login/' not in url.path
        and tab.evaluate(
            '!!window.M?.cfg?.sesskey && !!document.querySelector('
            '".usermenu [data-toggle=dropdown], .usermenu .dropdown-toggle, a[href*=logout]")'
        )
    )


def authorize_moodle(root):
    """Open a browser for normal sign-in, then save the session if it is valid."""
    from playwright.sync_api import sync_playwright

    base = os.environ['MOODLE_URL']
    origin = urlsplit(base)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(base)
            input('Log in normally, leave your Moodle dashboard open, then press Enter here: ')
            if not any(moodle_signed_in(tab, origin) for tab in context.pages):
                raise AuthorizationError(
                    'Moodle sign-in could not be verified. The saved session was preserved.'
                )
            write_atomic(
                root / 'moodle-session.json', json.dumps(context.storage_state()).encode('utf-8')
            )
        finally:
            browser.close()


def main(argv=None):
    """Command-line entry point. Errors are shown by type, never with secrets."""
    providers = {'gmail': authorize_gmail, 'outlook': authorize_outlook, 'moodle': authorize_moodle}
    parser = argparse.ArgumentParser(description='Authorize a Deadline Planner import source.')
    parser.add_argument('provider', choices=providers)
    args = parser.parse_args(argv)
    try:
        load_environment(ROOT)
        providers[args.provider](ROOT)
    except AuthorizationError as error:
        parser.exit(1, f'{error}\n')
    except Exception as error:
        parser.exit(1, f'Authorization failed ({type(error).__name__})\n')


if __name__ == '__main__':
    main()

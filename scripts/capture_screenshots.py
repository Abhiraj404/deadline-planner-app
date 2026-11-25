"""Capture README screenshots from a running demo-mode server.

Start the demo first with ``.\\start.ps1 -Demo``, then run::

    .\\.venv\\Scripts\\python.exe scripts\\capture_screenshots.py

Only the synthetic demo data is shown. The script refuses to run against a server
that is not in demo mode, so personal data cannot end up in a screenshot.
"""

import json
import sys
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8001'
OUTPUT = Path(__file__).resolve().parent.parent / 'docs' / 'screenshots'


def main() -> int:
    """Capture each page of a running demo server into docs/screenshots."""
    with urlopen(f'{BASE}/api/app', timeout=5) as response:
        if not json.load(response).get('demo'):
            print('The server on port 8001 is not in demo mode. Aborting.', file=sys.stderr)
            return 1
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1200, 'height': 900}, color_scheme='dark')
        for name, path in (
            ('overview', '/'),
            ('assignments', '/assignments'),
            ('calendar', '/calendar'),
            ('setup', '/setup'),
        ):
            page.goto(BASE + path)
            page.wait_for_load_state('networkidle')
            page.screenshot(path=OUTPUT / f'{name}.png')
        page.goto(BASE + '/library')
        page.wait_for_load_state('networkidle')
        page.fill('#docsearch', 'portfolio')
        page.wait_for_timeout(600)
        page.click('summary:has-text("SQL Query Portfolio")')
        page.click('summary:has-text("Document preview")')
        page.wait_for_timeout(400)
        page.screenshot(path=OUTPUT / 'library.png')
        page.click('button:has-text("Review deadline details")')
        page.wait_for_timeout(400)
        page.locator('#review').screenshot(path=OUTPUT / 'review.png')
        browser.close()
    print(f'Saved screenshots to {OUTPUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

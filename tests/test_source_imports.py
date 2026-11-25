"""Gmail and Outlook imports with mocked APIs."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import sources


class SourceImportTests(unittest.TestCase):
    """Email parsing, paging, retries and token cache handling."""

    def test_email_rows_use_visible_text_and_safe_links(self):
        rows = sources.email_rows(
            'message-id',
            'Assignment',
            '<p hidden>Due 1 June 2026</p><p>Deadline: 12 June 2026</p>',
            'javascript:alert(1)',
        )

        self.assertEqual([(row['due'], row['link']) for row in rows], [('2026-06-12', '')])

    def test_outlook_does_not_send_credentials_to_invalid_next_page(self):
        response = Mock()
        response.json.return_value = {'@odata.nextLink': 'https://example.test/private-page'}
        get = Mock(side_effect=[response, Mock(**{'json.return_value': {}})])

        with self.outlook_mocks(get), self.assertRaises(RuntimeError):
            sources.collect('Outlook')

        self.assertEqual(get.call_count, 1)

    def test_outlook_retries_transient_response_then_follows_next_page(self):
        busy = requests.Response()
        busy.status_code = 503
        busy.headers['Retry-After'] = '0'
        busy._content = b''
        busy._content_consumed = True
        next_page = 'https://graph.microsoft.com/v1.0/me/messages?%24skip=50'
        first = Mock(**{'json.return_value': {'@odata.nextLink': next_page}})
        second = Mock(
            **{'json.return_value': {'value': [{'id': 'm', 'subject': 'Due 12 June 2026'}]}}
        )
        get = Mock(side_effect=[busy, first, second])

        with self.outlook_mocks(get):
            (row,) = sources.collect('Outlook')

        self.assertEqual(row['identity'], 'message:m:2026-06-12')
        self.assertEqual(get.call_args.args[0], next_page)
        self.assertIsNone(get.call_args.kwargs['params'])

    def test_gmail_ignores_malformed_body_part_and_imports_subject(self):
        api = Mock()
        messages = api.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {'messages': [{'id': 'm'}]}
        messages.get.return_value.execute.return_value = {
            'payload': {
                'headers': [{'name': 'Subject', 'value': 'Deadline 12 June 2026'}],
                'mimeType': 'text/plain',
                'body': {'data': 'x'},
            }
        }
        with (
            patch.object(sources.Path, 'exists', return_value=True),
            patch(
                'google.oauth2.credentials.Credentials.from_authorized_user_file',
                return_value=SimpleNamespace(expired=False),
            ),
            patch('googleapiclient.discovery.build', return_value=api),
        ):
            (row,) = sources.collect('Gmail')

        self.assertEqual(row['identity'], 'message:m:2026-06-12')

    def test_outlook_updates_only_the_configured_cache(self):
        account = SimpleNamespace(get_accounts=lambda: [])
        cache = SimpleNamespace(has_state_changed=True, serialize=lambda: 'updated')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / 'outlook-cache.json'
            cache_path.write_text('previous', encoding='utf-8')
            with (
                patch.dict('os.environ', {'OUTLOOK_CLIENT_ID': 'mock'}),
                patch('auth.outlook_app', return_value=(account, cache)) as outlook_app,
                self.assertRaisesRegex(RuntimeError, 'Reauthorize Outlook'),
            ):
                sources.collect('Outlook', root=root)

            outlook_app.assert_called_once_with(cache_path)
            self.assertEqual(cache_path.read_text(encoding='utf-8'), 'updated')
            self.assertEqual(list(root.iterdir()), [cache_path])

    def outlook_mocks(self, get):
        """Patch MSAL and the token cache so no real account is used."""
        account = SimpleNamespace(
            get_accounts=lambda: ['account'],
            acquire_token_silent=lambda *args, **kwargs: {'access_token': 'mock'},
        )
        stack = ExitStack()
        stack.enter_context(patch.dict('os.environ', {'OUTLOOK_CLIENT_ID': 'mock'}))
        stack.enter_context(patch.object(sources.Path, 'exists', return_value=True))
        stack.enter_context(
            patch(
                'auth.outlook_app', return_value=(account, SimpleNamespace(has_state_changed=False))
            )
        )
        stack.enter_context(patch('sources.requests.Session.get', get))
        return stack

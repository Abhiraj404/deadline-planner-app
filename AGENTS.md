# Guidance for coding agents and contributors

Read this before changing the code. Current progress is in `DEVELOPMENT_STATUS.md`.

## Product boundaries

- Single user, local-first, self-hosted on Windows. The server binds to `127.0.0.1`.
- Do **not** add user registration, multi-user accounts, admin roles, cloud hosting,
  subscriptions, external database servers, microservices, mandatory Docker or CSV
  export. Keep the stack: Python, FastAPI, SQLite, plain HTML/CSS/JS, Playwright.
- Every integration (Moodle, Outlook, Gmail) is optional. The app must stay useful with
  none of them and must not require an AI API key.

## Privacy rules

- Never commit or print the contents of `.env`, `credentials.json`, `*-token.json`,
  `*-cache.json`, `*-session.json`, `*.db` or anything in `data/` (the personal data
  folder) or `demo-data/`. `.gitignore` covers them; keep it that way.
- Use synthetic data in tests, fixtures, screenshots and docs. `demo_data.py` and
  `synthetic_documents.py` generate it. Use `.test`, `.invalid` or `example.*` domains.
- Log exception types, not messages: provider messages can contain private URLs.
- Treat documents, Moodle HTML and email bodies as untrusted. Escape all output.

## Layout and conventions

- Application modules sit in the project folder, tests in `tests/`, developer tools in
  `scripts/`, and personal data in `data/` (`data_location.py`).
- Routes live in `app.py`; SQL lives in `database.py`, `listing.py`, `search_index.py`
  and `data_management.py`. Keep request values out of SQL text: use parameters.
- `assessment_extraction.py` must stay pure (no I/O, standard library `re` and
  `datetime` only) so it can be ported to JavaScript. Change
  `tests/fixtures/extraction_cases.json` and `docs/assessment-data-model.md` with it.
- Document bytes go through `document_processing.process_document`.
- Writers of the document index must hold the operation lock (`jobs.acquire()`).
- Python: Ruff (`ruff check`, `ruff format`, single quotes, 100 columns), type hints on
  new functions, short docstrings that explain *why*. JavaScript: ES modules, no
  framework, no build step, ESLint and Prettier.

## Checks before finishing

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
npm run lint; npm run format:check
```

If the sandbox cannot write to `.pytest_cache` or the system temp folder, add
`-p no:cacheprovider --basetemp <writable folder>` to the pytest command.

## Releases

Bump `VERSION`, update `CHANGELOG.md` and `DEVELOPMENT_STATUS.md`, then run
`scripts\New-SourceRelease.ps1` and follow *Publishing* in the README. Never push,
force-push, rewrite history or change repository visibility without the owner's
explicit approval.

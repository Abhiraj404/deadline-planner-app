# Changelog

All notable changes are recorded here. The project uses
[Semantic Versioning](https://semver.org/) and this format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-11-25

First public release as a single-user, self-hosted application.

### Added

- Manual PDF and DOCX import from the document library, with no account required.
- `assessment_extraction.py`: rule-based extraction of title, module name and code,
  weighting, submission date and time, presentation dates, word count, deliverables,
  marking criteria and instructions, with source text and confidence for every value.
  Day-first Irish and European dates, no invented years, and mandatory confirmation
  for ambiguous deadlines. Documented JSON data model, JSON Schema and shared
  synthetic test cases for a future JavaScript port.
- Review panel to confirm extracted details before a deadline is saved, linking the
  deadline to its source document.
- SQLite FTS5 full-text search for deadlines and documents with highlighted excerpts,
  maintained by triggers, with automatic substring fallback.
- Structured Word previews (headings, lists, tables, headers and footers).
- Extraction completeness indicators and optional local OCR (`PLANNER_DOCUMENT_OCR`).
- Reminders with configurable days, overdue items and optional browser notifications.
- First-run Setup page for Moodle, Outlook, Gmail, timezone, default sync range and
  reminders. Integration secrets are never returned by the API.
- Demo mode with synthetic modules, assignments, emails and generated briefs in an
  isolated `demo-data/` folder.
- `setup.ps1`, `start.ps1` and `stop.ps1` for Windows; `run_server.py` single-instance
  launcher bound to 127.0.0.1 with token-protected graceful shutdown.
- `scripts/New-SourceRelease.ps1` for clean, secret-scanned source releases.
- GitHub Actions workflow for Ruff, pytest, ESLint and Prettier.
- MIT licence, `VERSION`, release notes, Chrome extension plan and developer docs.

### Changed

- Personal data now lives in `data/` instead of the project folder. Earlier copies are
  moved there once on start (`data_location.py`), never overwriting files and never
  while a planner process is running. `PLANNER_DATA_DIR` still overrides the location.
- Tests moved to `tests/` (fixtures in `tests/fixtures/`); the benchmark moved to
  `scripts/benchmark.py`.
- The release script now uses an explicit allowlist, checks it against `.gitignore` and
  Git's view of the project, scans the built copy and verifies the zip contents.
- Release notes and publishing steps were merged into this changelog and the README.
- Document processing is independent of Moodle (`document_processing.py`,
  `document_store.py`); the Moodle scanner was split into focused modules.
- Benchmarks use synthetic data by default.
- `requirements.txt` declares compatible version ranges; lock files hold tested pins.

### Fixed

- Word previews failed with `SyntaxError: prefix 'http' not found` because element
  lookups used expat's `namespace tag` names instead of ElementTree's `{namespace}tag`.
- The data maintenance module imported Moodle code only to reach a file helper.

### Removed

- Unused helpers `extract_pdf_text`, `extract_word_text`, `pdf_path`,
  `environment_file_values` and unused constants.
- Compatibility re-exports in `moodle_details.py`; callers import from the module that
  defines each name.
- Three copies of the OS file-lock code, replaced by `local_files.try_lock`.
- `RELEASE_NOTES.md`, `docs/publishing.md` and `scripts/New-SourceZip.ps1`.
- The tracked legacy `assessment_index.json` placeholder (still read if present).

### Security

- `X-Frame-Options: DENY` on all responses; upload endpoint requires a document
  `Content-Type` and rejects cross-site requests; `.env` updates are whitelisted and
  reject control characters.
- Expanded `.gitignore` for all private runtime files.
- The public release is created from a clean, secret-scanned source folder with a new
  Git history, so no earlier development history is published.

## [0.x] - before 2026-11-25

Private development versions: Moodle calendar and scan, Gmail and Outlook imports,
assessment document library, pagination, data management and backups.

## Upgrading to 1.0.0

Stop the planner, back up your data, replace the source files and run `.\setup.ps1`.
The database gains a full-text index and a preferences table in place, and personal
files left in the project folder move into `data/`.

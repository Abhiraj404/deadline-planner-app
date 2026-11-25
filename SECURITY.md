# Security policy

Deadline Planner is a single-user application that each person runs on their own
computer. There is no hosted service, so there is no central server to attack, but a
bug in this code could still expose a user's local data.

## Reporting a vulnerability

Please report security problems privately through GitHub's **Report a vulnerability**
button on the repository's Security tab rather than in a public issue. Include steps to
reproduce with synthetic data only. Never include real credentials, calendar URLs,
session files or college documents in a report.

## Threat model in brief

- **In scope:** web pages in the user's browser trying to read or change the local
  planner (CSRF, DNS rebinding, cross-origin reads), malicious PDF or DOCX files,
  malicious content in Moodle pages or emails, path traversal through document IDs or
  file names, and private data leaking into logs, API responses or releases.
- **Out of scope:** other programs already running as the same Windows user. They can
  read the data folder directly, as with any local desktop application.

## Built-in protections

See the *Security and privacy* section of the README. Key points: loopback-only
binding and Host checking, Origin checks on writes, non-simple upload content types,
hash-named file storage, size and page limits, DTD rejection for Word XML, no macro
or embedded object execution, escaped rendering, and secrets never returned by the API.

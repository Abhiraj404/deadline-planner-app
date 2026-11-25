# Assessment data model (schema version 1)

`assessment_extraction.extract_assessment(text)` returns one JSON-compatible object.
The same shape is returned by `GET /api/assessment-documents/{id}/extraction` and
inside the response of `POST /api/assessment-documents/upload`. A machine-readable
JSON Schema is in [assessment.schema.json](assessment.schema.json).

The model is designed to be ported to JavaScript unchanged: it uses only strings,
numbers, booleans, `null`, arrays and objects. Dates are ISO `YYYY-MM-DD` strings and
times are 24-hour `HH:MM` strings. There are no time zones: a brief states local dates.

## Field

Every extracted value is wrapped in a *field* object, or is `null` when the text does
not state it. Nothing is guessed.

| Key | Type | Meaning |
| --- | --- | --- |
| `value` | string, number or `null` | The extracted value. `null` inside a field means the text mentioned it but the value is incomplete, for example a date without a year |
| `source_text` | string | The line the value came from, trimmed to 200 characters |
| `confidence` | `"high"`, `"medium"` or `"low"` | How explicit the source was (see below) |

## Date field

A field with these extra keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `time` | string or `null` | A time written on the same line, near the date |
| `matched_text` | string | The exact date text, for example `Friday 11 December 2026` |
| `label` | string | `submission`, `presentation`, `late_submission`, `feedback`, `release`, `exam` or `other` |
| `notes` | string[] | Why confidence was lowered, if it was |

## Top-level object

| Key | Type | Notes |
| --- | --- | --- |
| `schema_version` | `1` | Increment when the shape changes |
| `title` | field or `null` | `Assessment title:` labels are high confidence; a leading `CA1 …` line is medium |
| `module_name` | field or `null` | From `Module:` labels. A code at the start is moved to `module_code` |
| `module_code` | field or `null` | From `Module code:` (high) or the start of the module name (medium) |
| `weighting` | field or `null` | A single percentage. Several conflicting percentages give `low` confidence and an `alternatives` list |
| `submission_deadline` | date field or `null` | Only set when exactly one distinct submission date is found |
| `submission_time` | field or `null` | The time next to the chosen submission date |
| `deadline_candidates` | date field[] | Every date labelled as a submission date |
| `presentation_dates` | date field[] | Presentations, demos, vivas, interviews |
| `other_dates` | date field[] | Release, feedback, late-submission, exam and unlabelled dates |
| `word_count` | field or `null` | `value` is display text; `minimum` and `maximum` are integers (`minimum` is `null` for a single limit) |
| `deliverables` | field[] | Items under headings such as `Deliverables` or `What to submit` |
| `marking_criteria` | field[] | Items under `Marking criteria`, `Rubric`, `Marking scheme` |
| `instructions` | field[] | Items under `Instructions` or lines starting `Important:` / `Note:` |
| `needs_confirmation` | boolean | `false` only for one unambiguous, high-confidence submission date |
| `warnings` | string[] | Human-readable reasons to check the result |

At most 20 items are returned per list and each item is trimmed to 300 characters.

## Rules

1. **Dates are explicit.** Supported forms: `11 December 2026`, `11th Dec 2026`,
   `Friday 11 December 2026`, `December 11, 2026`, `11/12/2026`, `11.12.2026`,
   `11-12-26` and ISO `2026-12-11`. Month names may be abbreviated.
2. **Day first.** Numeric dates are read day/month/year, the Irish and European
   convention. If both parts are 12 or less (for example `03/04/2026`), confidence is
   `medium` because a US reader would see a different date.
3. **No invented years.** `14 March` gives a date field with `value: null`, `low`
   confidence and a warning. Two-digit years become 20xx with `medium` confidence.
4. **Weekday check.** If a weekday is written and does not match the date, confidence
   becomes `low`.
5. **Labels decide meaning.** The closest label before the date on the same line (or
   on the line above, for table layouts) classifies it. A longer label wins a tie, so
   `late submission` beats `submission`. `due to` is not a deadline.
6. **Ambiguity needs a person.** Several distinct submission dates leave
   `submission_deadline` empty and list them in `deadline_candidates`.
7. **Times.** `17:00`, `17.00`, `5pm`, `5:30 p.m.`, `12 noon`, `midday` and `midnight`
   (`00:00`) on the same line within 40 characters of the date.

## Example

Input text:

```text
Module: COMP3010 Software Engineering
Assessment Title: CA2 Group Project Report
Weighting: 40%
Submission deadline: Friday 11 December 2026 at 17:00
Presentations will take place on 15/12/2026.
```

Output (abridged):

```json
{
  "schema_version": 1,
  "title": {"value": "CA2 Group Project Report", "source_text": "Assessment Title: CA2 Group Project Report", "confidence": "high"},
  "module_name": {"value": "Software Engineering", "source_text": "Module: COMP3010 Software Engineering", "confidence": "high"},
  "module_code": {"value": "COMP3010", "source_text": "COMP3010 Software Engineering", "confidence": "medium"},
  "weighting": {"value": "40%", "source_text": "Weighting: 40%", "confidence": "high"},
  "submission_deadline": {
    "value": "2026-12-11", "time": "17:00", "confidence": "high", "label": "submission",
    "matched_text": "Friday 11 December 2026", "notes": [],
    "source_text": "Submission deadline: Friday 11 December 2026 at 17:00"
  },
  "submission_time": {"value": "17:00", "source_text": "Submission deadline: Friday 11 December 2026 at 17:00", "confidence": "high"},
  "presentation_dates": [{"value": "2026-12-15", "label": "presentation", "confidence": "high", "time": null, "notes": [], "matched_text": "15/12/2026", "source_text": "Presentations will take place on 15/12/2026."}],
  "needs_confirmation": false,
  "warnings": []
}
```

## Test cases

`tests/fixtures/extraction_cases.json` holds synthetic cases with expected values addressed
by dotted paths (`submission_deadline.value`, `presentation_dates.0.time`,
`warnings.length`). `tests/test_assessment_extraction.py` runs them in Python. A JavaScript
port should run the same file unchanged.

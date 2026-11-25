"""Run the shared, synthetic extraction cases against the Python implementation."""

import json
from pathlib import Path

import pytest

from assessment_extraction import extract_assessment

CASES = json.loads(
    (Path(__file__).parent / 'fixtures' / 'extraction_cases.json').read_text(encoding='utf-8')
)['cases']


def lookup(result, path):
    """Follow a dotted path such as 'presentation_dates.0.value' or 'warnings.length'."""
    value = result
    for part in path.split('.'):
        if part == 'length':
            return len(value)
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


@pytest.mark.parametrize('case', CASES, ids=[case['name'] for case in CASES])
def test_shared_extraction_case(case):
    result = extract_assessment(case['text'])
    for path, expected in case['expected'].items():
        assert lookup(result, path) == expected, path


def test_result_is_json_compatible_and_cites_sources():
    result = extract_assessment('Assessment title: Essay\nDeadline: 4 December 2026')
    assert json.loads(json.dumps(result)) == result
    assert result['schema_version'] == 1
    assert result['submission_deadline']['source_text'] == 'Deadline: 4 December 2026'
    assert result['title']['source_text'] == 'Assessment title: Essay'


def test_large_input_is_bounded():
    text = 'Instructions:\n' + '\n'.join(f'- Step {n}' for n in range(500))
    assert len(extract_assessment(text)['instructions']) == 20


def test_result_keys_match_the_documented_schema():
    schema_path = Path(__file__).parent.parent / 'docs' / 'assessment.schema.json'
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    for case in CASES:
        result = extract_assessment(case['text'])
        assert set(result) == set(schema['required']) == set(schema['properties'])
        for item in result['deadline_candidates'] + result['other_dates']:
            assert set(schema['$defs']['dateField']['required']) <= set(item)

"""Bounded source diagnostics contain reasons, never model text or values."""
import json

import pytest

from qian_labor.ai.grounding import ground_result, deterministic_citation_id, ExtractionInput
from qian_labor.ai.schemas import ExtractionResult
from qian_labor.parsers.protocols import ParsedBlock


@pytest.mark.parametrize('change,reason', [
    ({'citation_id': 'unknown-private-id'}, 'citation_unknown'),
    ({'excerpt': 'private-model-excerpt'}, 'excerpt_mismatch'),
    ({'column': 'C'}, 'position_mismatch'),
    ({'employee_id': 'OTHER-PRIVATE-ID'}, 'identity_mismatch'),
])
def test_failed_source_reports_only_bounded_reason(change, reason):
    location = {'sheet': 'synthetic', 'row': 2, 'column': 2}
    quote = '2026-01-01'
    citation = deterministic_citation_id('a'*64, location, quote)
    item = ExtractionInput('synthetic.xlsx', b'', (
        ParsedBlock('SYN-001', 'cell', {'sheet': 'synthetic', 'row': 2, 'column': 1}),
        ParsedBlock(quote, 'cell', location),
    ), source_file_hash='a'*64)
    change = dict(change)
    employee = change.pop('employee_id', 'SYN-001')
    result = ExtractionResult.model_validate({'facts': [{
        'employee_id': employee, 'fact_type': 'employment.probation.start_date',
        'value': quote, 'confidence': 1,
        'source': {'file_name': 'synthetic.xlsx', 'citation_id': citation,
                   'excerpt': quote, **change},
    }]})
    proof = ground_result(result, item, 'synthetic.xlsx')[0]
    assert proof['status'] == 'unlocated_needs_review'
    assert proof['diagnostic']['reason'] == reason
    assert not any(text in json.dumps(proof) for text in
                   ['private', 'PRIVATE', quote, citation, 'SYN-001'])
    assert result.facts[0].source.excerpt == ''


@pytest.mark.parametrize('column,index', [('B', 2), ('b', 2), ('AA', 27), ('XFD', 16384)])
def test_excel_column_letters_match_only_equivalent_parser_column(column, index):
    location = {'sheet': 'synthetic', 'row': 2, 'column': index}
    quote = '2026-01-01'
    item = ExtractionInput('synthetic.xlsx', b'', (
        ParsedBlock('SYN-001', 'cell', {'sheet': 'synthetic', 'row': 2, 'column': 1}),
        ParsedBlock(quote, 'cell', location),
    ), source_file_hash='a'*64)
    for citation in (None, deterministic_citation_id('a'*64, location, quote)):
        result = ExtractionResult.model_validate({'facts': [{
            'employee_id': 'SYN-001', 'fact_type': 'employment.probation.start_date',
            'value': quote, 'confidence': 1,
            'source': {'file_name': 'synthetic.xlsx', 'citation_id': citation,
                       'sheet': 'synthetic', 'row': 2, 'column': column, 'excerpt': quote},
        }]})
        proof = ground_result(result, item, 'synthetic.xlsx')[0]
        assert proof['status'] == 'locally_located', proof
        assert result.facts[0].source.column == str(index)


@pytest.mark.parametrize('column', ['C', 'B2', 'B:C', '0', '-1', 'XFE', '试用期开始'])
def test_wrong_or_non_coordinate_column_is_not_ignored(column):
    location = {'sheet': 'synthetic', 'row': 2, 'column': 2}
    quote = '2026-01-01'
    item = ExtractionInput('synthetic.xlsx', b'', (
        ParsedBlock('SYN-001', 'cell', {'sheet': 'synthetic', 'row': 2, 'column': 1}),
        ParsedBlock(quote, 'cell', location),
    ), source_file_hash='a'*64)
    result = ExtractionResult.model_validate({'facts': [{
        'employee_id': 'SYN-001', 'fact_type': 'employment.probation.start_date',
        'value': quote, 'confidence': 1,
        'source': {'file_name': 'synthetic.xlsx', 'column': column, 'excerpt': quote,
                   'citation_id': deterministic_citation_id('a'*64, location, quote)},
    }]})
    assert ground_result(result, item, 'synthetic.xlsx')[0]['status'] == 'unlocated_needs_review'


@pytest.mark.parametrize('citation,header,column,expected', [
    (True, '试用期开始', '试用期开始', 'locally_located'),
    (False, '试用期开始', '试用期开始', 'unlocated_needs_review'),
    (True, '试用期开始', '试用期结束', 'unlocated_needs_review'),
    (True, '试用期开始', ' 试用期开始', 'unlocated_needs_review'),
    (True, '试用期开始', 'C', 'unlocated_needs_review'),
])
def test_column_header_requires_exact_parser_citation_and_exact_header(citation, header, column, expected):
    location = {'sheet': 'synthetic', 'row': 2, 'column': 2, 'cell': 'B2', 'header': header}
    quote = '2026-01-01'
    item = ExtractionInput('synthetic.xlsx', b'', (
        ParsedBlock('SYN-001', 'cell', {'sheet': 'synthetic', 'row': 2, 'column': 1}),
        ParsedBlock(quote, 'cell', location),
    ), source_file_hash='a'*64)
    result = ExtractionResult.model_validate({'facts': [{
        'employee_id': 'SYN-001', 'fact_type': 'employment.probation.start_date',
        'value': quote, 'confidence': 1,
        'source': {'file_name': 'synthetic.xlsx', 'column': column, 'excerpt': quote,
                   'sheet': 'synthetic', 'row': 2,
                   'citation_id': deterministic_citation_id('a'*64, location, quote) if citation else None},
    }]})
    assert ground_result(result, item, 'synthetic.xlsx')[0]['status'] == expected
    if expected == 'locally_located':
        assert result.facts[0].source.column == '2'
        assert result.facts[0].source.cell == 'B2'

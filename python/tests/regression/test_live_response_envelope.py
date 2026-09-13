"""Synthetic reproductions of envelope failures found in the native App."""
import json
import time

import httpx
import pytest

from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from test_zhipu_provider import _provider_payload


def validate(content):
    return ZhipuChatCompletionsProvider._validated_result(
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}),
        time.monotonic(), 1,
    )


def test_identical_repeated_complete_objects_are_received_once():
    content = json.dumps(_provider_payload())
    result, _ = validate(content + '\n' + content)
    assert result is not None
    assert len(result.facts) == 1


def test_different_repeated_objects_are_not_silently_selected():
    first = _provider_payload()
    second = _provider_payload()
    second['employee_name'] = 'different synthetic employee'
    result, diagnostic = validate(json.dumps(first) + '\n' + json.dumps(second))
    assert result is None
    assert diagnostic.json_error_kind == 'extra_data'


@pytest.mark.parametrize('field', ['document_type', 'schema_version'])
def test_metadata_diagnostic_names_the_actual_field_without_its_value(field):
    payload = _provider_payload()
    payload[field] = 'synthetic-private-marker'
    result, diagnostic = validate(json.dumps(payload))
    assert result is None
    assert diagnostic.path == field
    assert diagnostic.validation_type == 'invalid_value'
    assert 'synthetic-private-marker' not in repr(diagnostic)


def test_advisory_diagnostic_identifies_broken_checks():
    payload = _provider_payload()
    payload['contract_advisory'] = {
        'version': 'contract-advisory-v1', 'status': 'completed',
        'observations': [{'source': {'file_name': 'synthetic.txt', 'excerpt': 'synthetic'},
                          'issue': 'synthetic', 'checks': 'wrong string', 'next_action': 'synthetic'}],
    }
    result, diagnostic = validate(json.dumps(payload))
    assert result is None
    assert diagnostic.path == 'contract_advisory.observations[].checks'


def test_rejected_item_reports_only_schema_field_and_error_type():
    payload = _provider_payload()
    payload['facts'][0]['source']['paragraph'] = 'private-synthetic-marker'
    result, _ = validate(json.dumps(payload))
    assert result.unreceived[0]['field'] == 'source.paragraph'
    assert result.unreceived[0]['validation_type'] == 'int_type'
    assert 'private-synthetic-marker' not in repr(result.unreceived)


def test_extra_fact_fields_do_not_discard_valid_declared_facts():
    payload = _provider_payload()
    payload['facts'][0]['explanation'] = 'synthetic-private-extension'
    payload['facts'][0]['unexpected'] = {'nested': ['synthetic']}
    result, _ = validate(json.dumps(payload))
    assert len(result.facts) == 1
    assert result.facts[0].value is True
    assert result.unreceived == []
    assert 'synthetic-private-extension' not in result.model_dump_json()


def test_extra_fields_cannot_make_a_broken_declared_value_valid():
    payload = _provider_payload()
    payload['facts'][0].update(value_boolean=1, explanation='synthetic')
    result, _ = validate(json.dumps(payload))
    assert result.facts == []
    assert result.unreceived[0]['field'] == 'value_boolean'


def test_explicitly_empty_date_carriers_remain_unknown_not_a_rejected_fact():
    payload = _provider_payload()
    payload['facts'][0].update(fact_type='employment.contract.end_date', value_type='text', value_boolean=None)
    result, _ = validate(json.dumps(payload))
    assert len(result.facts) == 1
    assert result.facts[0].value is None
    assert result.facts[0].needs_human_confirmation
    assert result.unreceived == []

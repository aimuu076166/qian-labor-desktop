"""Counterexamples from the independent review; synthetic HTTP only."""
from copy import deepcopy
import json

import httpx
import pytest

from qian_labor.ai.providers import AIProviderError
from qian_labor.jobs.control import ProcessingStopped, owned_invocation
from qian_labor.services.effective_facts import validate_manual_value
from test_zhipu_provider import _provider, _provider_payload, _success_response


@pytest.mark.parametrize('mutation', [
    lambda f: f.update(value_boolean=1),
    lambda f: f['source'].update(row='12'),
    lambda f: f.update(confidence='95%'),
    lambda f: f.update(value_type=[]),
    lambda f: f.update(value_type={}, value_='synthetic'),
    lambda f: f.update(fact_type='employment.probation.periods', value_type='json', value_boolean=None, value_json='{bad'),
])
def test_bad_fact_cannot_discard_nineteen_good_facts(mutation):
    payload = _provider_payload()
    payload['facts'] = [deepcopy(payload['facts'][0]) for _ in range(20)]
    mutation(payload['facts'][-1])
    calls = []
    def handler(request):
        calls.append(request)
        return _success_response(payload)
    result = _provider(handler).extract('synthetic.txt', b'synthetic')
    assert len(result.facts) == 19
    assert len(result.unreceived) == 1
    assert result.unreceived[0]['index'] == '19'
    assert len(calls) == 1


def test_omitted_unused_carrier_is_locally_filled():
    payload = _provider_payload()
    payload['facts'][0].pop('value_integer')
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert result.facts[0].value is True


@pytest.mark.parametrize('text', ['2026-01-01 至 2026-03-01', '2026年1月1日至2026年3月1日'])
def test_converted_period_can_be_confirmed_by_existing_app(text):
    payload = _provider_payload()
    payload['facts'][0].update(fact_type='employment.probation.periods', value_type='text', value_boolean=None, value_text=text)
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert validate_manual_value('employment.probation.periods', result.facts[0].value) == [['2026-01-01', '2026-03-01']]


@pytest.mark.parametrize('text', ['2026-02-31 至 2026-03-01', '2026-03-01 至 2026-01-01'])
def test_invalid_period_is_unreceived_without_losing_contract(text):
    payload = _provider_payload()
    fact = deepcopy(payload['facts'][0])
    fact.update(fact_type='employment.probation.periods', value_type='text', value_boolean=None, value_text=text)
    payload['facts'].append(fact)
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert len(result.facts) == 1
    assert len(result.unreceived) == 1


def test_json_conflict_preserves_declared_json_not_opposing_text():
    payload = _provider_payload()
    payload['facts'][0].update(fact_type='employment.probation.periods', value_type='json', value_boolean=None,
                              value_json='[["2026-01-01","2026-03-01"]]', value_text='opposing synthetic text')
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert result.facts[0].value == [['2026-01-01', '2026-03-01']]
    assert result.facts[0].needs_human_confirmation


def test_all_rejected_items_are_returned_for_durable_partial_result():
    payload = _provider_payload()
    payload['facts'][0]['value_boolean'] = 1
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert result.facts == [] and len(result.unreceived) == 1


def test_unknown_type_cannot_copy_arbitrary_content_to_diagnostics():
    payload = _provider_payload()
    payload['facts'][0]['fact_type'] = 'synthetic-secret-marker'
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert 'synthetic-secret-marker' not in repr(result.unreceived)


def _repair_handler(calls, *, second_status=200, on_first=None):
    def handler(request):
        calls.append(request)
        payload = _provider_payload()
        if len(calls) == 1:
            payload['document_type'] = 'invalid-synthetic-document-type'
            if on_first:
                on_first()
        elif second_status != 200:
            return httpx.Response(second_status, json={'error': {'code': 'synthetic'}})
        return _success_response(payload)
    return handler


def test_repair_accumulates_known_tokens_from_both_responses():
    calls = []
    result = _provider(_repair_handler(calls), max_attempts=1).extract('synthetic.txt', b'synthetic')
    assert len(calls) == 2
    assert result.usage.input_tokens == 246
    assert result.usage.output_tokens == 90
    assert result.usage.attempts == 2


def test_repair_http_failure_is_not_hidden_by_first_schema_failure():
    calls = []
    with pytest.raises(AIProviderError) as caught:
        _provider(_repair_handler(calls, second_status=401), max_attempts=1).extract('synthetic.txt', b'synthetic')
    assert caught.value.diagnostic.status_code == 401
    assert caught.value.diagnostic.attempt == 2
    assert caught.value.code == 'AI_PROVIDER_ERROR'
    assert caught.value.usage.input_tokens == 123
    assert caught.value.usage.attempts == 2


def test_cancel_during_first_request_prevents_repair_request():
    class Control:
        stopped = False
        def checkpoint(self):
            if self.stopped:
                raise ProcessingStopped()
    control, calls = Control(), []
    with owned_invocation(control), pytest.raises(ProcessingStopped):
        _provider(_repair_handler(calls, on_first=lambda: setattr(control, 'stopped', True)), max_attempts=1).extract('synthetic.txt', b'synthetic')
    assert len(calls) == 1


@pytest.mark.parametrize('repair', [False, True])
def test_http_error_response_known_usage_is_not_lost(repair):
    calls = []
    def handler(request):
        calls.append(request)
        if repair and len(calls) == 1:
            payload = _provider_payload()
            payload['document_type'] = 'invalid'
            return _success_response(payload)
        if len(calls) == 1 or repair:
            return httpx.Response(500, json={'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
        return _success_response(_provider_payload())
    provider = _provider(handler, max_attempts=2)
    if repair:
        with pytest.raises(AIProviderError) as caught:
            provider.extract('synthetic.txt', b'synthetic')
        usage = caught.value.usage
    else:
        usage = provider.extract('synthetic.txt', b'synthetic').usage
    assert (usage.input_tokens, usage.output_tokens, usage.attempts) == (133, 65, 2)


def test_native_json_period_carrier_is_losslessly_serialized():
    payload = _provider_payload()
    payload['facts'][0].update(fact_type='employment.probation.periods', value_type='json', value_boolean=None,
                              value_json=[['2026-01-01', '2026-03-01']])
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert result.facts[0].value == [['2026-01-01', '2026-03-01']]


def test_text_decimal_cannot_be_silently_rounded_during_conversion():
    payload = _provider_payload()
    payload['facts'][0].update(fact_type='employment.pay.contract_wage', value_type='text', value_boolean=None,
                              value_text='9007199254740993.5')
    result = _provider(lambda _: _success_response(payload)).extract('synthetic.txt', b'synthetic')
    assert not result.facts
    assert result.unreceived[0]['reason'] == 'unconvertible_value_type'

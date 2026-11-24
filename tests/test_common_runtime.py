import asyncio
import json
from dataclasses import asdict

import httpx
import pytest

from decissions import (Runtime, DecisionPolicy, ClassificationHandler, PresentationHandler,
                       SafetyHandler, RiskAssessmentHandler, RiskAssessor, SafetyGate,
                       Mode, load_decision_policies, load_risk_registry)
from decissions.runtime.api import DecisionOutcome
from decissions.runtime.config import DomainPolicy, SelectorKind, Settings
from decissions.runtime.selectors.jev import JevSelector, FIT_ID, parse_response
from decissions.runtime.selectors.base import SelectorInvalidResponse
from decissions.contracts.models import SelectorOutput
from tests.test_decision_modules import data, ref
from tests.test_engine import FakeEvaluator, policy as safety_policy, request as safety_request
from tests.test_risk_assessment import Evaluator, EXAMPLE


def request(policy='topics', version='1', *, request_id='r', tenant='t', input=None):
    return dict(policy=policy, policy_version=version, request_id=request_id, tenant_id=tenant,
                input={'text': 'Where is my invoice?'} if input is None else input)


class Classifier:
    name = 'hosted_model'
    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
    def release_for(self, domain): return 'fake/1'
    async def select(self, domain, ctx, elig, budget):
        self.calls.append(ctx)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.001)
        self.active -= 1
        return SelectorOutput(option_id='billing', probabilities={'billing': .9, 'support': .1},
                              fit_probability=.95, provider_confidence=.8, model_release='fake/1')


def make_runtime(selector=None, **kwargs):
    classifier = ClassificationHandler(question='Which department handles this text?',
        candidates={'billing': 'Invoices and payments', 'support': 'Technical issues'}, version='1')
    return Runtime(policies=[DecisionPolicy(id='topics', version='1', handler='topics',
        selection=DomainPolicy(serving='hosted_model', external_inference_tenants=['t']))],
        handlers={'topics': classifier}, selectors={SelectorKind.HOSTED: selector} if selector else {},
        settings=Settings(_env_file=None), **kwargs)


@pytest.mark.parametrize('mode', [Mode.ENFORCE, Mode.SHADOW])
@pytest.mark.parametrize('probability', [.1, .4, .9])
def test_safety_preserves_existing_policy_results(mode, probability):
    async def run():
        gate = SafetyGate(safety_policy(mode), FakeEvaluator(probability))
        runtime = Runtime(policies=[DecisionPolicy(id='chat', version='test/1', handler='safety')],
                             handlers={'safety': SafetyHandler(gate)})
        result = await runtime.decide(**request('chat', 'test/1', input={
            'content': 'synthetic test input', 'actor_ref': 'opaque-actor'}))
        expected = await gate.evaluate(safety_request())
        expected_json = json.loads(json.dumps(asdict(expected)))
        expected_json['request_id'] = 'r'
        assert result.result == expected_json
        assert result.kind == 'safety.policy'
        assert result.status == 'complete'
        assert 'synthetic test input' not in result.model_dump_json()
    asyncio.run(run())


def test_safety_error_and_session_follow_existing_fallback():
    async def run():
        gate = SafetyGate(safety_policy(session=True), FakeEvaluator(error=RuntimeError('secret')))
        runtime = Runtime(policies=[DecisionPolicy(id='chat', version='test/1', handler='safety')],
                             handlers={'safety': SafetyHandler(gate)})
        result = await runtime.decide(request('chat', 'test/1', input={'content': 'draft',
            'actor_ref': 'actor', 'stage': 'output_release', 'assessment_scopes': ['session'],
            'session_ref': 's', 'session_revision': 1,
            'recent_turns': [{'role': 'user', 'text': 'previous'}]}))
        assert result.status == 'fallback'
        assert result.result['effective_action'] == 'review'
        assert not result.result['tool_eligible']
        assert 'secret' not in result.model_dump_json()
    asyncio.run(run())


def test_all_capabilities_share_one_api_and_batch():
    async def run():
        registry = load_risk_registry(EXAMPLE)
        runtime = make_runtime(Classifier())
        # Construct a single runtime with all installed policies.
        runtime = Runtime(policies=[
            DecisionPolicy(id='topics', version='1', handler='topics', selection=DomainPolicy(
                serving='hosted_model', external_inference_tenants=['t'])),
            DecisionPolicy(id='chart', version='1', handler='chart'),
            DecisionPolicy(id='risks', version=registry.version, handler='risks'),
        ], handlers={'topics': runtime._handlers['topics'], 'chart': PresentationHandler(),
                     'risks': RiskAssessmentHandler(RiskAssessor(registry, Evaluator()))},
           selectors={SelectorKind.HOSTED: Classifier()}, settings=Settings(_env_file=None))
        results = await runtime.decide_many([
            request(request_id='row-1'),
            request('chart', request_id='chart-1', input={'reference': ref().model_dump(mode='json'),
                'data': data().model_dump(mode='json'), 'intent': {'text': 'compare'}}),
            request('risks', registry.version, request_id='risk-1', input={'content': 'question'}),
        ])
        assert [r.kind for r in results] == ['classification', 'presentation', 'safety.assessment']
        assert all(r.status == 'complete' for r in results)
        assert results[0].result['label'] == 'billing'
        assert results[1].result['rendering_status'] == 'ready'
        assert len(results[2].result['scores']) == 4
        assert all(r.schema_version == 'decissions.response/1' for r in results)
    asyncio.run(run())


def test_classification_abstains_without_authorized_provider():
    async def run():
        selector = Classifier()
        runtime = make_runtime(selector)
        denied = await runtime.decide(request(tenant='not-enabled'))
        offline = await make_runtime().decide(request())
        for result in (denied, offline):
            assert result.result['label'] is None
            assert result.result['disposition'] == 'abstain'
            assert result.status == 'fallback'
        assert not selector.calls
    asyncio.run(run())


def test_batches_preserve_order_bound_concurrency_and_preflight():
    async def run():
        selector = Classifier()
        runtime = make_runtime(selector, concurrency=2, max_batch_items=5)
        results = await runtime.decide_many([request(request_id=str(i)) for i in range(5)])
        assert [r.request_id for r in results] == [str(i) for i in range(5)]
        assert selector.peak == 2
        selector.calls.clear()
        for batch in ([request(), request(request_id='bad', input={'text': 'x', 'endpoint': 'evil'})],
                      [request(), request()], [request(request_id=str(i)) for i in range(6)],
                      [request(), request(version='wrong', request_id='bad')]):
            with pytest.raises(ValueError): await runtime.decide_many(batch)
        assert not selector.calls
    asyncio.run(run())


def test_policy_admission_and_version_cannot_be_overridden():
    async def run():
        selector = Classifier()
        runtime = Runtime(policies=[DecisionPolicy(id='topics', version='1', handler='topics', tenants={'allowed'})],
            handlers={'topics': make_runtime()._handlers['topics']}, selectors={SelectorKind.HOSTED: selector})
        with pytest.raises(PermissionError): await runtime.decide(request())
        with pytest.raises(ValueError): await runtime.decide(request(version='wrong', tenant='allowed'))
        with pytest.raises(ValueError): await runtime.decide(request(policy='unknown'))
        assert not selector.calls
    asyncio.run(run())


def test_custom_handler_timeout_and_failure_are_explicit():
    class Custom:
        kind = 'custom'
        def prepare(self, request, policy): return request.input
        async def execute(self, value, request, policy, engine):
            if value.get('slow'): await asyncio.sleep(1)
            raise RuntimeError('private secret')
    async def run():
        runtime = Runtime(policies=[DecisionPolicy(id='custom', version='1', handler='custom', timeout_seconds=.01)],
                             handlers={'custom': Custom()})
        for value, code in [({'slow': True}, 'DECISION_TIMEOUT'), ({}, 'DECISION_UNAVAILABLE')]:
            response = await runtime.decide(request('custom', input=value))
            assert response.status == 'unavailable' and response.error_code == code
            assert response.result == {}
            assert 'private secret' not in response.model_dump_json()
    asyncio.run(run())


def test_runtime_handles_unexpected_selector_exception():
    class Broken(Classifier):
        async def select(self, *args): raise RuntimeError('secret')
    result = asyncio.run(make_runtime(Broken()).decide(request()))
    assert result.result['label'] is None
    assert result.result['decision']['fallback_reason'] == 'provider_error'


def test_fit_and_confidence_policy_are_applied():
    class Uncertain(Classifier):
        async def select(self, *args):
            value = await super().select(*args)
            return value.model_copy(update={'fit_probability': .1})
    result = asyncio.run(make_runtime(Uncertain()).decide(request()))
    assert result.result['label'] is None
    assert result.result['decision']['fallback_reason'] == 'abstained'
    assert result.result['probabilities'] is None


def test_jev_end_to_end_through_common_runtime():
    def respond(req):
        body = json.loads(req.content)
        assert set(body['questions']['choice']['criteria']) == {'billing', 'support'}
        assert body['state']['untrusted_user_request'] == 'Where is my invoice?'
        return httpx.Response(200, json={'model': 'pinned', 'answers': {
            'choice': {'type': 'choice', 'choice': 'billing', 'confidence': .9,
                       'probabilities': {'billing': .95, 'support': .05}},
            FIT_ID: {'type': 'noul', 'noul': .99}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            selector = JevSelector(client, 'test', 'http://gateway.test/decisions', 'pinned')
            result = await make_runtime(selector).decide(request())
            assert result.status == 'complete' and result.result['label'] == 'billing'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['missing_fit', 'missing_probability', 'wrong_model', 'wrong_type'])
def test_malformed_jev_response_rejected(change):
    domain = make_runtime()._handlers['topics'].domain
    eligible = domain.eligibility(None)
    eligible = type(eligible)(eligible.options[:-1], [], 'billing')
    body = {'model': 'pinned', 'answers': {
        'choice': {'type': 'choice', 'choice': 'billing', 'probabilities': {'billing': 1, 'support': 0}},
        FIT_ID: {'type': 'noul', 'noul': .9}}}
    if change == 'missing_fit': del body['answers'][FIT_ID]
    if change == 'missing_probability': del body['answers']['choice']['probabilities']['support']
    if change == 'wrong_model': body['model'] = 'other'
    if change == 'wrong_type': body['answers']['choice']['type'] = 'score'
    with pytest.raises(SelectorInvalidResponse): parse_response(body, eligible, model='pinned')


def test_policy_file_rejects_duplicate_keys_and_unknown_fields(tmp_path):
    file = tmp_path / 'policies.json'
    file.write_text('{"policies": [], "policies": []}')
    with pytest.raises(ValueError): load_decision_policies(file)
    file.write_text(json.dumps({'policies': [{'id': 'x', 'version': '1', 'handler': 'h', 'secret': 'x'}]}))
    with pytest.raises(ValueError): load_decision_policies(file)


def test_replay_is_scoped_to_policy_and_classification_definitions():
    from tests.test_runtime_adoption import Store

    async def run():
        selector, store = Classifier(), Store()
        handlers = {'first': ClassificationHandler(version='1', question='Choose a department',
            candidates={'billing': 'Invoices', 'support': 'Technical issues'}),
            'second': ClassificationHandler(version='1', question='Choose a department',
            candidates={'billing': 'Subscriptions', 'support': 'Onboarding'})}
        policies = [DecisionPolicy(id=name, version='1', handler=name, selection=DomainPolicy(
            serving='hosted_model', external_inference_tenants=['t'])) for name in handlers]
        runtime = Runtime(policies=policies, handlers=handlers, store=store,
                          selectors={SelectorKind.HOSTED: selector})
        first = await runtime.decide(request('first'))
        replay = await runtime.decide(request('first'))
        second = await runtime.decide(request('second'))
        assert replay.result['decision']['replayed']
        assert len(selector.calls) == 2
        assert first.result['decision']['input_hash'] != second.result['decision']['input_hash']
        assert first.result['decision']['domain_version'] != second.result['decision']['domain_version']
    asyncio.run(run())


def test_invalid_policy_configuration_and_nonfinite_input_fail_before_execution():
    handler = make_runtime()._handlers['topics']
    p = DecisionPolicy(id='topics', version='1', handler='topics')
    with pytest.raises(ValueError): Runtime(policies=[p, p], handlers={'topics': handler})
    with pytest.raises(ValueError): Runtime(policies=[p], handlers={})
    with pytest.raises(ValueError): DomainPolicy(min_fit_probability=float('nan'))
    with pytest.raises(ValueError): DomainPolicy(selector_deadline_ms=0)
    with pytest.raises(ValueError):
        asyncio.run(make_runtime().decide(request(input={'text': 'x', 'value': float('nan')})))


def test_safety_hard_rules_and_invalid_route_selection():
    from tests.test_engine import RefuseRule

    async def run():
        evaluator = FakeEvaluator(.0)
        gate = SafetyGate(safety_policy(), evaluator, [RefuseRule()])
        handler = SafetyHandler(gate)
        route = DecisionPolicy(id='chat', version='test/1', handler='safety')
        runtime = Runtime(policies=[route], handlers={'safety': handler})
        result = await runtime.decide(request('chat', 'test/1', input={'content': 'text', 'actor_ref': 'actor'}))
        assert result.result['effective_action'] == 'refuse'
        assert evaluator.calls == 0
        invalid = route.model_copy(update={'selection': DomainPolicy(serving='rules')})
        runtime = Runtime(policies=[invalid], handlers={'safety': handler})
        with pytest.raises(ValueError):
            await runtime.decide(request('chat', 'test/1', input={'content': 'text', 'actor_ref': 'actor'}))
    asyncio.run(run())


def test_removed_chart_engine_has_no_compatibility_import():
    import importlib.util
    assert importlib.util.find_spec('decissions.decisions') is None
    assert importlib.util.find_spec('decissions.adapters.jev_decision') is None

"""Three capabilities, one runtime. Synthetic scores only; no network or credentials."""
import asyncio
import hashlib
import json
from pathlib import Path

from decissions import (
    Runtime, DecisionPolicy, DomainPolicy, SelectorKind, Settings,
    SafetyGate, SafetyHandler, PresentationHandler, ClassificationHandler,
    Evaluation, Signal, load_policy_json,
)
from decissions.contracts import SelectorOutput


class DemoSafetyEvaluator:
    def __init__(self, categories):
        self.categories = categories

    async def evaluate(self, request):
        return Evaluation(tuple(Signal(category, .1, scope=scope)
            for scope in request.assessment_scopes for category in self.categories),
            evaluator='synthetic-demo', model_version='demo/1')


class DemoClassifier:
    name = 'hosted_model'

    def release_for(self, domain):
        return 'synthetic-demo/1'

    async def select(self, domain, ctx, eligible, budget_s):
        # Demonstrates the contract; these invented probabilities are not calibration evidence.
        selected = 'billing' if 'invoice' in ctx.input.text.lower() else 'support'
        return SelectorOutput(option_id=selected,
            probabilities={o.option_id: .9 if o.option_id == selected else .1 for o in eligible.options},
            provider_confidence=.8, fit_probability=.95, model='synthetic-demo', model_release='demo/1')


def create_runtime():
    config = load_policy_json(Path(__file__).with_name('runtime-policy.json'))
    return Runtime(
        policies=[
            DecisionPolicy(id='chat', version=config.policy.version, handler='safety'),
            DecisionPolicy(id='chart', version='1', handler='presentation'),
            DecisionPolicy(id='topics', version='1', handler='classification', selection=DomainPolicy(
                serving='hosted_model', external_inference_tenants=['demo'])),
        ],
        handlers={
            'safety': SafetyHandler(SafetyGate(config.policy, DemoSafetyEvaluator(config.policy.required_categories))),
            'presentation': PresentationHandler(),
            'classification': ClassificationHandler(version='1', question='Which team handles this message?',
                candidates={'billing': 'Invoices and payments', 'support': 'Technical support'}),
        },
        settings=Settings(_env_file=None, serving_selector='rules', shadow_selectors=[]),
        selectors={SelectorKind.HOSTED: DemoClassifier()},
    ), config.policy.version


async def main():
    runtime, safety_version = create_runtime()
    rows = [{'region': 'East', 'orders': 12}, {'region': 'West', 'orders': 8}]
    result_hash = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    inputs = [
        dict(policy='chat', policy_version=safety_version, input={'content': 'Explain chart types.', 'actor_ref': 'demo-user'}),
        dict(policy='chart', policy_version='1', input={
            'reference': {'result_id': 'orders', 'revision': 1, 'content_hash': 'sha256:' + result_hash},
            'data': {'fields': [
                {'name': 'region', 'role': 'dimension', 'dtype': 'string'},
                {'name': 'orders', 'role': 'measure', 'dtype': 'integer', 'semantics': 'count', 'additive': True},
            ], 'rows': rows, 'is_complete': True, 'total_rows': 2},
            'intent': {'text': 'Compare orders by region'},
        }),
        dict(policy='topics', policy_version='1', input={'text': 'Please send my invoice.'}),
    ]
    for index, item in enumerate(inputs):
        response = await runtime.decide(request_id=f'demo-{index}', tenant_id='demo', **item)
        print(json.dumps({'kind': response.kind, 'status': response.status,
                          'result': response.result}, indent=2))


if __name__ == '__main__':
    asyncio.run(main())

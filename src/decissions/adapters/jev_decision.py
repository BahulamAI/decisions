"""Generic Jev Choice adapter, isolated from assessment probabilities and product policy."""
from __future__ import annotations

import math
from ..decisions.models import SelectorOutput
from .jev import JevEvaluator, _post_decisions, _validate_endpoint


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('invalid decision probability')
    return float(value)


class JevDecisionSelector:
    name = 'hosted_model'

    def __init__(self, *, api_key: str, model: str,
                 endpoint: str = JevEvaluator.ENDPOINT, client=None,
                 max_response_bytes: int = 262144):
        if not api_key or not model or type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError('credentials, model and positive response limit required')
        self._key, self.model = api_key, model
        self.endpoint, self.client = _validate_endpoint(endpoint), client
        self.max_response_bytes = max_response_bytes

    def release_for(self, domain):
        return f'jev-choice/1:{self.model}'

    async def select(self, domain, context, eligible, budget_s):
        if not 1 <= len(eligible.options) <= 255:
            raise ValueError('Jev requires 1–255 eligible options')
        criteria = {o.option_id: dict(o.description) for o in eligible.options}
        body = {'model':self.model,
                'state':{'facts':domain.model_state(context),
                         'untrusted_user_request':domain.untrusted_text(context.input)},
                'questions':{
                    'choice':{'type':'choice', 'instructions':domain.choice_instructions, 'criteria':criteria},
                    'fit':{'type':'noul', 'instructions':'At least one offered option faithfully serves this request.',
                           'criteria':{'true':'An offered option fits the request', 'false':'None fits or the request is unclear'}}}}
        raw = await _post_decisions(endpoint=self.endpoint, body=body, api_key=self._key,
            timeout_seconds=budget_s, max_response_bytes=self.max_response_bytes, client=self.client)
        model = raw['model']
        if not isinstance(model, str) or not (model == self.model or model.startswith(self.model+'-')):
            raise ValueError('unexpected model release')
        answers = raw['answers']
        if set(answers) != {'choice','fit'}:
            raise ValueError('unexpected decision answers')
        choice, fit = answers['choice'], answers['fit']
        if choice['type'] != 'choice' or fit['type'] != 'noul' or choice['choice'] not in criteria:
            raise ValueError('invalid decision answer')
        probs = {key:probability(value) for key,value in choice['probabilities'].items()}
        if set(probs) != set(criteria) or abs(sum(probs.values())-1) > 0.02:
            raise ValueError('invalid candidate probabilities')
        return SelectorOutput(option_id=choice['choice'], probabilities=probs,
            provider_confidence=probability(choice['confidence']) if 'confidence' in choice else None,
            fit_probability=probability(fit['noul']), model=self.model, model_release=model)

"""Caller-configured text classification, including text columns fetched with SQL."""
from __future__ import annotations

from typing import Mapping
import hashlib
import json

from pydantic import Field

from .contracts.domain import DecisionDomain
from .contracts.models import Eligibility, Option
from .runtime.api import Contract

ABSTAIN = '__abstain__'


class ClassificationInput(Contract):
    text: str = Field(min_length=1, max_length=12000)


class ClassificationDomain(DecisionDomain):
    name = 'text.classify'
    subject_kind = 'text'
    description = 'Classify supplied text using the configured category definitions'
    input_model = ClassificationInput

    def __init__(self, *, question: str, candidates: Mapping[str, str], version: str):
        if not question.strip() or len(question) > 4000 or not version.strip():
            raise ValueError('bounded question and version are required')
        if not 2 <= len(candidates) <= 255 or ABSTAIN in candidates:
            raise ValueError('classification requires 2–255 candidates; abstention is reserved')
        self.definition_version, self.choice_instructions = version, question
        digest = hashlib.sha256(json.dumps({'question': question, 'candidates': dict(candidates)},
                                           sort_keys=True).encode()).hexdigest()
        self.version = f'{version}:{digest}'
        self._options = []
        for key, description in candidates.items():
            if not isinstance(description, str) or not description.strip() or len(description) > 4000:
                raise ValueError('candidate descriptions must be nonempty bounded text')
            self._options.append(Option(option_id=key, group='classification',
                                        description={'what': description}))
        self._fallback = Option(option_id=ABSTAIN, group='abstain',
                                description={'what': 'No classification accepted'})

    def eligibility(self, inp):
        return Eligibility([*self._options, self._fallback], [], ABSTAIN)

    def choice_options(self, ctx, elig):
        return [option for option in elig.options if option.option_id != ABSTAIN]

    def rules_select(self, ctx, elig):
        # An unavailable classifier must never manufacture a category label.
        return ABSTAIN

    def untrusted_text(self, inp):
        return inp.text

    def audit_summary(self, ctx):
        return {'character_count': len(ctx.input.text), 'candidate_count': len(self._options)}

"""Capability adapters for the common runtime. Existing safety semantics are retained."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from pydantic import Field, StrictBool

from .assessment import AssessmentInput, RiskAssessor
from .classification import ABSTAIN, ClassificationDomain, ClassificationInput
from .contracts.models import FallbackReason, SubjectRef
from .engine import SafetyGate
from .models import (AssessmentScope, Category, RecentTurn, RiskTier, SafetyInput,
                     ScannerStatus, Stage)
from .runtime.api import Contract, DecisionOutcome, selection_policy


class ConversationInput(Contract):
    content: str = Field(min_length=1)
    stage: Stage = Stage.INPUT
    surface: str = 'chat'
    recent_turns: tuple[RecentTurn, ...] = ()
    safety_summary: str = ''
    assessment_scopes: tuple[AssessmentScope, ...] = (AssessmentScope.MESSAGE,)
    session_ref: str | None = None
    session_revision: int | None = None


class SafetyPayload(ConversationInput):
    actor_ref: str = Field(min_length=1)
    risk_tier: RiskTier = RiskTier.LOW
    attachment_kinds: tuple[str, ...] = ()
    scanner_status: ScannerStatus = ScannerStatus.NOT_APPLICABLE
    authorization_facts: dict[str, StrictBool] = Field(default_factory=dict)
    focus_categories: tuple[Category, ...] = ()


class SafetyHandler:
    kind = 'safety.policy'

    def __init__(self, gate: SafetyGate):
        self.gate = gate

    def prepare(self, request, policy):
        if policy.selection.model_dump(exclude_none=True):
            raise ValueError('safety uses its installed PolicyBundle, not candidate-selection settings')
        if policy.version != self.gate.policy.version:
            raise ValueError('route version must match the installed safety policy')
        data = SafetyPayload.model_validate(request.input)
        return SafetyInput(request_id=request.request_id, tenant_ref=request.tenant_id,
                           policy_version=policy.version, **dict(data))

    async def execute(self, prepared, request, policy, engine):
        result = await self.gate.evaluate(prepared)
        return DecisionOutcome(status='fallback' if result.degraded else 'complete',
                               result=json.loads(json.dumps(asdict(result))))


class AssessmentPayload(ConversationInput):
    family_ids: tuple[str, ...] | None = None
    top_k_families: int | None = Field(default=None, gt=0, strict=True)


class RiskAssessmentHandler:
    kind = 'safety.assessment'

    def __init__(self, assessor: RiskAssessor):
        self.assessor = assessor

    def prepare(self, request, policy):
        if policy.selection.model_dump(exclude_none=True):
            raise ValueError('risk assessment uses its installed registry, not candidate-selection settings')
        if policy.version != self.assessor.registry.version:
            raise ValueError('route version must match the installed risk registry')
        data = AssessmentPayload.model_validate(request.input)
        fields = dict(data)
        families, top_k = fields.pop('family_ids'), fields.pop('top_k_families')
        if families is not None:
            if top_k is not None or not families or len(set(families)) != len(families):
                raise ValueError('subfamily selection requires unique parents and no top-k')
            for family in families:
                if family not in self.assessor.registry.families or not self.assessor.registry.families[family].subfamilies:
                    raise ValueError('unknown family or family has no subfamilies')
        value = AssessmentInput(request_id=request.request_id, registry_version=policy.version, **fields)
        self.assessor.registry.limits.validate(value._context())
        return value, families, top_k

    async def execute(self, prepared, request, policy, engine):
        value, families, top_k = prepared
        result = (await self.assessor.assess_subfamilies(value, family_ids=families) if families is not None
                  else await self.assessor.assess_families(value, top_k_families=top_k))
        return DecisionOutcome(status=result.status.value,
                               result=json.loads(json.dumps(result.to_dict())), error_code=result.error_code)


class PresentationHandler:
    kind = 'presentation'

    def __init__(self, *, renderer=None):
        self.renderer = renderer

    def prepare(self, request, policy):
        # Importing safety/classification handlers never loads the optional chart compiler.
        from .presentations.service import PresentationPayload, validate_admission
        data = PresentationPayload.model_validate(request.input)
        validate_admission(data.data, data.title)
        return data

    async def execute(self, prepared, request, policy, engine):
        from .presentations.service import PresentationService
        from .presentations.domain import PresentationDomain
        result = await PresentationService(engine=engine, renderer=self.renderer).present(
            request_id=request.request_id, tenant_id=request.tenant_id,
            reference=prepared.reference, data=prepared.data, intent=prepared.intent,
            title=prepared.title, policy=selection_policy(engine, PresentationDomain(), policy))
        degraded = (result.rendering_status != 'ready' or
                    result.decision.fallback_reason != FallbackReason.NONE)
        return DecisionOutcome(status='fallback' if degraded else 'complete', result={
            'decision': result.decision.model_dump(mode='json'), 'rendering': result.rendering,
            'renderer': result.renderer, 'rendering_status': result.rendering_status})


class ClassificationHandler:
    kind = 'classification'

    def __init__(self, *, question, candidates, version):
        self.domain = ClassificationDomain(question=question, candidates=candidates, version=version)

    def prepare(self, request, policy):
        if policy.version != self.domain.definition_version:
            raise ValueError('route version must match classification definitions')
        return ClassificationInput.model_validate(request.input)

    async def execute(self, prepared, request, policy, engine):
        digest = hashlib.sha256(prepared.text.encode()).hexdigest()
        result = await engine.decide(self.domain, request_id=request.request_id, tenant_id=request.tenant_id,
            subject=SubjectRef(kind='text', id=request.request_id, content_hash='sha256:' + digest),
            input=prepared.model_dump(), policy=selection_policy(engine, self.domain, policy))
        accepted = result.option_id != ABSTAIN
        return DecisionOutcome(status='complete' if accepted else 'fallback', result={
            'label': result.option_id if accepted else None,
            'disposition': 'answer' if accepted else 'abstain',
            'probabilities': result.probabilities,
            'decision': result.model_dump(mode='json')})

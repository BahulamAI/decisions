"""Product-neutral chart decisions with an interchangeable rendering adapter."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from ..decisions.engine import DecisionEngine
from ..decisions.models import DecisionRecord, SubjectRef
from .contract import Candidate, Intent, ResultRef
from .domain import PresentationDomain, candidate_of
from .profile import ProfileInput, build_profile


class Renderer(Protocol):
    """A host-supplied adapter must advertise the candidates it can compile."""
    name: str

    def supports(self, candidate: Candidate) -> bool: ...

    def compile(self, candidate: Candidate, data: ProfileInput, reference: ResultRef,
                intent: Intent, title: str | None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PresentationResult:
    decision: DecisionRecord
    rendering: dict[str, Any]
    renderer: str
    rendering_status: str


class PresentationService:
    def __init__(self, *, engine: DecisionEngine | None = None, renderer: Renderer | None = None):
        if renderer is None:
            from ..renderers.workplane.adapter import WorkplaneRenderer
            renderer = WorkplaneRenderer()
        self.engine, self.renderer = engine or DecisionEngine(), renderer

    async def present(self, *, request_id: str, tenant_id: str, reference: ResultRef,
                      data: ProfileInput, intent: Intent | None = None,
                      title: str | None = None) -> PresentationResult:
        # Take one immutable-by-ownership snapshot before awaiting a remote selector.
        data = ProfileInput.model_validate(data.model_dump(mode='json'))
        intent = Intent.model_validate((intent or Intent()).model_dump(mode='json'))
        reference = ResultRef.model_validate(reference.model_dump(mode='json'))
        if len(data.rows) > 1000 or len(data.fields) > 50:
            raise ValueError('inline presentation exceeds row or field limits')
        if title is not None and (not isinstance(title, str) or len(title) > 300):
            raise ValueError('title must be bounded text')
        if data.total_rows is not None and (data.total_rows < len(data.rows) or
                (data.is_complete and data.total_rows != len(data.rows))):
            raise ValueError('declared coverage contradicts supplied rows')
        names = {f.name for f in data.fields}
        if any(set(row) - names for row in data.rows):
            raise ValueError('rows contain undeclared fields')
        if any(isinstance(v, (list, dict)) for row in data.rows for v in row.values()):
            raise ValueError('rows must contain scalar values')
        if len(json.dumps(data.model_dump(mode='json'), allow_nan=False).encode()) > 2_000_000:
            raise ValueError('inline presentation exceeds byte limit')
        profile = build_profile(data)
        renderer = self.renderer

        class AvailableDomain(PresentationDomain):
            def eligibility(self, inp):
                from ..decisions.models import Eligibility, Ineligible
                all_options = super().eligibility(inp)
                available, rejected = [], list(all_options.ineligible)
                for option in all_options.options:
                    if renderer.supports(candidate_of(option)):
                        available.append(option)
                    else:
                        rejected.append(Ineligible(group=option.group, reason_codes=['RENDERER_UNSUPPORTED']))
                # Every renderer must support an exact table (and an empty result).
                if all_options.safe_default_id not in {o.option_id for o in available}:
                    raise ValueError('renderer must support the safe table/empty fallback')
                return Eligibility(available, rejected, all_options.safe_default_id)

        domain = AvailableDomain()
        source_subject = domain.subject_for(reference)
        data_hash = 'sha256:' + hashlib.sha256(json.dumps(data.model_dump(mode='json'),
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        subject = SubjectRef.model_validate({**source_subject.model_dump(), 'attrs':{
            **source_subject.attrs, 'renderer':renderer.name, 'data_hash':data_hash}})
        decision = await self.engine.decide(domain, request_id=request_id, tenant_id=tenant_id,
            subject=subject,
            input={'result_ref':reference.model_dump(mode='json'), 'intent':intent.model_dump(mode='json'),
                   'profile':profile.model_dump(mode='json')},
            preference=intent.preference.value if intent.preference else None)
        try:
            rendering = renderer.compile(candidate_of(decision.option), data, reference, intent, title)
            if not isinstance(rendering, dict):
                raise ValueError('renderer returned invalid envelope')
            status = 'ready'
        except Exception:
            # Renderer-independent fallback, preserving the admitted values exactly.
            rendering = {'format':'decision-platform.table/1', 'columns':[f.name for f in data.fields],
                         'rows':[[row.get(f.name) for f in data.fields] for row in data.rows],
                         'disclosures':list(decision.option.disclosures),
                         'result_ref':reference.model_dump(mode='json')}
            status = 'table_fallback'
        return PresentationResult(decision, rendering, renderer.name, status)

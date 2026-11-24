"""One policy-selected package API for assessments, presentations and classification.

The host installs policies/handlers and supplies authenticated tenant identity. Request
data cannot install handlers, change policy, choose endpoints or supply credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..config import _read_config_json
from ..contracts.models import Ident
from .config import DomainPolicy, ResolvedPolicy, Settings
from .core.engine import Engine
from .persistence.store import Store
from .selectors.base import Selector
from .config import SelectorKind


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class DecisionPolicy(Contract):
    id: Ident
    version: str = Field(min_length=1, max_length=200)
    handler: Ident
    enabled: bool = True
    tenants: frozenset[str] | None = None
    timeout_seconds: float = Field(default=10, gt=0, le=300, allow_inf_nan=False)
    selection: DomainPolicy = Field(default_factory=DomainPolicy)


class DecisionRequest(Contract):
    policy: Ident
    policy_version: str = Field(min_length=1, max_length=200)
    request_id: Ident
    tenant_id: Ident
    input: dict[str, JsonValue]


class DecisionOutcome(Contract):
    status: Literal['complete', 'fallback', 'unavailable'] = 'complete'
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None


class DecisionResponse(DecisionOutcome):
    schema_version: Literal['decissions.response/1'] = 'decissions.response/1'
    request_id: str
    tenant_id: str
    policy: str
    policy_version: str
    policy_digest: str
    kind: str
    latency_ms: float


class DecisionHandler(Protocol):
    """Extension point: validate without I/O, then execute under the shared runtime."""
    kind: str

    def prepare(self, request: DecisionRequest, policy: DecisionPolicy) -> object: ...

    async def execute(self, prepared: object, request: DecisionRequest,
                      policy: DecisionPolicy, engine: Engine) -> DecisionOutcome: ...


def policy_digest(policy: DecisionPolicy) -> str:
    value = policy.model_dump(mode='json')
    if value['tenants'] is not None:
        value['tenants'] = sorted(value['tenants'])
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
                                                separators=(',', ':')).encode()).hexdigest()


def selection_policy(engine: Engine, domain, policy: DecisionPolicy) -> ResolvedPolicy:
    """Per-route overrides, then domain/global settings; all selection uses Engine."""
    base = engine.settings.policy(domain.name, domain.policy_defaults)
    fields = {**base.model_dump(), **policy.selection.model_dump(exclude_none=True)}
    fields['policy_ref'] = policy_digest(policy)
    fields['selector_deadline_ms'] = min(fields['selector_deadline_ms'], fields['deadline_ms'],
                                         max(1, int(policy.timeout_seconds * 1000)))
    return ResolvedPolicy.model_validate(fields)


class PolicyDocument(Contract):
    schema_version: Literal['decissions.policies/1'] = 'decissions.policies/1'
    policies: list[DecisionPolicy]


def load_decision_policies(path) -> list[DecisionPolicy]:
    """Load bounded JSON with duplicate-key rejection; no dynamic imports or secrets."""
    return PolicyDocument.model_validate(_read_config_json(path)).policies


class Runtime:
    """The single public runtime. Single and batch calls use the identical dispatch path."""

    def __init__(self, *, policies: Sequence[DecisionPolicy], handlers: Mapping[str, DecisionHandler],
                 settings: Settings | None = None, selectors: Mapping[SelectorKind, Selector] | None = None,
                 store: Store | None = None, max_batch_items: int = 500, concurrency: int = 16):
        if type(max_batch_items) is not int or type(concurrency) is not int or min(max_batch_items, concurrency) < 1:
            raise ValueError('batch size and concurrency must be positive integers')
        self._policies = {}
        self._handlers = dict(handlers)
        for value in policies:
            policy = DecisionPolicy.model_validate_json(value.model_dump_json())
            if policy.id in self._policies:
                raise ValueError('duplicate policy id')
            if policy.handler not in self._handlers:
                raise ValueError('policy references an unregistered handler')
            self._policies[policy.id] = policy
        self.engine = Engine(settings or Settings(), store, dict(selectors or {}))
        self.max_batch_items, self.concurrency = max_batch_items, concurrency

    def _prepare(self, value):
        request = DecisionRequest.model_validate(value)
        encoded = json.dumps(request.model_dump(mode='json'), allow_nan=False)
        if len(encoded.encode()) > 2_100_000:
            raise ValueError('request exceeds size limit')
        # Own the snapshot before any await, including inputs in a queued batch.
        request = DecisionRequest.model_validate_json(encoded)
        policy = self._policies.get(request.policy)
        if policy is None or policy.version != request.policy_version:
            raise ValueError('unknown policy or policy version mismatch')
        if not policy.enabled or (policy.tenants is not None and request.tenant_id not in policy.tenants):
            raise PermissionError('policy is not enabled for this tenant')
        handler = self._handlers[policy.handler]
        return request, policy, handler, handler.prepare(request, policy)

    async def _execute(self, prepared) -> DecisionResponse:
        request, policy, handler, value = prepared
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(handler.execute(value, request, policy, self.engine),
                                             timeout=policy.timeout_seconds)
            outcome = DecisionOutcome.model_validate(outcome)
        except asyncio.TimeoutError:
            outcome = DecisionOutcome(status='unavailable', error_code='DECISION_TIMEOUT')
        except Exception:
            # Programming/provider errors are never serialized with raw text or credentials.
            outcome = DecisionOutcome(status='unavailable', error_code='DECISION_UNAVAILABLE')
        return DecisionResponse(**outcome.model_dump(), request_id=request.request_id,
            tenant_id=request.tenant_id, policy=policy.id, policy_version=policy.version,
            policy_digest=policy_digest(policy), kind=handler.kind,
            latency_ms=(time.monotonic() - started) * 1000)

    async def decide(self, request: DecisionRequest | dict | None = None, **fields) -> DecisionResponse:
        if request is not None and fields:
            raise ValueError('provide a request or keyword fields, not both')
        return await self._execute(self._prepare(request if request is not None else fields))

    async def decide_many(self, requests: Sequence[DecisionRequest | dict]) -> list[DecisionResponse]:
        if len(requests) > self.max_batch_items:
            raise ValueError('batch exceeds configured limit')
        # Reject malformed inputs/policies across the batch before any provider is called.
        prepared = [self._prepare(request) for request in requests]
        identities = [(item[0].tenant_id, item[0].request_id) for item in prepared]
        if len(set(identities)) != len(identities):
            raise ValueError('batch request identities must be unique within each tenant')
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(item):
            async with semaphore:
                return await self._execute(item)

        return list(await asyncio.gather(*(one(item) for item in prepared)))

    async def drain(self) -> None:
        """Wait for configured shadow comparisons; host retains resource ownership."""
        await self.engine.drain()

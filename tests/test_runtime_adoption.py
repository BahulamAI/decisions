import asyncio
from types import SimpleNamespace

import pytest
from decissions.decisions.feedback import submit_feedback, review_feedback
from decissions.decisions.models import DecisionContext
from decissions.presentations import PresentationService, ProfileInput, ResultRef, Intent
from decissions.presentations.saved import compile_saved, SavedPresentationConflict
from decissions.presentations.validation import validate_snapshot
from decissions.runtime.core.engine import Engine
from decissions.runtime.config import Settings, SelectorKind
from decissions.runtime.selectors.rules import RulesSelector
from decissions.runtime.selectors.jev import CircuitBreaker
from decissions.runtime.shared import LocalState


def data():
    return ProfileInput(fields=[{"name":"group","role":"dimension","dtype":"string"},
        {"name":"count","role":"measure","dtype":"integer","semantics":"count","additive":True}],
        rows=[{"group":"A","count":4},{"group":"B","count":2}],total_rows=2,is_complete=True)


def reference():
    return ResultRef(result_id="r1",revision=1,content_hash="sha256:"+"a"*64)


class Store:
    def __init__(self): self.saved={}
    async def get_by_hash(self,tenant,key): return self.saved.get((tenant,key))
    async def save(self,record):
        return self.saved.setdefault((record.tenant_id,record.input_hash),record)


class Adapter:
    def __init__(self): self.engine=Engine(Settings(_env_file=None,rules_only=True),Store(),{SelectorKind.RULES:RulesSelector()})
    async def decide(self,domain,**kwargs):
        kwargs['input']=domain.input_model.model_validate(kwargs['input'])
        return await self.engine.decide(domain,DecisionContext(domain=domain.name,**kwargs))


def test_runtime_replay_and_tenant_separation():
    async def run():
        service=PresentationService(engine=Adapter())
        async def select(tenant):
            return await service.present(request_id="req",tenant_id=tenant,reference=reference(),data=data())
        first=await select("a"); repeat=await select("a"); other=await select("b")
        assert first.decision.decision_id==repeat.decision.decision_id
        assert repeat.decision.replayed
        assert other.decision.decision_id!=first.decision.decision_id
    asyncio.run(run())


def test_switch_preserves_values_and_rejects_mutation():
    async def run():
        result=await PresentationService().present(request_id="req",tenant_id="a",reference=reference(),data=data())
        alternate=result.decision.alternatives[0]
        kwargs=dict(record=result.decision,option_id=alternate.option_id,tenant_id="a",reference=reference(),data=data(),intent=Intent())
        _, snapshot=compile_saved(**kwargs)
        validate_snapshot(data(),snapshot)
        kwargs['data'].rows[0]['count']=999
        with pytest.raises(SavedPresentationConflict): compile_saved(**kwargs)
        with pytest.raises(ValueError): validate_snapshot(kwargs['data'],snapshot)
    asyncio.run(run())


def test_feedback_requires_review_and_known_option():
    async def run():
        result=await PresentationService().present(request_id="req",tenant_id="a",reference=reference(),data=data())
        record=result.decision
        feedback=submit_feedback(record,{"verdict":"misleading","severity":"critical"},actor="user")
        assert feedback['review_status']=='pending_review'
        reviewed=review_feedback(record,feedback,{"status":"confirmed_error"},reviewer="reviewer")
        assert reviewed['review_status']=='confirmed_error'
        with pytest.raises(ValueError): review_feedback(record,reviewed,{"status":"rejected"},reviewer="reviewer")
        with pytest.raises(ValueError): submit_feedback(record,{"verdict":"preferred","preferred_option":"invented"},actor="user")
    asyncio.run(run())


def test_rate_and_circuit_controls():
    async def run():
        state=LocalState(rate_per_s=0.01,burst=1)
        assert (await state.take_token('a','caller'))[0]
        assert not (await state.take_token('a','caller'))[0]
        assert (await state.take_token('b','caller'))[0]
    asyncio.run(run())
    breaker=CircuitBreaker(1,60)
    assert breaker.allow()
    breaker.record(False)
    assert not breaker.allow()


def test_reusable_evaluation_harness():
    from decissions.testing.presentations import Case, run, summarize
    from decissions.presentations.domain import PresentationDomain
    case=Case(case_id="generic",family="comparison",result_kind="dataset",true_intent="compare",
        difficulty="easy",intent_text="compare",audience="analyst",
        fields=[f.model_dump(mode="json") for f in data().fields],rows=data().rows,
        total_rows=2,is_complete=True,comparable_runs=True,acceptable=["category_metric_bar"],
        preferred="category_metric_bar")
    results=asyncio.run(run([case],SelectorKind.RULES,RulesSelector(),Settings(_env_file=None),PresentationDomain()))
    report=summarize(results)
    assert report['acceptable_rate']==1 and report['compile_failures']==0

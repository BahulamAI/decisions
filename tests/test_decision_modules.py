import asyncio
import json
import sys
import subprocess
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel
from decissions.decisions import DecisionDomain,DecisionEngine,Eligibility,Option,SelectorOutput,SubjectRef
from decissions.adapters.jev_decision import JevDecisionSelector
from decissions.presentations import PresentationService,ProfileInput,FieldSpec,ResultRef,Intent
from decissions.presentations.contract import ChartKind
from decissions.renderers.workplane.adapter import WorkplaneRenderer


def data():
    return ProfileInput(fields=[FieldSpec(name='category',role='dimension',dtype='string'),
        FieldSpec(name='amount',role='measure',dtype='integer',semantics='count',additive=True)],
        rows=[{'category':'private-label-a','amount':3},{'category':'private-label-b','amount':7}],
        is_complete=True,total_rows=2)


def ref():
    return ResultRef(result_id='result',revision=1,content_hash='sha256:'+'a'*64,result_kind='customer_defined_result')


def present(**kw):
    service=kw.pop('service',PresentationService())
    return asyncio.run(service.present(request_id='req',tenant_id=kw.pop('tenant','one'),
        reference=ref(),data=kw.pop('data',data()),**kw))


def test_rules_and_explicit_preference_compile_without_entitybi():
    result=present(intent=Intent(preference='pie'))
    assert result.decision.selection_source=='user'
    assert result.rendering['chart']['payload']['chart_type']=='pie'
    assert sorted(row[1] for row in result.rendering['rows'])==[3,7]
    assert result.decision.subject.kind=='data_result'


def test_ineligible_pie_is_not_forced_and_partial_coverage_survives():
    d=data().model_copy(update={'is_complete':False,'total_rows':10})
    result=present(data=d,intent=Intent(preference='pie'))
    assert result.decision.option.group!='category_share_pie'
    assert any('PARTIAL_COVERAGE' in x for x in result.decision.option.disclosures)
    assert any(x.group=='category_share_pie' for x in result.decision.ineligible)


class BrokenSelector:
    name='hosted_model'
    def release_for(self,domain):return 'broken/1'
    async def select(self,*a):raise RuntimeError('provider secret must not leak')


class WrongSelector(BrokenSelector):
    async def select(self,*a):return SelectorOutput(option_id='not-offered')


class SlowSelector(BrokenSelector):
    async def select(self,*a):await asyncio.sleep(1)


@pytest.mark.parametrize('selector,reason',[(BrokenSelector(),'provider_error'),(WrongSelector(),'invalid_response'),(SlowSelector(),'timeout')])
def test_provider_failures_return_exact_table(selector,reason):
    result=present(service=PresentationService(engine=DecisionEngine(selector=selector,timeout_seconds=.01)))
    assert result.decision.fallback_reason==reason
    assert result.decision.option.group=='detail_table'
    assert 'provider secret' not in result.decision.model_dump_json()
    assert result.rendering['rows']==[['private-label-a',3],['private-label-b',7]]


class OtherRenderer:
    name='other.table/1'
    def supports(self,c):return c.kind in (ChartKind.TABLE,ChartKind.TEXT) and c.chart_type is None
    def compile(self,c,data,ref,intent,title):return {'table':[dict(r) for r in data.rows]}


def test_alternative_renderer_filters_chart_candidates():
    result=present(service=PresentationService(renderer=OtherRenderer()))
    assert result.renderer=='other.table/1'
    assert result.decision.option.group=='detail_table'
    assert len(result.rendering['table'])==2
    assert any('RENDERER_UNSUPPORTED' in x.reason_codes for x in result.decision.ineligible)


def test_compiler_failure_is_explicit_and_retains_exact_values():
    class BrokenRenderer(OtherRenderer):
        def compile(self,*a):raise ValueError('bad renderer')
    result=present(service=PresentationService(renderer=BrokenRenderer()))
    assert result.rendering_status=='table_fallback'
    assert result.rendering['rows']==[['private-label-a',3],['private-label-b',7]]


@pytest.mark.parametrize('patch',[
    {'total_rows':1}, {'total_rows':3},
    {'rows':[{'category':'a','amount':float('nan')}]},
    {'rows':[{'category':'a','amount':2,'secret':'not declared'}]},
])
def test_invalid_input_never_reaches_selector(patch):
    with pytest.raises(ValueError):present(data=data().model_copy(update=patch))


def test_core_is_domain_neutral_and_tenant_scoped():
    class RoutingInput(BaseModel):
        large:bool
    class Routing(DecisionDomain):
        name='example.routing';version='1';input_model=RoutingInput;subject_kind='request'
        choice_instructions='Choose an eligible route'
        def eligibility(self,inp):
            return Eligibility([Option(option_id=n,group=n,description={'what':n}) for n in ('small','large')],[],'small')
        def rules_select(self,ctx,elig):return 'large' if ctx.input.large else 'small'
    async def run(tenant):
        return await DecisionEngine().decide(Routing(),request_id='req',tenant_id=tenant,
            subject=SubjectRef(kind='request',id='one'),input={'large':True})
    one,two=asyncio.run(run('one')),asyncio.run(run('two'))
    assert one.option_id=='large' and one.input_hash!=two.input_hash


def test_assessment_import_does_not_load_optional_decision_modules():
    subprocess.run([sys.executable,'-c',"import decissions,sys; assert 'decissions.decisions' not in sys.modules; assert 'decissions.presentations' not in sys.modules"],check=True)


def test_jev_receives_profile_and_choices_but_no_rows_or_labels():
    seen=[]
    def respond(req):
        body=json.loads(req.content);seen.append(body)
        ids=list(body['questions']['choice']['criteria'])
        return httpx.Response(200,json={'model':'configured-jev','answers':{
            'choice':{'type':'choice','choice':ids[0],'probabilities':{x:1 if x==ids[0] else 0 for x in ids}},
            'fit':{'type':'noul','noul':.9}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            selector=JevDecisionSelector(api_key='test-key',model='configured-jev',endpoint='http://gateway.test/decisions',client=client)
            return await PresentationService(engine=DecisionEngine(selector=selector)).present(
                request_id='r',tenant_id='t',reference=ref(),data=data(),intent=Intent(text='compare categories'))
    result=asyncio.run(run())
    assert result.decision.selection_source=='hosted_model'
    assert 'private-label' not in json.dumps(seen)
    assert seen[0]['state']['untrusted_user_request']=='compare categories'


def test_row_values_are_bound_to_receipt_even_if_host_reference_is_reused():
    first=present()
    changed=data();changed.rows[0]['amount']=9
    second=present(data=changed)
    assert first.decision.input_hash!=second.decision.input_hash


def test_nonfinite_provider_probabilities_cannot_be_served():
    class BadNumbers(BrokenSelector):
        async def select(self,domain,ctx,elig,budget):
            return {'option_id':elig.options[0].option_id,'provider_confidence':float('nan')}
    result=present(service=PresentationService(engine=DecisionEngine(selector=BadNumbers())))
    assert result.decision.selection_source=='fallback'

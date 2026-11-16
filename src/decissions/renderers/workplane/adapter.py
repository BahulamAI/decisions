"""First renderer adapter. Generic decisions do not import this module."""
from ...presentations.contract import ChartKind
from .compiler import compile_spec
from .chat_chart import BUILDABLE, chart_type_of


class WorkplaneRenderer:
    name = 'workplane.chat-chart/1'

    def supports(self, candidate):
        return candidate.chart_type in BUILDABLE or (candidate.chart_type is None and candidate.kind in (ChartKind.TABLE, ChartKind.TEXT))

    def compile(self, candidate, data, reference, intent, title):
        result = compile_spec(candidate, data.rows, reference, fields=data.fields,
                              title=title, audience=intent.audience, screen_reader=intent.screen_reader)
        if result['provenance']['chart_status'] == 'invalid':
            raise ValueError('Workplane rejected compiled chart')
        return result

"""Single policy-driven runtime; hosts supply identity, providers and persistence."""

def __getattr__(name):
    if name in {'Runtime', 'DecisionPolicy', 'DecisionRequest', 'DecisionResponse',
                'DecisionOutcome', 'load_decision_policies'}:
        from . import api
        return getattr(api, name)
    raise AttributeError(name)

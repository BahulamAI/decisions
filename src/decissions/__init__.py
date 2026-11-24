"""Reusable decision platform. Existing safety APIs remain available; optional decision and presentation modules are imported explicitly."""

from .context import ContextLimits, ContextTooLarge
from .assessment import (
    AssessmentInput, AssessmentStatus, RiskAssessment, RiskAssessor, RiskDefinition,
    RiskEvaluation, RiskEvaluator, RiskFamily, RiskQuestion, RiskRegistry, RiskScore,
)
from .questions import BinaryQuestion
from .registry import load_risk_registry, parse_risk_registry
from .config import CategoryDefinition, PolicyConfiguration, load_policy_json, parse_policy_config
from .engine import SafetyGate
from .models import (
    Action, AssessmentScope, Category, Evaluation, Mode, RecentTurn, RiskTier, SafetyDecision,
    SafetyInput, ScannerStatus, Signal, Stage,
)
from .policy import PolicyBundle, ThresholdBand
from .session import (
    EventOutcome, SessionConflict, SessionEvent, SessionSafetyState,
    SessionStore, SessionTracker, content_digest, state_from_record, state_to_record,
)

__all__ = [
    "AssessmentInput", "AssessmentStatus", "BinaryQuestion", "RiskAssessment", "RiskAssessor",
    "RiskDefinition", "RiskEvaluation", "RiskEvaluator", "RiskFamily", "RiskQuestion",
    "RiskRegistry", "RiskScore", "load_risk_registry", "parse_risk_registry",
    "Action", "AssessmentScope", "Category", "CategoryDefinition", "ContextLimits", "ContextTooLarge", "Evaluation",
    "Mode", "PolicyBundle", "PolicyConfiguration", "RecentTurn", "RiskTier", "SafetyDecision",
    "SafetyGate", "SafetyInput", "ScannerStatus", "Signal", "Stage", "ThresholdBand",
    "EventOutcome", "SessionConflict", "SessionEvent", "SessionSafetyState",
    "SessionStore", "SessionTracker", "content_digest",
    "state_from_record", "state_to_record", "load_policy_json", "parse_policy_config",
]

# Preserve dependency-free imports for already-integrated safety consumers.
def __getattr__(name):
    if name in {'Runtime', 'DecisionPolicy', 'DecisionRequest', 'DecisionResponse',
                'DecisionOutcome', 'load_decision_policies'}:
        from .runtime import api
        return getattr(api, name)
    if name in {'SafetyHandler', 'RiskAssessmentHandler', 'PresentationHandler', 'ClassificationHandler'}:
        from . import handlers
        return getattr(handlers, name)
    if name in {'DomainPolicy', 'SelectorKind', 'Settings'}:
        from .runtime import config
        return getattr(config, name)
    if name == 'JevSelector':
        from .runtime.selectors.jev import JevSelector
        return JevSelector
    raise AttributeError(name)

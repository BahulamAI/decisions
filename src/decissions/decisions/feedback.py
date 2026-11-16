"""Reusable review state machine. Hosts authorize reviewers and persist the returned metadata."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["acceptable", "preferred", "unacceptable", "misleading", "unsafe"]
    preferred_option: str | None = Field(default=None, max_length=200)
    severity: Literal["low", "medium", "critical"] | None = None


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed_error", "rejected"]
    intent: Literal["compare", "share", "trend", "distribution", "relationship", "detail", "other"] | None = None
    preferred_option: str | None = Field(default=None, max_length=200)


def submit_feedback(record, submitted, *, actor):
    body = FeedbackInput.model_validate(submitted)
    options = {o.option_id for o in [record.option, *record.alternatives]}
    if body.preferred_option is not None and body.preferred_option not in options:
        raise ValueError("Preferred option was not eligible")
    if not actor:
        raise ValueError("Authenticated actor is required")
    return {**body.model_dump(), "decision_id": record.decision_id, "actor": actor,
            "review_status": "pending_review"}


def review_feedback(record, feedback, submitted, *, reviewer):
    body = ReviewInput.model_validate(submitted)
    if feedback.get("review_status") != "pending_review":
        raise ValueError("Feedback is already reviewed")
    if feedback.get("decision_id") != record.decision_id or not reviewer:
        raise ValueError("Review identity does not match")
    preferred = body.preferred_option or feedback.get("preferred_option")
    if preferred is not None and preferred not in {o.option_id for o in [record.option, *record.alternatives]}:
        raise ValueError("Preferred option was not eligible")
    return {**feedback, "review_status": body.status, "review_intent": body.intent,
            "preferred_option": preferred, "reviewer": reviewer}

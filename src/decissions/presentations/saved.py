"""Recompile a previously eligible presentation; no fetching, reselection or storage."""
import hashlib
import json

from .domain import candidate_of, PresentationDomain
from ..renderers.workplane.adapter import WorkplaneRenderer


class SavedPresentationConflict(ValueError):
    pass


def compile_saved(*, record, option_id, tenant_id, reference, data, intent, title=None,
                  expected_scope=None, renderer=None):
    attrs = record.subject.attrs
    if (record.tenant_id != tenant_id or record.subject.id != reference.result_id
            or record.subject.revision != reference.revision
            or record.subject.content_hash != reference.content_hash
            or any(attrs.get(k) != v for k, v in (expected_scope or {}).items())):
        raise SavedPresentationConflict("Saved decision identity does not match")
    digest = "sha256:" + hashlib.sha256(json.dumps(data.model_dump(mode="json"),
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if attrs.get("data_hash") != digest or record.domain_version != PresentationDomain.version:
        raise SavedPresentationConflict("Saved data or policy revision does not match")
    option = next((o for o in [record.option, *record.alternatives] if o.option_id == option_id), None)
    if option is None:
        raise SavedPresentationConflict("Option was not eligible for this decision")
    renderer = renderer or WorkplaneRenderer()
    if attrs.get("renderer") != renderer.name or not renderer.supports(candidate_of(option)):
        raise SavedPresentationConflict("Saved renderer is unavailable")
    return option, renderer.compile(candidate_of(option), data, reference, intent, title)

"""Load and check the presentation catalog. It fails closed: any doubt stops the service from starting.

The catalog (catalog/chart_types.json) has two parts:
* `chart_types`: every type the vendored Workplane contract can draw, with our own descriptions, its family,
  the guards it needs, and whether it is enabled (offered) and selectable (a model may pick it).
* `templates`: the complete candidates the service can build today. Each names a chart type (or none, for
  tables), the fields it binds, its guards in order, and how its id, description and disclosures read.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .guards import GUARDS, READY_FAMILIES, mandatory_guards

Family = Literal["point", "multi", "sample", "heatmap", "graph", "hierarchy", "target", "interval", "other"]
Role = Literal["dimension", "measure", "time", "identifier", "series", "source", "target", "relation",
               "evidence|status"]
_PACKAGE = resources.files("decissions")
DEFAULT_CATALOG = _PACKAGE / "presentations/catalog/chart_types.json"
DEFAULT_CONTRACT = _PACKAGE / "renderers/workplane/contracts/chat-chart-v1.json"


class CatalogError(RuntimeError):
    """The catalog is unsafe or inconsistent. The service must not start."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Text(Strict):
    what: Annotated[str, Field(min_length=1, max_length=200)]
    best_for: Annotated[str, Field(min_length=1, max_length=200)]
    not_for: Annotated[str, Field(min_length=1, max_length=200)]


class ChartTypeSpec(Text):
    chart_type: str
    family: Family
    shape: str  # the contract's series shape, for readers; builders arrive with each family
    guards: list[str] = []
    variant_of: str | None = None
    never: bool = False  # excluded by this built-in catalog policy
    enabled: bool = False  # may be offered in Switch view
    rules_selectable: bool = True  # the rules may choose it (D11 "rules select"); only meaningful when enabled
    selectable: bool = False  # a model may choose it (D11 "Jev select")
    disclosures: list[str] = []
    verified_with: str | None = None  # renderer and package version it was verified against


class Requirement(Strict):
    role: Role
    missing: str = ""  # reason code when no field has this role
    optional: bool = False  # bound when present (a series field), never a reason to refuse

    @model_validator(mode="after")
    def _code_unless_optional(self) -> Requirement:
        if not self.optional and not self.missing:
            raise ValueError(f"required role {self.role} needs a missing reason code")
        return self


class GuardUse(Strict):
    model_config = ConfigDict(extra="allow", frozen=True)  # extra keys are the guard's parameters
    name: str


class TemplateSpec(Strict):
    template_id: str
    version: Annotated[int, Field(ge=1)]
    chart_type: str | None  # None for tables and the empty state, which are always drawable
    kind: Literal["bar", "line", "pie", "table", "text"]  # how the /1 fallback block is drawn
    prefix: str = ""
    candidate_id: str  # format string over the bound field names
    requires: list[Requirement] = []
    guards: list[GuardUse] = []
    bindings: dict[str, str] = {}  # binding name -> role, or "@all_fields"
    ordering: str | None = None
    top_n: int | None = None
    empty_state: bool = False
    description: Text
    disclosures: list[str] = []  # literals (may use {top_n}, {omitted}) or @coverage, @measure_semantics, @paginated


class Catalog(Strict):
    catalog_version: str
    contract_sha256: str
    chart_types: list[ChartTypeSpec]
    templates: list[TemplateSpec]

    @model_validator(mode="after")
    def _unique(self) -> Catalog:
        for label, ids in (("chart type", [c.chart_type for c in self.chart_types]),
                           ("template", [t.template_id for t in self.templates])):
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"duplicate {label}s: {sorted(dupes)}")
        return self

    def chart_type(self, name: str) -> ChartTypeSpec:
        return next(c for c in self.chart_types if c.chart_type == name)

    def active_templates(self) -> list[TemplateSpec]:
        """Templates whose chart type is enabled (tables and the empty state are always available)."""
        return [t for t in self.templates if t.chart_type is None or self.chart_type(t.chart_type).enabled]


def _fail(problems: list[str]) -> None:
    if problems:
        raise CatalogError("Presentation catalog rejected:\n  - " + "\n  - ".join(problems))


def check(catalog: Catalog, contract: dict[str, Any], contract_sha256: str) -> None:
    problems: list[str] = []
    if catalog.contract_sha256 != contract_sha256:
        problems.append("contract_sha256 does not match the vendored contract; review the new contract, then "
                        "update the catalog")
    drawable = set(contract["tool_schema"]["properties"]["chart_type"]["enum"])
    listed = {c.chart_type for c in catalog.chart_types}
    if listed - drawable:
        problems.append(f"chart types not in the vendored contract: {sorted(listed - drawable)}")
    if drawable - listed:
        problems.append(f"contract chart types missing from the catalog: {sorted(drawable - listed)}")
    for c in catalog.chart_types:
        unknown = [g for g in c.guards if g not in GUARDS]
        if unknown:
            problems.append(f"{c.chart_type}: unknown guards {unknown}")
        floor = set(mandatory_guards(c.family, c.chart_type))
        if not floor <= set(c.guards):
            problems.append(f"{c.chart_type}: drops mandatory guards {sorted(floor - set(c.guards))}")
        if c.variant_of is not None and c.variant_of not in listed:
            problems.append(f"{c.chart_type}: variant_of names unknown type {c.variant_of}")
        if c.never and (c.enabled or c.selectable):
            problems.append(f"{c.chart_type}: marked never, so it cannot be enabled or selectable")
        if c.selectable and not (c.enabled and c.rules_selectable):
            problems.append(f"{c.chart_type}: selectable requires enabled and rules_selectable")
        if c.enabled and not c.verified_with:
            problems.append(f"{c.chart_type}: enabled without verified_with")
        if c.enabled and c.family not in READY_FAMILIES:
            problems.append(f"{c.chart_type}: family {c.family} has no reviewed guards yet, so it cannot be enabled")
    for t in catalog.templates:
        if t.chart_type is not None and t.chart_type not in listed:
            problems.append(f"template {t.template_id}: unknown chart type {t.chart_type}")
            continue
        unknown = [g.name for g in t.guards if g.name not in GUARDS]
        if unknown:
            problems.append(f"template {t.template_id}: unknown guards {unknown}")
        if t.chart_type is not None:
            needed = set(catalog.chart_type(t.chart_type).guards)
            if not needed <= {g.name for g in t.guards}:
                problems.append(f"template {t.template_id}: drops guards required by {t.chart_type}: "
                                f"{sorted(needed - {g.name for g in t.guards})}")
    if sum(t.empty_state for t in catalog.templates) != 1:
        problems.append("exactly one template must be the empty state")
    _fail(problems)


def load_catalog(catalog_path: Path | None = None, contract_path: Path | None = None) -> Catalog:
    catalog_file = catalog_path or DEFAULT_CATALOG
    contract_file = contract_path or DEFAULT_CONTRACT
    contract_bytes = contract_file.read_bytes()
    try:
        catalog = Catalog.model_validate(json.loads(catalog_file.read_text()))
    except (ValidationError, ValueError) as e:
        raise CatalogError(f"Presentation catalog rejected: {e}") from None
    check(catalog, json.loads(contract_bytes), hashlib.sha256(contract_bytes).hexdigest())
    return catalog

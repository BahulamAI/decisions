"""Typed settings. Every knob comes from the environment (prefix DECISION_), never from request bodies."""

from __future__ import annotations

from decissions.contracts.compat import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BUILTIN_CONFIGS = Path(__file__).parent / "domain_configs"


class SelectorKind(StrEnum):
    RULES = "rules"
    HOSTED = "hosted_model"
    IN_HOUSE = "in_house_model"


class DomainPolicy(BaseModel):
    """Per-domain overrides. Anything left None inherits the domain's own defaults, then the global setting."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    serving: SelectorKind | None = None
    shadows: list[SelectorKind] | None = None
    min_auto_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    min_fit_probability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    deadline_ms: int | None = Field(default=None, gt=0)
    selector_deadline_ms: int | None = Field(default=None, gt=0)
    on_selector_failure: Literal["rules", "safe_default"] | None = None  # HIGH-risk domains always safe_default
    external_inference_tenants: list[str] | None = None
    allowed_groups: list[str] | None = None  # kill switch per option family: only these may be offered
    tenant_allowed_groups: dict[str, list[str]] | None = None  # the same, per tenant
    # "when_unclear": ask the model only when the domain says the rules are not enough (e.g. no clear intent words).
    model_routing: Literal["always", "when_unclear"] | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DECISION_", env_file=None, extra="ignore", populate_by_name=True,
                                      env_ignore_empty=True)  # an empty value means unset, so aliases fall through

    env: str = "local"
    log_level: str = "INFO"

    # Selection policy
    serving_selector: SelectorKind = SelectorKind.RULES
    shadow_selectors: list[SelectorKind] = Field(default_factory=list)
    total_deadline_ms: int = 2000
    min_auto_confidence: float = 0.35  # calibrated per selector release; see evals/
    min_fit_probability: float = 0.30  # Jev companion Noul gate
    external_inference_tenants: list[str] = Field(
        default_factory=list, description="Tenants whose policy allows hosted inference"
    )
    # D12 review lane: tenants that agreed to have de-identified review items judged by an external LLM.
    # Empty (the default) means nothing is ever sent.
    review_opt_in_tenants: list[str] = Field(
        default_factory=list, description="Tenants whose pending reviews may be judged offline by an LLM"
    )

    # Per-domain policy, e.g. {"agent.tool_gate": {"serving": "rules", "min_auto_confidence": 0.8}}
    domain_policies: dict[str, DomainPolicy] = Field(default_factory=dict)
    # Kill switch: every domain serves rules and runs no shadows, whatever else is configured.
    rules_only: bool = False
    # The five intent yes/no questions sent with each Jev Choice. Off saves tokens; evals keep them on.
    jev_signal_questions: bool = True
    batch_max_items: int = 500
    batch_concurrency: int = 16

    # Hosted Jev via OpenRouter Decisions API
    # Reuses the organisation's existing OPENROUTER_API_KEY if DECISION_OPENROUTER_API_KEY is not set.
    openrouter_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DECISION_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"))
    # Shown in the OpenRouter dashboard so this service's spend is separable on a shared org key.
    openrouter_app_title: str = "Decissions"
    openrouter_app_url: str = "https://github.com/BahulamAI/decissions"
    openrouter_base_url: str = "https://openrouter.ai/api/alpha/decisions"
    jev_model: str = "typesafe/jev-1.14"  # pinned; never the ~latest alias in production
    jev_max_retries: int = 1
    # Cost controls for development and testing (see selectors/jev_cost.py)
    jev_cache_mode: str = Field(default="off", pattern="^(off|record|replay)$")
    jev_cache_dir: str = ".jev-cache"
    jev_daily_budget_usd: float | None = None  # soft daily cap, shared through Redis when it is configured
    jev_tenant_daily_budget_usd: float | None = None  # the same cap per tenant, so one tenant cannot use it all
    jev_shared_cache_ttl_s: int = Field(default=0, ge=0)  # Redis response cache; forced off outside local/test (D9)
    jev_price_per_m_input_usd: float = 0.04  # for estimates only; billing uses usage.cost
    shadow_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    breaker_failure_threshold: int = 5
    breaker_reset_s: float = 30.0

    # Shared state (D13): budget, breaker, rate limits and cache in Redis when DECISION_REDIS_URL is set. When Redis
    # is unreachable each replica falls back to its own in-process state and Jev spend stops (rules serve).
    redis_url: SecretStr | None = None
    redis_prefix: str = Field(default="decision:", pattern=r"^[a-z][a-z0-9_.-]*:$")
    redis_timeout_ms: int = Field(default=50, ge=5)  # a slow Redis must never eat the 2 s decision budget
    rate_limit_per_s: float = Field(default=0.0, ge=0)  # per tenant and caller; 0 = off
    rate_limit_burst: int = Field(default=40, ge=1)

    # Observability: traces export only when an OTLP endpoint is configured; ids are always logged.
    otel_exporter_endpoint: str | None = None
    service_name: str = "decissions"
    cache_max_entries: int = 10_000

    # Request bounds (defence against oversized payloads)
    max_rows: int = 5_000
    max_columns: int = 64
    max_intent_chars: int = 1_000

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @model_validator(mode="after")
    def _jev_cache_only_in_development(self) -> Settings:
        # Recording writes the untrusted user question to disk; replay serves stale answers. Neither belongs
        # outside local development and tests, whatever the environment says.
        if self.env not in ("local", "test") and self.jev_cache_mode != "off":
            self.jev_cache_mode = "off"
        if self.env not in ("local", "test"):
            self.jev_shared_cache_ttl_s = 0  # D9: no Jev response cache outside local and CI
        return self

    def policy(self, domain: str, defaults: dict[str, Any] | None = None) -> ResolvedPolicy:
        """Configured per-domain value, else the domain's own default, else the global setting."""
        p, d = self.domain_policies.get(domain, DomainPolicy()), defaults or {}

        def pick(field: str, global_value: Any) -> Any:
            value = getattr(p, field)
            return value if value is not None else d.get(field, global_value)

        deadline = pick("deadline_ms", self.total_deadline_ms)
        return ResolvedPolicy(
            serving=pick("serving", self.serving_selector),
            shadows=pick("shadows", self.shadow_selectors),
            min_auto_confidence=pick("min_auto_confidence", self.min_auto_confidence),
            min_fit_probability=pick("min_fit_probability", self.min_fit_probability),
            deadline_ms=deadline,
            selector_deadline_ms=min(pick("selector_deadline_ms", deadline), deadline),
            on_selector_failure=pick("on_selector_failure", "safe_default"),
            external_inference_tenants=pick("external_inference_tenants", self.external_inference_tenants),
            allowed_groups=pick("allowed_groups", None),
            tenant_allowed_groups=pick("tenant_allowed_groups", None),
            model_routing=pick("model_routing", "always"),
        )


class ResolvedPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    serving: SelectorKind
    shadows: list[SelectorKind]
    min_auto_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    min_fit_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    deadline_ms: int = Field(gt=0)
    selector_deadline_ms: int = Field(gt=0)
    on_selector_failure: Literal["rules", "safe_default"]
    external_inference_tenants: list[str]
    allowed_groups: list[str] | None = None
    tenant_allowed_groups: dict[str, list[str]] | None = None
    model_routing: str = "always"
    policy_ref: str | None = None  # Common API route identity, included in replay keys.

    def groups_for(self, tenant_id: str) -> list[str] | None:
        if self.tenant_allowed_groups and tenant_id in self.tenant_allowed_groups:
            return self.tenant_allowed_groups[tenant_id]
        return self.allowed_groups


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Deterministic, provider-neutral model selection.

This module decides which configured model should handle a request and how
fallbacks are chosen after categorized failures. It is intentionally pure:

- no file, network, environment, or Keychain access
- no credential values are accepted or stored; only credential references
- every transition is a pure function of the current state and the failure

Selection rules
---------------
- A policy has exactly one primary model and an ordered list of unique
  fallback models.
- A fallback switch is allowed only for eligible failure categories:
  ``network``, ``timeout``, ``rate_limit``, ``billing``,
  ``service_unavailable``.
- ``auth``, ``invalid_request``, ``model_not_found``, ``task_failure``, and
  ``unknown`` failures never trigger a fallback.
- An attempted model is never selected again within the same run.
- The number of switches is capped by ``max_switches``; once the cap is hit or
  no candidate remains, the state stays on the current model and
  ``exhausted`` becomes ``True``.

State model
-----------
- ``generation`` is the 1-based attempt ordinal: ``begin()`` returns
  generation 1 and every eligible switch increments it by exactly one.
- ``attempted`` is the ordered tuple of model ids already tried in this run.
"""

from __future__ import annotations

import enum
import types
import urllib.parse
from dataclasses import dataclass, replace
from typing import FrozenSet, Optional, Sequence, Tuple, Union


_HTTP_SCHEMES = frozenset({"http", "https"})
_INLINE_SECRET_PREFIXES = ("sk-", "ghp_", "xoxb-", "AKIA", "Bearer ", "-----BEGIN")
_MAX_INLINE_SECRET_LENGTH = 64


class FailureCategory(str, enum.Enum):
    """Categorized failure outcomes reported by a model provider call."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    BILLING = "billing"
    SERVICE_UNAVAILABLE = "service_unavailable"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    MODEL_NOT_FOUND = "model_not_found"
    TASK_FAILURE = "task_failure"
    UNKNOWN = "unknown"


ELIGIBLE_CATEGORIES: FrozenSet[FailureCategory] = frozenset(
    {
        FailureCategory.NETWORK,
        FailureCategory.TIMEOUT,
        FailureCategory.RATE_LIMIT,
        FailureCategory.BILLING,
        FailureCategory.SERVICE_UNAVAILABLE,
    }
)


@dataclass(frozen=True)
class CredentialRef:
    """Reference to a stored credential; never holds the credential value."""

    kind: str
    name: str


@dataclass(frozen=True)
class Model:
    """Configured model. ``id`` is the stable local identifier."""

    id: str
    provider_id: str
    remote_name: str
    base_url: str
    protocol: str
    credential: CredentialRef


@dataclass(frozen=True)
class Failure:
    """A categorized failure with an optional human-readable detail."""

    category: FailureCategory
    message: Optional[str] = None


@dataclass(frozen=True)
class SelectionState:
    """Deterministic snapshot of an in-progress selection run."""

    active_model_id: Optional[str]
    generation: int
    attempted: Tuple[str, ...]
    switch_count: int
    last_failure: Optional[Failure]
    exhausted: bool = False


class ValidationError(ValueError):
    """Raised when a model or policy configuration is invalid."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: Tuple[str, ...] = tuple(errors)
        super().__init__("; ".join(self.errors))


def _is_valid_base_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in _HTTP_SCHEMES and bool(parsed.netloc)


def _looks_like_inline_secret(value: str) -> bool:
    """Heuristic: an inline credential value is not a valid reference name."""
    if not value:
        return False
    if len(value) > _MAX_INLINE_SECRET_LENGTH:
        return True
    if any(char.isspace() for char in value):
        return True
    if "=" in value:
        return True
    lowered = value.lower()
    return any(lowered.startswith(prefix.lower()) for prefix in _INLINE_SECRET_PREFIXES)


def validate_model(model: Model) -> Tuple[str, ...]:
    """Return all validation errors for a single model (empty means valid)."""
    errors = []
    if not model.id:
        errors.append("model id must not be empty")
    if not model.provider_id:
        errors.append("provider id must not be empty")
    if not model.remote_name:
        errors.append("remote model name must not be empty")
    if not model.protocol:
        errors.append("protocol must not be empty")
    if not _is_valid_base_url(model.base_url):
        errors.append("base URL must be an absolute http(s) URL")
    if not model.credential.kind:
        errors.append("credential reference kind must not be empty")
    if not model.credential.name:
        errors.append("credential reference name must not be empty")
    if _looks_like_inline_secret(model.credential.kind) or _looks_like_inline_secret(
        model.credential.name
    ):
        errors.append("credential reference must not contain an inline credential value")
    return tuple(errors)


def validate_models(models: Sequence[Model]) -> Tuple[str, ...]:
    """Validate every model and reject duplicate local ids."""
    errors = []
    seen = set()
    for model in models:
        errors.extend(validate_model(model))
        if model.id:
            if model.id in seen:
                errors.append("duplicate model id: {!r}".format(model.id))
            seen.add(model.id)
    return tuple(errors)


def validate_policy(
    models: Sequence[Model],
    primary_id: str,
    fallback_ids: Sequence[str],
    max_switches: int,
) -> Tuple[str, ...]:
    """Return all policy-level validation errors (empty means valid)."""
    errors = list(validate_models(models))
    known_ids = {model.id for model in models if model.id}
    if not primary_id:
        errors.append("primary model id must not be empty")
    elif primary_id not in known_ids:
        errors.append("primary model {!r} is not configured".format(primary_id))
    if max_switches < 0:
        errors.append("max switches must be >= 0")
    seen_fallbacks = set()
    for fallback_id in fallback_ids:
        if not fallback_id:
            errors.append("fallback model id must not be empty")
            continue
        if fallback_id not in known_ids:
            errors.append("fallback model {!r} is not configured".format(fallback_id))
        if fallback_id == primary_id:
            errors.append("primary model must not appear in the fallback list")
        if fallback_id in seen_fallbacks:
            errors.append("fallback model {!r} appears more than once".format(fallback_id))
        seen_fallbacks.add(fallback_id)
    return tuple(errors)


def is_eligible_category(category: Union[FailureCategory, str]) -> bool:
    """True when a failure of this category may trigger a fallback switch."""
    return _coerce_category(category) in ELIGIBLE_CATEGORIES


def _coerce_category(category: Union[FailureCategory, str]) -> FailureCategory:
    if isinstance(category, FailureCategory):
        return category
    if isinstance(category, str):
        try:
            return FailureCategory(category)
        except ValueError:
            raise ValueError("unknown failure category: {!r}".format(category)) from None
    raise TypeError(
        "failure category must be FailureCategory or str, got {}".format(
            type(category).__name__
        )
    )


class SelectionPolicy:
    """Immutable, validated selection policy over a set of configured models."""

    def __init__(
        self,
        models: Sequence[Model],
        primary_id: str,
        fallback_ids: Sequence[str],
        max_switches: int,
    ) -> None:
        errors = validate_policy(models, primary_id, fallback_ids, max_switches)
        if errors:
            raise ValidationError(errors)
        self._models = {model.id: model for model in models}
        self._primary = self._models[primary_id]
        self._fallbacks = tuple(self._models[fallback_id] for fallback_id in fallback_ids)
        self._max_switches = max_switches

    @property
    def models(self) -> types.MappingProxyType:
        """Read-only view of all configured models keyed by local id."""
        return types.MappingProxyType(self._models)

    @property
    def primary(self) -> Model:
        return self._primary

    @property
    def fallbacks(self) -> Tuple[Model, ...]:
        return self._fallbacks

    @property
    def max_switches(self) -> int:
        return self._max_switches

    def model(self, model_id: str) -> Model:
        return self._models[model_id]

    def candidates(self, attempted: Sequence[str]) -> Tuple[Model, ...]:
        """Ordered fallbacks not yet attempted; primary is never a candidate."""
        attempted_set = frozenset(attempted)
        return tuple(
            fallback for fallback in self._fallbacks if fallback.id not in attempted_set
        )

    def begin(self) -> SelectionState:
        """Initial state: primary model active, generation 1, no switches."""
        return SelectionState(
            active_model_id=self._primary.id,
            generation=1,
            attempted=(self._primary.id,),
            switch_count=0,
            last_failure=None,
            exhausted=False,
        )

    def record_failure(
        self,
        state: SelectionState,
        category: Union[FailureCategory, str],
        message: Optional[str] = None,
    ) -> SelectionState:
        """Record a categorized failure and return the next deterministic state."""
        if not isinstance(state, SelectionState):
            raise TypeError(
                "state must be a SelectionState, got {}".format(type(state).__name__)
            )
        if state.active_model_id not in self._models:
            raise ValueError(
                "state references unknown model {!r}".format(state.active_model_id)
            )
        failure = Failure(category=_coerce_category(category), message=message)
        if failure.category not in ELIGIBLE_CATEGORIES:
            # Ineligible failures never switch; they only update the last failure.
            return replace(state, last_failure=failure)
        if state.switch_count >= self._max_switches:
            return replace(state, last_failure=failure, exhausted=True)
        candidates = self.candidates(state.attempted)
        if not candidates:
            return replace(state, last_failure=failure, exhausted=True)
        candidate = candidates[0]
        return replace(
            state,
            active_model_id=candidate.id,
            generation=state.generation + 1,
            attempted=state.attempted + (candidate.id,),
            switch_count=state.switch_count + 1,
            last_failure=failure,
            exhausted=False,
        )


__all__ = [
    "CredentialRef",
    "ELIGIBLE_CATEGORIES",
    "Failure",
    "FailureCategory",
    "Model",
    "SelectionPolicy",
    "SelectionState",
    "ValidationError",
    "is_eligible_category",
    "validate_model",
    "validate_models",
    "validate_policy",
]

"""Load and render provider/model manifests without storing credential values."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from model_selection import CredentialRef, Model, SelectionPolicy, ValidationError
from platform_runtime import python_command_toml, toml_path_escape


IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SUPPORTED_CODEX_PROTOCOLS = frozenset({"responses"})
SUPPORTED_UPSTREAM_PROTOCOLS = frozenset({"openai_responses", "anthropic_messages"})
UPSTREAM_OPENAI_RESPONSES = "openai_responses"
UPSTREAM_ANTHROPIC_MESSAGES = "anthropic_messages"
FORBIDDEN_AUTH_FIELDS = frozenset({"api_key", "key", "secret", "token", "value"})
DEFAULT_ADAPTER_LISTEN_PORT = 18766
DEFAULT_ADAPTER_MAX_OUTPUT_TOKENS = 16384
SUPPORTED_REASONING_LEVELS = (
    ("low", "Lower-cost bounded work"),
    ("medium", "Balanced repository work"),
    ("high", "Complex analysis and implementation"),
    ("max", "Maximum reasoning depth"),
)
SUPPORTED_REASONING_EFFORTS = frozenset(
    effort for effort, _description in SUPPORTED_REASONING_LEVELS
)
MAX_RETRY_COUNT = 10
MIN_STREAM_IDLE_TIMEOUT_MS = 1_000
MAX_STREAM_IDLE_TIMEOUT_MS = 3_600_000


@dataclass(frozen=True)
class AuthSpec:
    kind: str
    name: str
    account: str = "codex"
    header: Optional[str] = None


@dataclass(frozen=True)
class AdapterSpec:
    kind: str
    listen_host: str
    listen_port: int
    max_output_tokens: int

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str
    protocol: str
    auth: AuthSpec
    adapter: Optional[AdapterSpec] = None
    upstream_protocol: str = UPSTREAM_OPENAI_RESPONSES
    request_max_retries: int = 2
    stream_max_retries: int = 2
    stream_idle_timeout_ms: int = 300000

    @property
    def effective_base_url(self) -> str:
        return self.adapter.base_url if self.adapter is not None else self.base_url


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider_id: str
    remote_model: str
    agent: str
    reasoning_effort: str
    display_name: str
    context_window: int
    max_context_window: int
    effective_context_window_percent: int
    supports_parallel_tool_calls: bool
    supports_search_tool: bool


@dataclass(frozen=True)
class ModelManifest:
    providers: Mapping[str, ProviderSpec]
    models: Mapping[str, ModelSpec]
    selection: SelectionPolicy

    def normalized_selection(self) -> dict:
        return {
            "schema_version": 1,
            "selection": {
                "primary": self.selection.primary.id,
                "fallbacks": [model.id for model in self.selection.fallbacks],
                "max_switches": self.selection.max_switches,
            },
            "models": {
                model.id: {
                    "agent": self.models[model.id].agent,
                    "provider": model.provider_id,
                    "remote_model": model.remote_name,
                    "catalog": catalog_filename(self.models[model.id]),
                    "context_window": self.models[model.id].context_window,
                    "max_context_window": self.models[model.id].max_context_window,
                    "effective_context_window_percent": self.models[
                        model.id
                    ].effective_context_window_percent,
                }
                for model in self.selection.models.values()
            },
        }


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError((f"{label} must be an object",))
    return value


def _require_string(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError((f"{label}.{key} must be a non-empty string",))
    return value.strip()


def _require_int(mapping: Mapping[str, object], key: str, default: int) -> int:
    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError((f"{key} must be a non-negative integer",))
    return value


def _require_bounded_int(
    mapping: Mapping[str, object],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    value = mapping.get(key, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ValidationError(
            (f"{label}.{key} must be an integer between {minimum} and {maximum}",)
        )
    return value


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValidationError((f"{label} must match {IDENTIFIER.pattern}",))
    return value


def _require_positive_int(mapping: Mapping[str, object], key: str, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError((f"{label}.{key} must be a positive integer",))
    return value


def _optional_bool(
    mapping: Mapping[str, object], key: str, default: bool, label: str
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError((f"{label}.{key} must be a boolean",))
    return value


def _reasoning_effort(mapping: Mapping[str, object], label: str) -> str:
    value = mapping.get("reasoning_effort", "high")
    if not isinstance(value, str) or value not in SUPPORTED_REASONING_EFFORTS:
        allowed = ", ".join(effort for effort, _description in SUPPORTED_REASONING_LEVELS)
        raise ValidationError((f"{label}.reasoning_effort must be one of: {allowed}",))
    return value


def _validate_provider_base_url(
    value: str,
    label: str,
    allowed_schemes: frozenset[str],
) -> None:
    scheme_label = "HTTPS" if allowed_schemes == frozenset({"https"}) else "HTTP(S)"
    contains_invalid_character = any(
        character.isspace() or ord(character) < 32 for character in value
    ) or "\\" in value
    if contains_invalid_character:
        raise ValidationError((f"{label} must be an absolute {scheme_label} URL",))
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValidationError(
            (f"{label} must be an absolute {scheme_label} URL",)
        ) from error
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValidationError(
            (
                f"{label} must be an absolute {scheme_label} URL without credentials, "
                "query, or fragment",
            )
        )


def _parse_auth(raw: object, label: str) -> AuthSpec:
    auth = _require_mapping(raw, label)
    forbidden = FORBIDDEN_AUTH_FIELDS.intersection(auth)
    if forbidden:
        raise ValidationError(
            (f"{label} contains forbidden inline credential fields: {sorted(forbidden)}",)
        )
    kind = _require_string(auth, "type", label)
    if kind == "keychain":
        return AuthSpec(
            kind=kind,
            name=_require_string(auth, "service", label),
            account=str(auth.get("account", "codex")),
        )
    if kind == "env":
        return AuthSpec(kind=kind, name=_require_string(auth, "variable", label))
    if kind == "env_header":
        header = _require_string(auth, "header", label)
        if "\n" in header or "\r" in header:
            raise ValidationError((f"{label}.header must be a single HTTP header name",))
        return AuthSpec(
            kind=kind,
            name=_require_string(auth, "variable", label),
            header=header,
        )
    raise ValidationError((f"{label}.type must be 'keychain', 'env', or 'env_header'",))


def _parse_adapter(
    raw: object, label: str, *, require_type: bool = True
) -> Optional[AdapterSpec]:
    if raw is None:
        return None
    adapter = _require_mapping(raw, label)
    kind = (
        _require_string(adapter, "type", label)
        if require_type
        else str(adapter.get("type", UPSTREAM_ANTHROPIC_MESSAGES))
    )
    if kind != "anthropic_messages":
        raise ValidationError((f"{label}.type must be 'anthropic_messages'",))
    listen_host = str(adapter.get("listen_host", "127.0.0.1"))
    if listen_host not in ("127.0.0.1", "localhost"):
        raise ValidationError((f"{label}.listen_host must be loopback",))
    listen_port = _require_int(adapter, "listen_port", DEFAULT_ADAPTER_LISTEN_PORT)
    if not 1 <= listen_port <= 65535:
        raise ValidationError((f"{label}.listen_port must be between 1 and 65535",))
    max_output_tokens = _require_int(
        adapter, "max_output_tokens", DEFAULT_ADAPTER_MAX_OUTPUT_TOKENS
    )
    if not 1 <= max_output_tokens <= 64000:
        raise ValidationError((f"{label}.max_output_tokens must be between 1 and 64000",))
    return AdapterSpec(
        kind=kind,
        listen_host=listen_host,
        listen_port=listen_port,
        max_output_tokens=max_output_tokens,
    )


def _default_anthropic_adapter(listen_port: int = DEFAULT_ADAPTER_LISTEN_PORT) -> AdapterSpec:
    """V2 Anthropic Messages providers need no explicit adapter block."""
    return AdapterSpec(
        kind=UPSTREAM_ANTHROPIC_MESSAGES,
        listen_host="127.0.0.1",
        listen_port=listen_port,
        max_output_tokens=DEFAULT_ADAPTER_MAX_OUTPUT_TOKENS,
    )


def _upstream_protocol(
    item: Mapping[str, object], label: str, adapter: Optional[AdapterSpec]
) -> str:
    """Resolve the canonical upstream protocol, normalizing V1 declarations."""
    raw = item.get("upstream_protocol")
    if raw is None:
        return (
            UPSTREAM_ANTHROPIC_MESSAGES
            if adapter is not None
            else UPSTREAM_OPENAI_RESPONSES
        )
    if not isinstance(raw, str) or raw not in SUPPORTED_UPSTREAM_PROTOCOLS:
        allowed = ", ".join(sorted(SUPPORTED_UPSTREAM_PROTOCOLS))
        raise ValidationError(
            (f"{label}.upstream_protocol must be one of: {allowed}",)
        )
    return raw


def load_manifest(path: Path) -> ModelManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError((f"cannot read manifest {path}: {error}",)) from error
    root = _require_mapping(raw, "manifest")
    schema_version = root.get("schema_version")
    if schema_version not in (1, 2):
        raise ValidationError(("schema_version must be 1 or 2",))

    provider_entries = root.get("providers")
    model_entries = root.get("models")
    if not isinstance(provider_entries, list) or not provider_entries:
        raise ValidationError(("providers must be a non-empty array",))
    if not isinstance(model_entries, list) or not model_entries:
        raise ValidationError(("models must be a non-empty array",))

    providers: Dict[str, ProviderSpec] = {}
    adapter_addresses: Dict[Tuple[str, int], str] = {}
    for index, entry in enumerate(provider_entries):
        label = f"providers[{index}]"
        item = _require_mapping(entry, label)
        provider_id = _identifier(_require_string(item, "id", label), "provider id")
        if provider_id in providers:
            raise ValidationError((f"duplicate provider id: {provider_id}",))
        base_url = _require_string(item, "base_url", label)
        if schema_version == 2 and "adapter" in item:
            raise ValidationError(
                (
                    f"{label}.adapter is a schema_version 1 compatibility field; "
                    "use local_adapter with upstream_protocol in schema_version 2",
                )
            )
        adapter_key = "local_adapter" if schema_version == 2 else "adapter"
        adapter = _parse_adapter(
            item.get(adapter_key),
            f"{label}.{adapter_key}",
            require_type=schema_version == 1,
        )
        upstream_protocol = _upstream_protocol(item, label, adapter)
        if upstream_protocol == UPSTREAM_OPENAI_RESPONSES and adapter is not None:
            raise ValidationError(
                (
                    f"{label}.upstream_protocol 'openai_responses' conflicts with "
                    "adapter; the local adapter is only used when upstream_protocol "
                    "is 'anthropic_messages'",
                )
            )
        if upstream_protocol == UPSTREAM_ANTHROPIC_MESSAGES and adapter is None:
            listen_port = DEFAULT_ADAPTER_LISTEN_PORT
            while ("127.0.0.1", listen_port) in adapter_addresses:
                listen_port += 1
                if listen_port > 65535:
                    raise ValidationError(("no loopback adapter port is available",))
            adapter = _default_anthropic_adapter(listen_port)
        allowed_schemes = (
            frozenset({"https"})
            if adapter is not None
            else frozenset({"http", "https"})
        )
        _validate_provider_base_url(
            base_url,
            f"{label}.base_url",
            allowed_schemes,
        )
        if adapter is not None:
            normalized_host = (
                "127.0.0.1" if adapter.listen_host == "localhost" else adapter.listen_host
            )
            address = (normalized_host, adapter.listen_port)
            conflicting_provider = adapter_addresses.get(address)
            if conflicting_provider is not None:
                raise ValidationError(
                    (
                        f"adapter listen address conflict: providers {conflicting_provider} "
                        f"and {provider_id} both use loopback:{adapter.listen_port}",
                    )
                )
            adapter_addresses[address] = provider_id
        provider = ProviderSpec(
            id=provider_id,
            name=_require_string(item, "name", label),
            base_url=base_url,
            protocol=_require_string(item, "protocol", label),
            auth=_parse_auth(item.get("auth"), f"{label}.auth"),
            adapter=adapter,
            upstream_protocol=upstream_protocol,
            request_max_retries=_require_bounded_int(
                item, "request_max_retries", 2, 1, MAX_RETRY_COUNT, label
            ),
            stream_max_retries=_require_bounded_int(
                item, "stream_max_retries", 2, 1, MAX_RETRY_COUNT, label
            ),
            stream_idle_timeout_ms=_require_bounded_int(
                item,
                "stream_idle_timeout_ms",
                300000,
                MIN_STREAM_IDLE_TIMEOUT_MS,
                MAX_STREAM_IDLE_TIMEOUT_MS,
                label,
            ),
        )
        providers[provider_id] = provider

    specs: Dict[str, ModelSpec] = {}
    agents: Dict[str, str] = {}
    selection_models = []
    for index, entry in enumerate(model_entries):
        label = f"models[{index}]"
        item = _require_mapping(entry, label)
        model_id = _identifier(_require_string(item, "id", label), "model id")
        if model_id in specs:
            raise ValidationError((f"duplicate model id: {model_id}",))
        provider_id = _identifier(
            _require_string(item, "provider", label), "model provider id"
        )
        if provider_id not in providers:
            raise ValidationError(
                (f"model {model_id} references unknown provider {provider_id}",)
            )
        agent = _identifier(_require_string(item, "agent", label), "agent name")
        conflicting_model = agents.get(agent)
        if conflicting_model is not None:
            raise ValidationError(
                (
                    f"duplicate agent name: {agent} is used by models "
                    f"{conflicting_model} and {model_id}",
                )
            )
        agents[agent] = model_id
        context_window = _require_positive_int(item, "context_window", label)
        max_context_window = _require_positive_int(
            item, "max_context_window", label
        )
        effective_percent = _require_positive_int(
            item, "effective_context_window_percent", label
        )
        if max_context_window < context_window:
            raise ValidationError(
                (f"model {model_id} max_context_window must be >= context_window",)
            )
        if effective_percent > 100:
            raise ValidationError(
                (f"model {model_id} effective_context_window_percent must be <= 100",)
            )
        spec = ModelSpec(
            id=model_id,
            provider_id=provider_id,
            remote_model=_require_string(item, "remote_model", label),
            agent=agent,
            reasoning_effort=_reasoning_effort(item, label),
            display_name=str(item.get("display_name", model_id)),
            context_window=context_window,
            max_context_window=max_context_window,
            effective_context_window_percent=effective_percent,
            supports_parallel_tool_calls=_optional_bool(
                item, "supports_parallel_tool_calls", True, f"models[{index}]"
            ),
            supports_search_tool=_optional_bool(
                item, "supports_search_tool", False, f"models[{index}]"
            ),
        )
        specs[model_id] = spec
        provider = providers[provider_id]
        selection_models.append(
            Model(
                id=model_id,
                provider_id=provider_id,
                remote_name=spec.remote_model,
                base_url=provider.effective_base_url,
                protocol=provider.protocol,
                credential=CredentialRef(provider.auth.kind, provider.auth.name),
            )
        )

    models_by_adapter_provider: Dict[str, list[str]] = {
        provider.id: [] for provider in providers.values() if provider.adapter is not None
    }
    for spec in specs.values():
        if spec.provider_id in models_by_adapter_provider:
            models_by_adapter_provider[spec.provider_id].append(spec.id)
    for provider_id, model_ids in models_by_adapter_provider.items():
        if len(model_ids) != 1:
            raise ValidationError(
                (
                    f"adapter provider {provider_id} must define exactly one model catalog; "
                    f"found {len(model_ids)} models",
                )
            )

    selection = _require_mapping(root.get("selection"), "selection")
    primary = _require_string(selection, "primary", "selection")
    fallbacks = selection.get("fallbacks", [])
    if not isinstance(fallbacks, list) or not all(
        isinstance(item, str) for item in fallbacks
    ):
        raise ValidationError(("selection.fallbacks must be an array of strings",))
    policy = SelectionPolicy(
        selection_models,
        primary,
        tuple(fallbacks),
        _require_int(selection, "max_switches", len(fallbacks)),
    )
    return ModelManifest(providers=providers, models=specs, selection=policy)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_provider(
    provider: ProviderSpec,
    credential_command: Optional[Sequence[str]] = None,
) -> str:
    lines = [
        f"[model_providers.{provider.id}]",
        f"name = {_toml_string(provider.name)}",
        f"base_url = {_toml_string(provider.effective_base_url)}",
        f"wire_api = {_toml_string(provider.protocol)}",
        "supports_websockets = false",
        f"request_max_retries = {provider.request_max_retries}",
        f"stream_max_retries = {provider.stream_max_retries}",
        f"stream_idle_timeout_ms = {provider.stream_idle_timeout_ms}",
    ]
    if provider.auth.kind == "env":
        lines.append(f"env_key = {_toml_string(provider.auth.name)}")
    elif provider.auth.kind == "env_header":
        lines.append(
            "env_http_headers = { "
            + _toml_string(provider.auth.header or "")
            + " = "
            + _toml_string(provider.auth.name)
            + " }"
        )
    else:
        command = "/usr/bin/security"
        args = [
            "find-generic-password",
            "-a",
            provider.auth.account,
            "-s",
            provider.auth.name,
            "-w",
        ]
        if credential_command:
            command = credential_command[0]
            args = [
                *credential_command[1:],
                "get",
                "--account",
                provider.auth.account,
                "--service",
                provider.auth.name,
            ]
        lines.extend(
            [
                "",
                f"[model_providers.{provider.id}.auth]",
                f"command = {_toml_string(command)}",
                "args = " + json.dumps(args, ensure_ascii=False),
                "timeout_ms = 5000",
                "refresh_interval_ms = 0",
            ]
        )
    return "\n".join(lines) + "\n"


def render_agent(template: str, codex_home: Path, model: ModelSpec) -> str:
    catalog_line = "model_catalog_json = " + _toml_string(
        str(codex_home / "models" / catalog_filename(model))
    )
    replacements = {
        "__AGENT_NAME__": model.agent,
        "__MODEL_ID__": model.id,
        "__REMOTE_MODEL__": model.remote_model,
        "__PROVIDER_ID__": model.provider_id,
        "__REASONING_EFFORT__": model.reasoning_effort,
        "__PYTHON_COMMAND__": python_command_toml(),
        "__CODEX_HOME__": toml_path_escape(str(codex_home)),
        "__MODEL_CATALOG_LINE__": catalog_line,
    }
    rendered = template
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    return rendered


def catalog_filename(model: ModelSpec) -> str:
    """Namespace generated catalogs so legacy and provider-specific models can coexist."""
    return f"{model.provider_id}--{model.id}.json"


def render_model_catalog(model: ModelSpec) -> bytes:
    """Render model metadata from the manifest so context limits are not source constants."""
    digest = hashlib.sha256(
        (
            f"{model.id}:{model.remote_model}:{model.context_window}:"
            f"{model.max_context_window}:{model.effective_context_window_percent}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    catalog = {
        "models": [{
            "slug": model.remote_model,
            "prefer_websockets": False,
            "support_verbosity": False,
            "default_verbosity": "low",
            "apply_patch_tool_type": "freeform",
            "web_search_tool_type": "text",
            "input_modalities": ["text"],
            "supports_image_detail_original": False,
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_parallel_tool_calls": model.supports_parallel_tool_calls,
            "tool_mode": None,
            "multi_agent_version": "v2",
            "use_responses_lite": False,
            "include_skills_usage_instructions": False,
            "auto_review_model_override": None,
            "context_window": model.context_window,
            "max_context_window": model.max_context_window,
            "effective_context_window_percent": model.effective_context_window_percent,
            "auto_compact_token_limit": None,
            "comp_hash": digest,
            "reasoning_summary_format": "experimental",
            "default_reasoning_summary": "none",
            "display_name": model.display_name,
            "description": f"{model.display_name} configured by the subagent provider manifest.",
            "default_reasoning_level": model.reasoning_effort,
            "supported_reasoning_levels": [
                {"effort": effort, "description": description}
                for effort, description in SUPPORTED_REASONING_LEVELS
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "minimal_client_version": "0.146.0",
            "supported_in_api": True,
            "availability_nux": None,
            "upgrade": None,
            "priority": 1,
            "model_messages": {
                "instructions_template": "Use the supplied Codex tools and follow workspace instructions.",
                "instructions_variables": {
                    "personality_default": "",
                    "personality_friendly": "",
                    "personality_pragmatic": "",
                },
                "approvals": None,
            },
            "experimental_supported_tools": [],
            "supports_search_tool": model.supports_search_tool,
            "default_service_tier": None,
            "supports_reasoning_summaries": False,
            "base_instructions": "Use the supplied Codex tools for repository work.",
        }]
    }
    return (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def unsupported_protocols(manifest: ModelManifest) -> Tuple[str, ...]:
    return tuple(
        provider.id
        for provider in manifest.providers.values()
        if provider.protocol not in SUPPORTED_CODEX_PROTOCOLS
    )

"""Read/write gitmate TOML config; secrets live in keyring, never in files."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import keyring
import tomli_w

APP_NAME = "gitmate"
SERVICE_NAME = "gitmate"
API_KEY_ACCOUNT = "api-key"
CONFIG_ENV_VAR = "GITMATE_CONFIG_DIR"

# Default model ID. This is the literal google-genai SDK identifier. Gemini
# model IDs churn fast (2.0 Flash is retired; Flash-Lite previews deprecate
# within months), so keep this configurable and re-check before assuming it
# still resolves.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_COMMIT_STYLE = "conventional"

#: Context window limits for known models; users override via `max_context_tokens`.
DEFAULT_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-3.5-flash-lite": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-flash": 1_048_576,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
}
DEFAULT_RESERVED_OUTPUT_TOKENS = 2048
DEFAULT_TEMPLATE_OVERHEAD = 500


class ConfigError(Exception):
    """Raised when the config file exists but cannot be parsed."""


class UnknownModelError(ConfigError):
    """Raised when a model has no configured context window."""


@dataclass
class GitmateConfig:
    """User settings; the API key is stored separately via SecretStore."""

    model: str = DEFAULT_MODEL
    commit_style: str = DEFAULT_COMMIT_STYLE
    budget_cap_usd: float | None = None
    ignore_globs: list[str] = field(default_factory=list)
    max_context_tokens: int | None = None
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS
    template_overhead: int = DEFAULT_TEMPLATE_OVERHEAD
    cache_dir: str | None = None


class SecretStore(Protocol):
    """Key storage behind an interface so tests can substitute a fake."""

    def set_secret(self, account: str, secret: str) -> None:
        """Persist a secret."""
        ...

    def get_secret(self, account: str) -> str | None:
        """Return a secret, or None when absent."""
        ...


class KeyringSecretStore:
    """OS credential store via keyring (macOS Keychain, etc.)."""

    def set_secret(self, account: str, secret: str) -> None:
        """Persist a secret in the OS credential store."""
        keyring.set_password(SERVICE_NAME, account, secret)

    def get_secret(self, account: str) -> str | None:
        """Return a secret from the OS store, or None when absent."""
        return keyring.get_password(SERVICE_NAME, account)


class InMemorySecretStore:
    """In-memory SecretStore for tests; never touches the OS store."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def set_secret(self, account: str, secret: str) -> None:
        """Persist a secret in memory."""
        self._secrets[account] = secret

    def get_secret(self, account: str) -> str | None:
        """Return a secret from memory, or None when absent."""
        return self._secrets.get(account)


def config_dir(base: Path | None = None) -> Path:
    """Resolve the config dir; tests override it via arg or env var."""
    if base is not None:
        return base
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".config" / APP_NAME


def config_path(base: Path | None = None) -> Path:
    """Full path of config.toml."""
    return config_dir(base) / "config.toml"


def get_context_window(cfg: GitmateConfig) -> int:
    """Resolve the model context window limit, checking overrides first."""
    if cfg.max_context_tokens is not None:
        return cfg.max_context_tokens
    if cfg.model in DEFAULT_MODEL_CONTEXT_WINDOWS:
        return DEFAULT_MODEL_CONTEXT_WINDOWS[cfg.model]
    raise UnknownModelError(
        f"unknown model {cfg.model!r} with no context window configured. "
        "Set 'max_context_tokens' in config.toml or use a known model."
    )


def load_config(path: Path | None = None) -> GitmateConfig:
    """Load config.toml; a missing file or missing fields fall back to defaults."""
    resolved = path if path is not None else config_path()
    if not resolved.exists():
        return GitmateConfig()
    try:
        raw: dict[str, Any] = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot parse {resolved}: {exc}") from exc
    model = raw.get("model", DEFAULT_MODEL)
    if not isinstance(model, str):
        raise ConfigError(f"cannot parse {resolved}: 'model' must be a string")
    style = raw.get("commit_style", DEFAULT_COMMIT_STYLE)
    if not isinstance(style, str):
        raise ConfigError(f"cannot parse {resolved}: 'commit_style' must be a string")
    budget = raw.get("budget_cap_usd")
    if budget is None:
        cap: float | None = None
    elif isinstance(budget, bool) or not isinstance(budget, (int, float)):
        raise ConfigError(f"cannot parse {resolved}: 'budget_cap_usd' must be a number")
    else:
        cap = float(budget)
    globs = raw.get("ignore_globs", [])
    if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
        raise ConfigError(f"cannot parse {resolved}: 'ignore_globs' must be a string list")

    max_ctx_raw = raw.get("max_context_tokens")
    if max_ctx_raw is None:
        max_ctx: int | None = None
    elif isinstance(max_ctx_raw, bool) or not isinstance(max_ctx_raw, int) or max_ctx_raw <= 0:
        raise ConfigError(
            f"cannot parse {resolved}: 'max_context_tokens' must be a positive integer"
        )
    else:
        max_ctx = max_ctx_raw

    reserved_raw = raw.get("reserved_output_tokens", DEFAULT_RESERVED_OUTPUT_TOKENS)
    if isinstance(reserved_raw, bool) or not isinstance(reserved_raw, int) or reserved_raw <= 0:
        raise ConfigError(
            f"cannot parse {resolved}: 'reserved_output_tokens' must be a positive integer"
        )
    reserved = reserved_raw

    overhead_raw = raw.get("template_overhead", DEFAULT_TEMPLATE_OVERHEAD)
    if isinstance(overhead_raw, bool) or not isinstance(overhead_raw, int) or overhead_raw <= 0:
        raise ConfigError(
            f"cannot parse {resolved}: 'template_overhead' must be a positive integer"
        )
    overhead = overhead_raw

    if max_ctx is not None and max_ctx <= (reserved + overhead):
        raise ConfigError(
            f"cannot parse {resolved}: 'max_context_tokens' ({max_ctx}) must be greater "
            f"than 'reserved_output_tokens' + 'template_overhead' ({reserved + overhead})"
        )

    cache_dir_raw = raw.get("cache_dir")
    if cache_dir_raw is None:
        cache_dir: str | None = None
    elif not isinstance(cache_dir_raw, str) or not cache_dir_raw:
        raise ConfigError(f"cannot parse {resolved}: 'cache_dir' must be a non-empty string")
    else:
        cache_dir = cache_dir_raw

    return GitmateConfig(
        model=model,
        commit_style=style,
        budget_cap_usd=cap,
        ignore_globs=globs,
        max_context_tokens=max_ctx,
        reserved_output_tokens=reserved,
        template_overhead=overhead,
        cache_dir=cache_dir,
    )


def save_config(cfg: GitmateConfig, path: Path | None = None) -> None:
    """Write config.toml, creating the config dir if needed."""
    resolved = path if path is not None else config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"model": cfg.model, "commit_style": cfg.commit_style}
    if cfg.budget_cap_usd is not None:
        data["budget_cap_usd"] = cfg.budget_cap_usd
    if cfg.ignore_globs:
        data["ignore_globs"] = cfg.ignore_globs
    if cfg.max_context_tokens is not None:
        data["max_context_tokens"] = cfg.max_context_tokens
    if cfg.reserved_output_tokens != DEFAULT_RESERVED_OUTPUT_TOKENS:
        data["reserved_output_tokens"] = cfg.reserved_output_tokens
    if cfg.template_overhead != DEFAULT_TEMPLATE_OVERHEAD:
        data["template_overhead"] = cfg.template_overhead
    if cfg.cache_dir is not None:
        data["cache_dir"] = cfg.cache_dir
    resolved.write_text(tomli_w.dumps(data), encoding="utf-8")

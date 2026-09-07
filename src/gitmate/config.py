"""Read/write gitmate TOML config; secrets live in keyring, never in files."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import keyring
import tomli_w

APP_NAME = "gitmate"
SERVICE_NAME = "gitmate"
API_KEY_ACCOUNT = "api-key"
CONFIG_ENV_VAR = "GITMATE_CONFIG_DIR"

# Opaque default; exact provider model IDs are verified in Phase 3.
DEFAULT_MODEL = "gemini-flash"
DEFAULT_COMMIT_STYLE = "conventional"


class ConfigError(Exception):
    """Raised when the config file exists but cannot be parsed."""


@dataclass
class GitmateConfig:
    """User settings; the API key is stored separately via SecretStore."""

    model: str = DEFAULT_MODEL
    commit_style: str = DEFAULT_COMMIT_STYLE
    budget_cap_usd: float | None = None


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
    return GitmateConfig(model=model, commit_style=style, budget_cap_usd=cap)


def save_config(cfg: GitmateConfig, path: Path | None = None) -> None:
    """Write config.toml, creating the config dir if needed."""
    resolved = path if path is not None else config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"model": cfg.model, "commit_style": cfg.commit_style}
    if cfg.budget_cap_usd is not None:
        data["budget_cap_usd"] = cfg.budget_cap_usd
    resolved.write_text(tomli_w.dumps(data), encoding="utf-8")

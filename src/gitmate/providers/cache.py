"""Caching wrapper that short-circuits provider calls on a hash key."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from diskcache import Cache  # type: ignore[import-untyped]

from gitmate.providers.base import LLMProvider, LLMResponse

#: Bumped in Phase 4 when real prompt templates land; kept separate so those
#: templates can thread the same key without migration. Different templates =
#: different (and intentionally colder) cache entries.
DEFAULT_TEMPLATE_VERSION = "1"


def default_cache_dir() -> Path:
    """XDG cache dir for gitmate, overridable via config.cache_dir."""
    override = os.environ.get("GITMATE_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "gitmate"


def cache_key(prompt: str, model: str, template_version: str) -> str:
    """Stable hash of the inputs that change a response's correctness."""
    material = f"{template_version}\x00{model}\x00{prompt}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CachedProvider:
    """LLMProvider that consults diskcache before calling the wrapped provider."""

    def __init__(
        self,
        provider: LLMProvider,
        cache_dir: Path | str | None = None,
        ttl: float | None = None,
        template_version: str = DEFAULT_TEMPLATE_VERSION,
    ) -> None:
        self._provider = provider
        resolved = str(cache_dir) if cache_dir is not None else str(default_cache_dir())
        self._cache = Cache(resolved)
        self._ttl = ttl
        self._template_version = template_version

    def generate(self, prompt: str, model: str) -> LLMResponse:
        """Return a cached response when present, else generate and store."""
        key = cache_key(prompt, model, self._template_version)
        cached: LLMResponse | None = self._cache.get(key, default=None)
        if cached is not None:
            return cached
        response = self._provider.generate(prompt, model)
        self._cache.set(key, response, expire=self._ttl)
        return response

    def close(self) -> None:
        """Close the backing cache."""
        self._cache.close()

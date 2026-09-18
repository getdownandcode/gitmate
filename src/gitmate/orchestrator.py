"""Central LLM generation pipeline with token budgeting, chunking, caching, and fallback."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from gitmate import config as config_mod
from gitmate.chunking import summarize_omitted_diffs
from gitmate.config import API_KEY_ACCOUNT, GitmateConfig, SecretStore
from gitmate.diff_extractor import FileDiff
from gitmate.metrics import calculate_cost
from gitmate.prompt import load_template, render_prompt
from gitmate.providers.base import LLMProvider, ProviderUnavailable
from gitmate.providers.cache import CachedProvider, cache_key
from gitmate.providers.gemini import GeminiProvider
from gitmate.token_budget import (
    BudgetStrategy,
    GeminiTokenCounter,
    TokenBudgetError,
    TokenBudgetManager,
    TokenCounter,
)


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of generation, tracking fallback, tokens, and estimated cost."""

    text: str
    is_fallback: bool
    model: str
    fallback_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool = False
    estimated_cost_usd: float = 0.0


def run_generation_pipeline(
    diffs: list[FileDiff],
    cfg: GitmateConfig,
    template_name: str,
    fallback_generator: Callable[[], str],
    template_vars: dict[str, str] | None = None,
    empty_diff_message: str | None = None,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: SecretStore | None = None,
    bypass_cache: bool = False,
) -> GenerationResult:
    """Execute unified generation: token budgeting, chunking, caching, LLM call, and fallback."""
    if empty_diff_message is not None and not diffs:
        return GenerationResult(
            text=empty_diff_message,
            is_fallback=False,
            model=cfg.model,
        )

    store = secret_store if secret_store is not None else config_mod.KeyringSecretStore()
    api_key = store.get_secret(API_KEY_ACCOUNT) or os.environ.get("GEMINI_API_KEY")

    active_template = load_template(template_name)
    version_key = active_template.version_key

    active_provider: LLMProvider
    if provider is not None:
        if bypass_cache and isinstance(provider, CachedProvider):
            active_provider = provider.underlying
        else:
            active_provider = provider
    else:
        base_provider = GeminiProvider(api_key=api_key)
        if bypass_cache:
            active_provider = base_provider
        else:
            active_provider = CachedProvider(
                provider=base_provider,
                cache_dir=cfg.cache_dir,
                template_version=version_key,
            )

    active_counter = counter if counter is not None else GeminiTokenCounter(api_key=api_key)
    try:
        budget_mgr = TokenBudgetManager.from_config(cfg, active_counter)
        decision = budget_mgr.assess(diffs)
    except TokenBudgetError as exc:
        if console is not None:
            console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")
        return GenerationResult(
            text=fallback_generator(),
            is_fallback=True,
            model=cfg.model,
            fallback_reason=f"Token budgeting failed: {exc}",
        )

    total_input_tokens = 0
    total_output_tokens = 0
    chunk_cost = 0.0

    try:
        if decision.strategy is BudgetStrategy.NEEDS_CHUNKING:
            if not decision.omitted_diffs:
                if console is not None:
                    console.print(
                        "[yellow]⚠ Diff exceeds token budget, using template fallback[/yellow]"
                    )
                return GenerationResult(
                    text=fallback_generator(),
                    is_fallback=True,
                    model=cfg.model,
                    fallback_reason=decision.summary_note,
                )

            merged_summaries, chunk_in, chunk_out = summarize_omitted_diffs(
                decision.omitted_diffs,
                active_provider,
                cfg.model,
            )
            total_input_tokens += chunk_in
            total_output_tokens += chunk_out
            if chunk_in or chunk_out:
                chunk_cost = calculate_cost(
                    model=cfg.model,
                    tokens_in=chunk_in or None,
                    tokens_out=chunk_out or None,
                    cache_hit=False,
                    fallback_used=False,
                    free_tier=cfg.free_tier,
                )

            summary_note = (
                f"{decision.summary_note}\n\nSummaries of large omitted files:\n{merged_summaries}"
            )
            patch_chunks = [d.patch_text for d in decision.included_diffs if d.patch_text]
            diff_text = "\n".join(patch_chunks)
        else:
            patch_chunks = [d.patch_text for d in decision.included_diffs if d.patch_text]
            diff_text = "\n".join(patch_chunks)
            summary_note = decision.summary_note

        render_kwargs: dict[str, str] = {
            "diff": diff_text,
            "summary_note": summary_note,
        }
        if template_vars:
            render_kwargs.update(template_vars)

        prompt_text, _ = render_prompt(template_name, **render_kwargs)

        cache_hit = False
        if not bypass_cache and isinstance(active_provider, CachedProvider):
            key = cache_key(prompt_text, cfg.model, active_provider._template_version)
            cache_hit = active_provider._cache.get(key, default=None) is not None

        resp = active_provider.generate(prompt=prompt_text, model=cfg.model)
        if resp.input_tokens is not None:
            total_input_tokens += resp.input_tokens
        if resp.output_tokens is not None:
            total_output_tokens += resp.output_tokens

        final_cost = calculate_cost(
            model=resp.model,
            tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens,
            cache_hit=cache_hit,
            fallback_used=False,
            free_tier=cfg.free_tier,
        )
        cost = round(chunk_cost + final_cost, 6)

        return GenerationResult(
            text=resp.text,
            is_fallback=False,
            model=resp.model,
            input_tokens=total_input_tokens or None,
            output_tokens=total_output_tokens or None,
            cache_hit=cache_hit,
            estimated_cost_usd=cost,
        )

    except ProviderUnavailable as exc:
        if console is not None:
            console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")

        return GenerationResult(
            text=fallback_generator(),
            is_fallback=True,
            model=cfg.model,
            input_tokens=total_input_tokens or None,
            output_tokens=total_output_tokens or None,
            estimated_cost_usd=chunk_cost,
            fallback_reason=str(exc),
        )

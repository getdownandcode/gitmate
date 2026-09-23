"""Central LLM generation pipeline with token budgeting, chunking, caching, and fallback."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from gitmate import config as config_mod
from gitmate.chunking import summarize_omitted_diffs
from gitmate.config import API_KEY_ACCOUNT, GitmateConfig, SecretStore
from gitmate.diff_extractor import FileDiff
from gitmate.metrics import calculate_cost, get_monthly_spend, record_invocation
from gitmate.prompt import load_template, render_prompt
from gitmate.providers.base import LLMProvider, ProviderUnavailable
from gitmate.providers.cache import CachedProvider, cache_key
from gitmate.providers.gemini import GeminiProvider
from gitmate.token_budget import (
    BudgetStrategy,
    GeminiTokenCounter,
    HeuristicTokenCounter,
    TokenBudgetError,
    TokenBudgetManager,
    TokenCounter,
)

logger = logging.getLogger("gitmate.orchestrator")


class BudgetCapExceededError(Exception):
    """Raised when monthly LLM spend has reached or exceeded the configured budget cap."""


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


def safe_record_telemetry(
    command: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_hit: bool,
    latency_ms: int | None,
    fallback_used: bool,
    estimated_cost_usd: float | None = None,
    free_tier: bool = False,
) -> None:
    """Record invocation to metrics database without propagating errors."""
    try:
        record_invocation(
            command=command,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
            estimated_cost_usd=estimated_cost_usd,
            free_tier=free_tier,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record telemetry: %s", exc)


def check_budget_cap(cfg: GitmateConfig, console: Console | None = None) -> None:
    """Check whether monthly spend has exceeded or is near the configured cap.

    Raises BudgetCapExceededError if spend is at or above cap.
    """
    if cfg.budget_cap_usd is None:
        return

    monthly_spend = get_monthly_spend()
    if monthly_spend >= cfg.budget_cap_usd:
        if console is not None:
            console.print(
                f"[red]error:[/red] Monthly budget cap exceeded "
                f"(${monthly_spend:.2f} >= ${cfg.budget_cap_usd:.2f}). "
                "Aborting to prevent further LLM spend.\n"
                "To adjust or remove the cap, run: gitmate config set budget_cap_usd <new_limit_or_none>"
            )
        raise BudgetCapExceededError(
            f"Monthly budget cap exceeded (${monthly_spend:.2f} >= ${cfg.budget_cap_usd:.2f})."
        )

    if monthly_spend >= 0.8 * cfg.budget_cap_usd and console is not None:
        console.print(
            f"[yellow]warning: Monthly spend has reached {int(monthly_spend / cfg.budget_cap_usd * 100)}% "
            f"of budget cap (${monthly_spend:.2f} / ${cfg.budget_cap_usd:.2f}).[/yellow]"
        )


def run_generation_pipeline(
    diffs: list[FileDiff],
    cfg: GitmateConfig,
    template_name: str,
    fallback_generator: Callable[[], str],
    template_vars: dict[str, str] | None = None,
    empty_diff_message: str | None = None,
    command: str | None = None,
    provider: LLMProvider | None = None,
    counter: TokenCounter | None = None,
    console: Console | None = None,
    secret_store: SecretStore | None = None,
    bypass_cache: bool = False,
    hook_mode: bool = False,
) -> GenerationResult:
    """Execute unified generation: token budgeting, chunking, caching, LLM call, and fallback."""
    if empty_diff_message is not None and not diffs:
        return GenerationResult(
            text=empty_diff_message,
            is_fallback=False,
            model=cfg.model,
        )

    check_budget_cap(cfg, console=console)

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
        base_provider = GeminiProvider(api_key=api_key, hook_mode=hook_mode)
        if bypass_cache:
            active_provider = base_provider
        else:
            active_provider = CachedProvider(
                provider=base_provider,
                cache_dir=cfg.cache_dir,
                template_version=version_key,
            )

    active_counter: TokenCounter
    if counter is not None:
        active_counter = counter
    elif provider is not None:
        active_counter = HeuristicTokenCounter()
    else:
        active_counter = GeminiTokenCounter(api_key=api_key, hook_mode=hook_mode)

    t0 = time.perf_counter()
    gen_result: GenerationResult

    try:
        try:
            budget_mgr = TokenBudgetManager.from_config(cfg, active_counter)
            decision = budget_mgr.assess(diffs)
        except TokenBudgetError as exc:
            if console is not None:
                console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")
            gen_result = GenerationResult(
                text=fallback_generator(),
                is_fallback=True,
                model=cfg.model,
                fallback_reason=f"Token budgeting failed: {exc}",
            )
            return gen_result

        total_input_tokens = 0
        total_output_tokens = 0
        chunk_cost = 0.0

        if decision.strategy is BudgetStrategy.NEEDS_CHUNKING:
            if hook_mode:
                gen_result = GenerationResult(
                    text=fallback_generator(),
                    is_fallback=True,
                    model=cfg.model,
                    fallback_reason="Hook time budget does not allow chunked generation.",
                )
                return gen_result
            if not decision.omitted_diffs:
                if console is not None:
                    console.print(
                        "[yellow]⚠ Diff exceeds token budget, using template fallback[/yellow]"
                    )
                gen_result = GenerationResult(
                    text=fallback_generator(),
                    is_fallback=True,
                    model=cfg.model,
                    fallback_reason=decision.summary_note,
                )
                return gen_result

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

        gen_result = GenerationResult(
            text=resp.text,
            is_fallback=False,
            model=resp.model,
            input_tokens=total_input_tokens or None,
            output_tokens=total_output_tokens or None,
            cache_hit=cache_hit,
            estimated_cost_usd=cost,
        )
        return gen_result

    except ProviderUnavailable as exc:
        if console is not None:
            console.print("[yellow]⚠ API unavailable, using template fallback[/yellow]")

        gen_result = GenerationResult(
            text=fallback_generator(),
            is_fallback=True,
            model=cfg.model,
            input_tokens=total_input_tokens or None,
            output_tokens=total_output_tokens or None,
            estimated_cost_usd=chunk_cost,
            fallback_reason=str(exc),
        )
        return gen_result

    finally:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if command is not None and "gen_result" in locals():
            safe_record_telemetry(
                command=command,
                model=gen_result.model,
                tokens_in=gen_result.input_tokens,
                tokens_out=gen_result.output_tokens,
                cache_hit=gen_result.cache_hit,
                latency_ms=latency_ms,
                fallback_used=gen_result.is_fallback,
                estimated_cost_usd=gen_result.estimated_cost_usd,
                free_tier=cfg.free_tier,
            )

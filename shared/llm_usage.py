"""LLM usage logging + cost calculation.

Every Anthropic / OpenAI / xAI call in the bot wraps its response in one
of the `record_*` helpers below. They:

  1. Extract token counts from the provider's usage object (shapes differ)
  2. Look up the model in PRICING and compute estimated_cost_usd
  3. Insert a row into llm_usage

All failures are swallowed — logging must NEVER break a live LLM call.
Silent tracking is fine; a crashed logger that breaks the heartbeat is not.

Pricing — USD per MILLION tokens. Update when providers change rates.
Cache pricing follows Anthropic's ephemeral cache conventions:
  cache_read   = 10% of input rate  (cached prefix hits)
  cache_write  = 125% of input rate (first-write creates the cache entry)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from shared.models.base import SessionLocal
from shared.models.llm_usage import LlmUsage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing table — USD per million tokens
# ---------------------------------------------------------------------------

# (input, output) per MTok. Cache rates are derived: read=10%, write=125%.
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    # Claude 4.6 family (current as of 2026-04)
    "claude-opus-4-6":              (15.0, 75.0),
    "claude-opus-4-6-20250514":     (15.0, 75.0),
    "claude-sonnet-4-6":            (3.0, 15.0),
    "claude-sonnet-4-6-20250514":   (3.0, 15.0),
    "claude-haiku-4-5":             (1.0, 5.0),
    "claude-haiku-4-5-20251001":    (1.0, 5.0),
    # Legacy fallbacks
    "claude-3-5-sonnet-20241022":   (3.0, 15.0),
    "claude-3-5-haiku-20241022":    (1.0, 5.0),
}

# xAI Grok pricing (as user stated: $2 in / $6 out per MTok for grok-4.20)
_XAI_PRICING: dict[str, tuple[float, float]] = {
    "grok-4.20-0309-reasoning":     (2.0, 6.0),
    "grok-4.20-0309-non-reasoning": (2.0, 6.0),
    "grok-3":                        (3.0, 15.0),  # legacy fallback
}

# OpenAI pricing if we ever call it directly
_OPENAI_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o":          (2.50, 10.0),
    "gpt-4o-mini":     (0.15, 0.60),
}


def _anthropic_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """Estimate cost in USD from Anthropic token counts."""
    rates = _ANTHROPIC_PRICING.get(model)
    if rates is None:
        # Best-effort fallback — assume Sonnet-ish pricing so we don't zero
        # out unknown models. Better to over-report cost than hide it.
        rates = (3.0, 15.0)

    input_rate, output_rate = rates
    cache_read_rate = input_rate * 0.10   # 90% discount on cache hits
    cache_write_rate = input_rate * 1.25  # 25% premium on cache creation

    per_million = 1_000_000.0
    return (
        (input_tokens * input_rate) / per_million
        + (output_tokens * output_rate) / per_million
        + (cache_read_tokens * cache_read_rate) / per_million
        + (cache_creation_tokens * cache_write_rate) / per_million
    )


def _xai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _XAI_PRICING.get(model, (2.0, 6.0))
    per_million = 1_000_000.0
    return (input_tokens * rates[0] + output_tokens * rates[1]) / per_million


def _openai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _OPENAI_PRICING.get(model, (2.50, 10.0))
    per_million = 1_000_000.0
    return (input_tokens * rates[0] + output_tokens * rates[1]) / per_million


# ---------------------------------------------------------------------------
# Service name resolution — so each process tags its rows correctly
# ---------------------------------------------------------------------------

def _resolve_service() -> str:
    """Return the service name for the current process.

    Each docker container sets SERVICE_NAME in its environment (see
    docker-compose.yml). Falls back to a best-effort guess from the
    process path if SERVICE_NAME isn't set.
    """
    explicit = os.environ.get("SERVICE_NAME")
    if explicit:
        return explicit
    # Best-effort fallback — look at the current working directory
    cwd = os.getcwd()
    for candidate in ("ai-brain", "dashboard", "sentiment", "analyzer", "notifier", "data-collector"):
        if candidate in cwd:
            return candidate
    return "unknown"


# ---------------------------------------------------------------------------
# Record functions — call these from each LLM call site
# ---------------------------------------------------------------------------

def _insert(
    call_site: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cost_usd: float,
    duration_ms: float | None,
    success: bool,
) -> None:
    """Low-level insert — never raises. All failures logged and swallowed."""
    try:
        with SessionLocal() as session:
            row = LlmUsage(
                ts=datetime.now(tz=timezone.utc),
                service=_resolve_service(),
                call_site=call_site[:64],
                model=model[:64],
                provider=provider,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cache_read_tokens=int(cache_read_tokens),
                cache_creation_tokens=int(cache_creation_tokens),
                estimated_cost_usd=round(cost_usd, 6),
                duration_ms=duration_ms,
                success=success,
            )
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("llm_usage insert failed (call_site=%s)", call_site)


def record_anthropic_call(
    call_site: str,
    model: str,
    usage: Any,
    duration_ms: float | None = None,
    success: bool = True,
) -> None:
    """Log an Anthropic messages.create response's usage object.

    `usage` is the `response.usage` attribute from the Anthropic SDK.
    It exposes: input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens.
    """
    try:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    except Exception:
        input_tokens = output_tokens = cache_read = cache_creation = 0

    cost = _anthropic_cost(model, input_tokens, output_tokens, cache_read, cache_creation)
    _insert(
        call_site=call_site,
        model=model,
        provider="anthropic",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=cost,
        duration_ms=duration_ms,
        success=success,
    )


def record_openai_compatible_call(
    call_site: str,
    model: str,
    usage: Any,
    duration_ms: float | None = None,
    success: bool = True,
    provider: str = "xai",
) -> None:
    """Log an OpenAI-compatible (OpenAI SDK or xAI via OpenAI SDK) response's usage.

    Usage object has: prompt_tokens, completion_tokens, total_tokens.
    """
    try:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    except Exception:
        prompt_tokens = completion_tokens = 0

    if provider == "xai":
        cost = _xai_cost(model, prompt_tokens, completion_tokens)
    else:
        cost = _openai_cost(model, prompt_tokens, completion_tokens)

    _insert(
        call_site=call_site,
        model=model,
        provider=provider,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=cost,
        duration_ms=duration_ms,
        success=success,
    )


def record_failure(
    call_site: str,
    model: str,
    provider: str,
    duration_ms: float | None = None,
) -> None:
    """Log a failed call — zero tokens, zero cost, success=False.

    Useful so the dashboard can show failure rates per call site.
    """
    _insert(
        call_site=call_site,
        model=model,
        provider=provider,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0,
        duration_ms=duration_ms,
        success=False,
    )

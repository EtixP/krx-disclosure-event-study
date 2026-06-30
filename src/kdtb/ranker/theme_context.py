"""Optional LLM annotation of hot themes — context, NOT a decision input.

The theme TILT that affects rankings is 100% data-driven (basket momentum). This
module only adds a one-line *explanation* of why a theme might be active right
now, using the LLM as a narration layer. It never changes a score. Per
CLAUDE.md: the LLM enriches/explains; deterministic code decides.

Activates only when an LLM provider + API key is configured (LLM_PROVIDER +
ANTHROPIC_API_KEY / OPENAI_API_KEY). Without one it returns no annotations and
the ranker runs unchanged.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from kdtb.interpretation.llm_client import build_client

logger = logging.getLogger(__name__)

_PROMPT = """You are a Korean equity market analyst. In ONE factual sentence, explain the \
most likely current driver behind strength or weakness in the "{theme}" theme on the \
Korean (KOSPI/KOSDAQ) market. Do NOT give buy/sell advice or price targets — describe the \
macro/policy/industry context only.{news}
Output only the single sentence."""


def annotate_hot_themes(
    hot: list[tuple[str, float]],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    news_by_theme: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Return {theme_name: one-line context}. Empty dict if no LLM is configured."""
    provider = provider or os.getenv("LLM_PROVIDER") or "stub"
    try:
        client = build_client(provider, model)  # raises if key/SDK missing
    except Exception as e:
        logger.info("theme context disabled (%s): %s", provider, e)
        return {}

    out: dict[str, str] = {}
    for name, _strength in hot:
        snippet = (news_by_theme or {}).get(name, "")
        news = f"\nRecent headlines for context:\n{snippet}" if snippet else ""
        prompt = _PROMPT.format(theme=name, news=news)
        try:
            txt = client.complete(prompt, max_tokens=120, temperature=0)
        except NotImplementedError:
            return {}  # stub provider — nothing configured
        except Exception as e:
            logger.warning("theme context failed for %s: %s", name, e)
            continue
        if txt and txt.strip():
            out[name] = txt.strip()
    return out
